"""豆包音色克隆：注册自定义音色、查询音色状态。

注册：POST https://openspeech.bytedance.com/api/v3/tts/voice_clone
查询：POST https://openspeech.bytedance.com/api/v3/tts/get_voice

用法：
  注册：voice_clone.py register --audio ref.wav --text "参考音频里念的原文" --speaker-id my_voice
  查询：voice_clone.py query --speaker-id my_voice
  等待：voice_clone.py wait --speaker-id my_voice --timeout 300
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from common import (  # noqa: E402
    EnvConfig,
    StageError,
    StageLogger,
    ensure_dir,
    ffmpeg_bin,
    ffprobe_bin,
    run,
)

CLONE_URL = "https://openspeech.bytedance.com/api/v3/tts/voice_clone"
QUERY_URL = "https://openspeech.bytedance.com/api/v3/tts/get_voice"
MAX_UPLOAD_MB = 10
MAX_AUDIO_SECONDS = 30.0
STATUS_NAMES = {0: "NotFound", 1: "Training", 2: "Success", 3: "Failed", 4: "Active"}
USABLE_STATUS = (2, 4)


def http_json(url: str, payload: dict, api_key: str, timeout: int = 300) -> dict:
    try:
        import requests
    except ImportError as exc:
        raise StageError("未安装 requests", hint="pip install requests") from exc
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
        "X-Api-Request-Id": str(uuid.uuid4()),
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    body = resp.text or ""
    try:
        data = resp.json()
    except json.JSONDecodeError:
        data = {"code": resp.status_code, "message": body[:500]}
    if resp.status_code != 200 or (data.get("code") not in (0, None)):
        code = data.get("code")
        message = data.get("message") or body[:300]
        raise StageError(
            f"豆包音色接口返回错误（HTTP {resp.status_code}, code={code}）：{message}",
            hint="对照 references/voice-clone.md 的错误码表排查",
        )
    return data


def prepare_audio(src: str, max_seconds: float, logger: StageLogger) -> tuple[bytes, float]:
    """把参考音频转成 24k 单声道 wav，并按需截断，返回 (字节, 时长秒)。"""
    src_path = pathlib.Path(src)
    if not src_path.exists():
        raise StageError(f"参考音频不存在：{src}", hint="确认音频路径")

    proc = run([ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(src_path)], check=False)
    try:
        duration = float(proc.stdout.strip())
    except ValueError:
        duration = 0.0

    tmp_dir = ensure_dir(pathlib.Path.cwd() / "_clone_tmp")
    out = tmp_dir / "reference.wav"
    cmd = [ffmpeg_bin(), "-y", "-v", "error", "-i", str(src_path),
           "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le"]
    if duration > max_seconds:
        logger.warn(f"参考音频 {duration:.1f}s 超过 {max_seconds:.0f}s，已截断")
        cmd += ["-t", f"{max_seconds:.0f}"]
    cmd += [str(out)]
    run(cmd, timeout=600)

    data = out.read_bytes()
    size_mb = len(data) / 1024 / 1024
    if size_mb > MAX_UPLOAD_MB:
        out.unlink(missing_ok=True)
        raise StageError(
            f"处理后的参考音频 {size_mb:.1f}MB 超过 {MAX_UPLOAD_MB}MB 上限",
            hint="换更短/更小的参考音频，或降低 --max-seconds",
        )
    final_duration = duration
    if final_duration > max_seconds:
        final_duration = max_seconds
    out.unlink(missing_ok=True)
    try:
        tmp_dir.rmdir()
    except OSError:
        pass
    return data, final_duration


def cmd_register(args: argparse.Namespace, logger: StageLogger) -> int:
    api_key = args.api_key
    speaker_id = args.speaker_id
    custom_speaker_id = args.custom_speaker_id
    if custom_speaker_id:
        # 后付费音色：speaker_id 固定为 custom_speaker_id
        speaker_id = "custom_speaker_id"
    if not speaker_id:
        raise StageError("缺少音色代号", hint="用 --speaker-id 指定，或用 --custom-speaker-id 指定后付费音色代号")
    if not args.audio:
        raise StageError("缺少参考音频", hint="用 --audio 指定本地音频文件")

    data, duration = prepare_audio(args.audio, args.max_seconds, logger)
    logger.log(f"参考音频已处理：{len(data) / 1024:.0f} KB / 约 {duration:.1f}s")

    payload = {
        "speaker_id": speaker_id,
        "audio": {
            "format": "wav",
            "data": base64.b64encode(data).decode("ascii"),
        },
    }
    if custom_speaker_id:
        payload["custom_speaker_id"] = custom_speaker_id
    if args.text:
        payload["audio"]["text"] = args.text
    if args.language is not None:
        payload["audio"]["language"] = args.language
    extra = {}
    if args.demo_text:
        extra["demo_text"] = args.demo_text
    if args.denoise:
        extra["enable_audio_denoise"] = True
    if args.no_volume_normalization:
        extra["disable_volume_normalization"] = True
    if extra:
        payload["extra_params"] = extra

    logger.log(f"注册音色：speaker_id={speaker_id}"
               + (f" / custom={custom_speaker_id}" if custom_speaker_id else ""))
    result = http_json(CLONE_URL, payload, api_key, timeout=args.timeout)
    logger.log(f"注册成功：code={result.get('code')} 可用复刻次数={result.get('available_training_times')} "
               f"创建时间={result.get('create_time')}")

    out = {"speaker_id": speaker_id}
    if custom_speaker_id:
        out["custom_speaker_id"] = custom_speaker_id
    out.update({k: result.get(k) for k in ("code", "message", "available_training_times", "create_time")})
    print(json.dumps(out, ensure_ascii=False, indent=2))
    logger.log("下一步：等训练完成后用 query/wait 查状态，status 为 2(Success) 或 4(Active) 即可用于合成")
    return 0


def _query(api_key: str, speaker_id: str, custom_speaker_id: str | None) -> dict:
    payload = {"speaker_id": speaker_id}
    if custom_speaker_id:
        payload["custom_speaker_id"] = custom_speaker_id
    return http_json(QUERY_URL, payload, api_key, timeout=120)


def cmd_query(args: argparse.Namespace, logger: StageLogger) -> int:
    api_key = args.api_key
    speaker_id = args.speaker_id or ("custom_speaker_id" if args.custom_speaker_id else "")
    if not speaker_id:
        raise StageError("缺少音色代号", hint="用 --speaker-id 指定")

    result = _query(api_key, speaker_id, args.custom_speaker_id)
    status = result.get("status")
    logger.log(f"speaker_id={result.get('speaker_id', speaker_id)} "
               f"status={status}({STATUS_NAMES.get(status, '未知')}) "
               f"model_type={result.get('model_type')} "
               f"剩余复刻次数={result.get('available_training_times')}")

    if args.save_demo and result.get("demo_audio"):
        out_dir = ensure_dir(pathlib.Path(args.out_dir or "."))
        demo_path = out_dir / f"{speaker_id}_demo.mp3"
        demo_path.write_bytes(base64.b64decode(result["demo_audio"]))
        logger.log(f"试听音频已保存 → {demo_path}（1 小时内有效）")

    print(json.dumps({k: v for k, v in result.items() if k != "demo_audio"}, ensure_ascii=False, indent=2))
    if status not in USABLE_STATUS:
        logger.warn(f"当前状态 {STATUS_NAMES.get(status, status)} 还不能用于合成"
                    f"（可用的状态是 2=Success、4=Active）")
        return 1
    return 0


def cmd_wait(args: argparse.Namespace, logger: StageLogger) -> int:
    api_key = args.api_key
    speaker_id = args.speaker_id or ("custom_speaker_id" if args.custom_speaker_id else "")
    if not speaker_id:
        raise StageError("缺少音色代号", hint="用 --speaker-id 指定")

    deadline = time.time() + args.timeout
    status = None
    while time.time() < deadline:
        result = _query(api_key, speaker_id, args.custom_speaker_id)
        status = result.get("status")
        logger.log(f"状态 {status}({STATUS_NAMES.get(status, '未知')})")
        if status in USABLE_STATUS:
            logger.log("音色已可用，可以开始合成")
            print(json.dumps({"speaker_id": speaker_id, "status": status,
                              "status_name": STATUS_NAMES.get(status)}, ensure_ascii=False))
            return 0
        if status == 3:
            raise StageError("音色训练失败（status=Failed）", hint="换一段参考音频重试，注意人声清晰、无背景音")
        time.sleep(args.interval)
    raise StageError(f"等待超时（{args.timeout}s）音色仍未就绪", hint="稍后再用 wait 查询，或到控制台查看")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="豆包音色克隆",
        epilog="不带子命令时等价于 --config（打印当前生效的配置）",
    )
    # 顶层也接受这几个参数，方便 `voice_clone.py --config` 直接用。
    # 子命令里用 SUPPRESS 作默认值，避免子解析器用默认值把顶层的值覆盖掉。
    ap.add_argument("--api-key", default=None, help="覆盖 .env 里的 DOUBAO_API_KEY")
    ap.add_argument("--speaker-id", default=None,
                    help="音色代号；预付费音色填控制台给的 ID，后付费填 custom_speaker_id")
    ap.add_argument("--custom-speaker-id", default=None,
                    help="后付费音色的自定义代号（会走 speaker_id=custom_speaker_id）")
    ap.add_argument("--work-dir", default="", help="日志目录")
    ap.add_argument("--config", action="store_true", help="打印当前生效的配置后退出")
    sub = ap.add_subparsers(dest="cmd")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--api-key", default=argparse.SUPPRESS, help="覆盖 .env 里的 DOUBAO_API_KEY")
        p.add_argument("--speaker-id", default=argparse.SUPPRESS,
                       help="音色代号；预付费音色填控制台给的 ID，后付费填 custom_speaker_id")
        p.add_argument("--custom-speaker-id", default=argparse.SUPPRESS,
                       help="后付费音色的自定义代号（会走 speaker_id=custom_speaker_id）")
        p.add_argument("--work-dir", default=argparse.SUPPRESS, help="日志目录")
        p.add_argument("--config", action="store_true", default=argparse.SUPPRESS,
                       help="打印当前生效的配置后退出")

    reg = sub.add_parser("register", help="注册自定义音色")
    common(reg)
    reg.add_argument("--audio", default=None, help="本地参考音频")
    reg.add_argument("--text", default="", help="参考音频里念的原文（强烈建议提供，WER 校验用）")
    reg.add_argument("--language", type=int, default=None, help="语种枚举，中文=0 英文=1")
    reg.add_argument("--demo-text", default="", help="试听文本（4-300 字）")
    reg.add_argument("--denoise", action="store_true", help="开启降噪（音频噪声大时用，会损失细节）")
    reg.add_argument("--no-volume-normalization", action="store_true")
    reg.add_argument("--max-seconds", type=float, default=MAX_AUDIO_SECONDS)
    reg.add_argument("--timeout", type=int, default=300)
    reg.set_defaults(func=cmd_register)

    qry = sub.add_parser("query", help="查询音色状态")
    common(qry)
    qry.add_argument("--save-demo", action="store_true", help="把试听音频下载到本地")
    qry.add_argument("--out-dir", default=".")
    qry.set_defaults(func=cmd_query)

    wai = sub.add_parser("wait", help="轮询直到音色可用")
    common(wai)
    wai.add_argument("--timeout", type=int, default=300)
    wai.add_argument("--interval", type=int, default=10)
    wai.set_defaults(func=cmd_wait)

    args = ap.parse_args()
    work_dir = pathlib.Path(getattr(args, "work_dir", "") or ".").resolve()
    logger = StageLogger(work_dir, "voice_clone")

    # 配置来源：命令行 > 进程环境变量 > .env 文件（就近优先）
    env = EnvConfig(work_dir)
    api_key, api_key_src = env.pick(getattr(args, "api_key", None), "DOUBAO_API_KEY")
    speaker_id, speaker_src = env.pick(getattr(args, "speaker_id", None), "DOUBAO_SPEAKER_ID")
    custom_speaker_id, custom_src = env.pick(getattr(args, "custom_speaker_id", None),
                                             "DOUBAO_CUSTOM_SPEAKER_ID")
    args.api_key = api_key
    args.speaker_id = speaker_id or ""
    args.custom_speaker_id = custom_speaker_id or ""

    if getattr(args, "config", False) or not args.cmd:
        print("当前生效的豆包配置（优先级：命令行 > 环境变量 > .env 就近优先）：")
        print(f"  --api-key          = {EnvConfig.mask(api_key)}   ← {api_key_src}")
        print(f"  --speaker-id       = {args.speaker_id or '(未设置)'}   ← {speaker_src}")
        print(f"  --custom-speaker-id= {args.custom_speaker_id or '(未设置)'}   ← {custom_src}")
        print("\n读取的 .env 文件：")
        for f in env.files:
            print(f"  - {f}")
        if not env.files:
            created = env.ensure_env_file(work_dir)
            print(f"  （没有找到 .env，已创建模板：{created}，填好即可用）")
        if not args.cmd:
            print("\n可用子命令：register / query / wait")
        return 0

    if not args.api_key:
        raise StageError(
            "缺少 API Key",
            hint=f"在 {env.ensure_env_file(work_dir)} 里填 DOUBAO_API_KEY，或用 --api-key 传入",
        )
    return args.func(args, logger)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StageError as err:
        print(f"[ERROR] {err}", file=sys.stderr)
        if err.hint:
            print(f"[HINT]  {err.hint}", file=sys.stderr)
        raise SystemExit(2)
