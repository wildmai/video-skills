"""阶段 6：豆包 TTS 配音。

调用豆包语音合成大模型（HTTP v3 单向流式）生成配音，并取回字级时间戳。
用法：
  单句自检：   doubao_tts.py --doctor --api-key KEY --speaker SPEAKER
  批量配音：   doubao_tts.py --script work/script.json --out-dir work/tts
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from common import (  # noqa: E402
    EnvConfig,
    StageError,
    StageLogger,
    ffprobe_bin,
    load_json,
    run,
    save_json,
)
import word_align  # noqa: E402

API_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
DEFAULT_RESOURCE_ID = "seed-icl-2.0"
CODE_FINAL = 20000000

# 声音复刻系列的 resource-id：这些才允许带 model 参数
ICL_RESOURCE_IDS = {"seed-icl-2.0", "seed-icl-1.0", "seed-icl-1.0-concurr"}
# model 枚举值（仅对声音复刻 2.0 生效）
MODEL_ALIASES = {
    "auto": "seed-tts-2.0-standard",
    "standard": "seed-tts-2.0-standard",
    "expressive": "seed-tts-2.0-expressive",
    "none": "",
    "": "",
}

EMOJI_RE = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F1E6-\U0001F1FF" "☀-➿" "️" "]+"
)
MARKDOWN_RE = re.compile(r"(\*{1,3}|_{1,3}|`{1,3}|~~|>\s*|\[|\]|\(|\))")
SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;…])|(?<=\n)")


def sanitize_text(text: str) -> tuple[str, list[str]]:
    """去掉 Markdown 记号与表情，避免被朗读出来。返回 (净化后文本, 变更说明)。"""
    notes: list[str] = []
    original = text
    text = text.replace("\r\n", "\n")
    text = EMOJI_RE.sub("", text)
    text = MARKDOWN_RE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    if text != original:
        notes.append("已移除 Markdown 记号/表情，避免被朗读")
    return text, notes


def split_sentences(text: str, max_len: int = 60) -> list[str]:
    """按中文标点切句；超长句再按逗号切。"""
    parts = [p.strip() for p in SENT_SPLIT_RE.split(text) if p and p.strip()]
    out: list[str] = []
    for part in parts:
        if len(part) <= max_len:
            out.append(part)
            continue
        buffer = ""
        for piece in re.split(r"(?<=[，,、])", part):
            if not piece:
                continue
            if len(buffer) + len(piece) <= max_len:
                buffer += piece
            else:
                if buffer:
                    out.append(buffer)
                buffer = piece
        if buffer:
            out.append(buffer)
    return out


def measure_duration(path: pathlib.Path) -> float:
    proc = run([ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    try:
        return round(float(proc.stdout.strip()), 3)
    except ValueError:
        return 0.0


def synthesize(text: str, api_key: str, speaker: str, resource_id: str = DEFAULT_RESOURCE_ID,
              audio_params: dict | None = None, additions: dict | None = None,
              model: str | None = None, timeout: int = 180, retries: int = 3,
              logger: StageLogger | None = None) -> dict:
    """调用豆包 TTS，返回 {audio: bytes, words: [...], duration: float, usage: {...}}。"""
    try:
        import requests
    except ImportError as exc:
        raise StageError("未安装 requests", hint="pip install requests") from exc

    payload: dict = {"req_params": {"text": text, "speaker": speaker}}
    if model:
        payload["req_params"]["model"] = model
    params = {"format": "mp3", "sample_rate": 24000, "enable_subtitle": True}
    params.update(audio_params or {})
    payload["req_params"]["audio_params"] = params
    if additions:
        payload["req_params"]["additions"] = json.dumps(additions, ensure_ascii=False)

    headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": resource_id,
        "Content-Type": "application/json",
        "Connection": "keep-alive",
    }

    last_error = ""
    for attempt in range(1, retries + 1):
        audio = bytearray()
        words: list[dict] = []
        usage: dict = {}
        offset = 0.0
        last_fingerprint: tuple | None = None
        try:
            resp = requests.post(API_URL, headers=headers, json=payload, stream=True, timeout=timeout)
            if resp.status_code != 200:
                body = resp.text[:500]
                last_error = f"HTTP {resp.status_code}: {body}"
                if resp.status_code in (401, 403):
                    raise StageError(
                        f"豆包 TTS 鉴权失败（HTTP {resp.status_code}）：{body}",
                        hint="检查 API Key 是否有效、X-Api-Resource-Id 是否与账号开通的资源一致",
                    )
                if resp.status_code in (400, 404):
                    raise StageError(
                        f"豆包 TTS 请求被拒绝（HTTP {resp.status_code}）：{body}",
                        hint="检查 speaker 音色 ID、resource-id、以及文本长度是否超限",
                    )
                raise StageError(f"豆包 TTS 请求失败（HTTP {resp.status_code}）：{body}")
            for raw_line in resp.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                try:
                    chunk = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                code = chunk.get("code", 0)
                if code == 0:
                    data = chunk.get("data")
                    if data:
                        audio.extend(base64.b64decode(data))
                    sentence = chunk.get("sentence") or {}
                    chunk_words = sentence.get("words") or []
                    if chunk_words:
                        # 豆包返回的 word 时间戳是「相对整段音频」的绝对时间（每句一条记录）。
                        # 只有当某条记录的时间出现回退时，才说明它是分片相对时间，需要补偏移。
                        min_start = min(float(w.get("startTime", 0)) for w in chunk_words)
                        base = 0.0 if min_start >= offset - 0.05 else offset
                        fingerprint = (chunk_words[0].get("word"),
                                       round(float(chunk_words[0].get("startTime", 0)), 3))
                        if fingerprint != last_fingerprint:
                            for w in chunk_words:
                                words.append({
                                    "word": w.get("word", ""),
                                    "start": round(float(w.get("startTime", 0)) + base, 3),
                                    "end": round(float(w.get("endTime", 0)) + base, 3),
                                })
                            last_fingerprint = fingerprint
                        offset = max([w["end"] for w in words] + [offset])
                    usage = chunk.get("usage") or usage
                elif code == CODE_FINAL:
                    break
                elif code > 0:
                    raise StageError(
                        f"豆包 TTS 返回错误 code={code} message={chunk.get('message')}",
                        hint="见 references/doubao-tts.md 的错误码对照表",
                    )
            if not audio:
                raise StageError("豆包 TTS 返回空音频", hint="检查文本是否为空、音色 ID 是否正确")
            return {"audio": bytes(audio), "words": words, "usage": usage}
        except StageError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            wait = 2 ** attempt
            if logger:
                logger.warn(f"配音请求失败（{last_error}），{wait}s 后重试（第 {attempt} 次）")
            time.sleep(wait)
    raise StageError(f"豆包 TTS 连续 {retries} 次失败：{last_error}", hint="检查网络与 API Key")


def build_additions(tone: str | None, disable_markdown: bool = True) -> dict:
    additions = {
        "disable_markdown_filter": disable_markdown,
        "disable_emoji_filter": True,
    }
    if tone:
        additions["context_texts"] = [tone]
    return additions


def main() -> int:
    ap = argparse.ArgumentParser(description="豆包 TTS 配音")
    ap.add_argument("--script", help="脚本 JSON（批量配音，读取每个 section 的 vo）")
    ap.add_argument("--text", help="直接合成一段文本")
    ap.add_argument("--out-dir", help="音频输出目录")
    ap.add_argument("--work-dir", default="", help="工作目录（只影响日志位置，默认取 --out-dir 的上级）")
    ap.add_argument("--api-key", default=None, help="覆盖 .env 里的 DOUBAO_API_KEY")
    ap.add_argument("--speaker", default=None, help="覆盖 .env 里的 DOUBAO_SPEAKER（音色 ID）")
    ap.add_argument("--resource-id", default=None,
                    help="覆盖 .env 里的 DOUBAO_RESOURCE_ID，默认 seed-icl-2.0（声音复刻 2.0）")
    ap.add_argument("--model", default=None,
                    help="覆盖 .env 里的 DOUBAO_MODEL：auto/standard/expressive/none，或完整枚举值")
    ap.add_argument("--config", action="store_true", help="打印当前生效的配置（含 .env 路径）后退出")
    ap.add_argument("--format", default="mp3", choices=["mp3", "wav", "pcm", "ogg_opus"])
    ap.add_argument("--sample-rate", type=int, default=24000)
    ap.add_argument("--speech-rate", type=int, default=0, help="语速 [-50,100]")
    ap.add_argument("--pitch", type=int, default=0, help="音调 [-12,12]")
    ap.add_argument("--loudness-rate", type=int, default=0)
    ap.add_argument("--tone", help="语气指令，如「请说慢一些，语速平稳」")
    ap.add_argument("--doctor", action="store_true", help="只合成一句测试音并报告")
    ap.add_argument("--dry-run", action="store_true", help="只打印请求体，不发起请求")
    args = ap.parse_args()

    if args.work_dir:
        work_dir = pathlib.Path(args.work_dir)
    elif args.out_dir:
        work_dir = pathlib.Path(args.out_dir).parent
    else:
        work_dir = pathlib.Path.cwd()
    logger = StageLogger(work_dir, "tts")

    # 配置来源：命令行 > 进程环境变量 > .env 文件（就近优先）
    env = EnvConfig(work_dir)
    api_key, api_key_src = env.pick(args.api_key, "DOUBAO_API_KEY")
    speaker, speaker_src = env.pick(args.speaker, "DOUBAO_SPEAKER")
    resource_id, resource_src = env.pick(args.resource_id, "DOUBAO_RESOURCE_ID", DEFAULT_RESOURCE_ID)
    model_key, model_src = env.pick(args.model, "DOUBAO_MODEL", "auto")

    if args.config:
        print("当前生效的豆包配置（优先级：命令行 > 环境变量 > .env 就近优先）：")
        print(f"  --api-key      = {EnvConfig.mask(api_key)}   ← {api_key_src}")
        print(f"  --speaker      = {speaker or '(未设置)'}   ← {speaker_src}")
        print(f"  --resource-id  = {resource_id}   ← {resource_src}")
        print(f"  --model        = {model_key}   ← {model_src}")
        print("\n读取的 .env 文件：")
        for f in env.files:
            print(f"  - {f}")
        if not env.files:
            created = env.ensure_env_file(work_dir)
            print(f"  （没有找到 .env，已创建模板：{created}，填好即可用）")
        return 0

    if not api_key:
        raise StageError(
            "缺少 API Key",
            hint=f"在 {env.ensure_env_file(work_dir)} 里填 DOUBAO_API_KEY，或用 --api-key 传入",
        )
    if not speaker:
        raise StageError(
            "缺少音色 ID",
            hint=f"在 {env.ensure_env_file(work_dir)} 里填 DOUBAO_SPEAKER，或用 --speaker 传入",
        )

    # model 参数只对声音复刻（seed-icl-*）生效，其他资源一律不发。
    model_value = MODEL_ALIASES.get(model_key.lower(), model_key.strip())
    if resource_id in ICL_RESOURCE_IDS:
        if not model_value:
            raise StageError(
                "声音复刻音色必须带 model 参数",
                hint="用 --model standard（默认）或 --model expressive",
            )
    else:
        if model_key.lower() not in ("", "auto", "none"):
            logger.warn(f"model 参数仅对声音复刻（seed-icl-*）生效，{resource_id} 下已忽略")
        model_value = ""

    if args.tone and model_value:
        raise StageError(
            "声音复刻音色不支持语音指令 context_texts（与 model 参数互斥）",
            hint="去掉 --tone，用文案本身控制语气；或改用预置音色 --resource-id seed-tts-2.0",
        )

    audio_params = {
        "format": args.format,
        "sample_rate": args.sample_rate,
        "speech_rate": args.speech_rate,
        "pitch": args.pitch,
    }
    if args.loudness_rate:
        audio_params["loudness_rate"] = args.loudness_rate
    additions = build_additions(args.tone)

    if args.dry_run:
        print(json.dumps({"url": API_URL,
                          "headers": {"X-Api-Key": "***", "X-Api-Resource-Id": resource_id},
                          "req_params": {"text": args.text or "<from script>", "speaker": speaker,
                                         "model": model_value or None,
                                         "audio_params": audio_params, "additions": additions}},
                         ensure_ascii=False, indent=2))
        return 0

    if args.doctor:
        probe_text = "你好，这是一次豆包语音合成测试。"
        result = synthesize(probe_text, api_key, speaker, resource_id,
                            audio_params, additions, model_value or None, logger=logger)
        logger.log(f"自检通过：{len(result['audio'])} 字节，字级时间戳 {len(result['words'])} 个，"
                   f"计费字符数 {result.get('usage', {}).get('text_words', 'n/a')}")
        return 0

    if not args.script:
        if not args.text:
            raise StageError("需要 --script 或 --text 或 --doctor", hint="用法见脚本头部说明")
        if not args.out_dir:
            raise StageError("--text 模式需要 --out-dir 指定输出目录")
        out_dir = pathlib.Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        vo, notes = sanitize_text(args.text)
        for note in notes:
            logger.warn(note)
        result = synthesize(vo, api_key, speaker, resource_id,
                            audio_params, additions, model_value or None, logger=logger)
        out_path = out_dir / "text.mp3"
        out_path.write_bytes(result["audio"])
        duration = measure_duration(out_path)
        logger.log(f"已生成 {out_path}（{duration:.2f}s / {len(result['audio'])} 字节 / "
                   f"字级时间戳 {len(result['words'])} 个）")
        return 0
    if not args.out_dir:
        raise StageError("批量配音需要 --out-dir")

    script = load_json(args.script)
    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sections = script.get("sections") or []
    if not sections:
        raise StageError("脚本里没有 sections", hint="检查 script.json 结构")

    index = {
        "engine": "doubao",          # 标记配音引擎：预览稿（edge-tts）不能被当成正式配音复用
        "speaker": speaker,
        "resource_id": resource_id,
        "model": model_value,
        "format": args.format,
        "sample_rate": args.sample_rate,
        "sections": {},
    }
    total_chars = 0
    for sec in sections:
        sid = sec.get("id") or f"s{len(index['sections']) + 1}"
        raw_vo = (sec.get("vo") or "").strip()
        if not raw_vo:
            logger.warn(f"{sid} 没有 vo 文本，跳过")
            index["sections"][sid] = {"duration": 0.0, "sentences": []}
            continue
        vo, notes = sanitize_text(raw_vo)
        for note in notes:
            logger.warn(f"{sid}: {note}")
        sentences = split_sentences(vo)
        entries = []
        cursor = 0.0
        for i, sentence in enumerate(sentences, start=1):
            result = synthesize(sentence, api_key, speaker, resource_id,
                                audio_params, additions, model_value or None, logger=logger)
            audio_path = out_dir / f"{sid}_{i:03d}.{args.format}"
            audio_path.write_bytes(result["audio"])
            duration = measure_duration(audio_path)
            raw_words = result["words"]
            if duration <= 0:
                duration = round(max((w["end"] for w in raw_words), default=0.0), 3)
            # 兜底：字级时间戳不该超出音频时长，超了就整体缩放到真实时长上
            if raw_words and duration > 0 and raw_words[-1]["end"] > duration + 0.5:
                scale = duration / raw_words[-1]["end"]
                logger.warn(f"{sid}[{i}] 字级时间戳({raw_words[-1]['end']:.2f}s)超出音频({duration:.2f}s)，"
                            f"按 {scale:.3f} 缩放对齐")
                raw_words = [
                    {"word": w["word"], "start": round(w["start"] * scale, 3), "end": round(w["end"] * scale, 3)}
                    for w in raw_words
                ]
            words = [
                {"word": w["word"], "start": round(cursor + w["start"], 3), "end": round(cursor + w["end"], 3)}
                for w in raw_words
            ]
            # 保留 API 原始时间戳（句内相对秒）：豆包对中英混排的词元时间戳不可靠
            # （拉丁词元时长被压成 30ms，字幕会闪断），对齐后仍需保留原值，
            # 这样 align_words.py 重跑时从同一份输入出发，结果幂等。
            api_words = [{"word": w["word"],
                          "start": round(w["start"], 3),
                          "end": round(w["end"], 3)} for w in raw_words]
            if api_words:
                aligned = word_align.align_words(api_words, str(audio_path), duration)
                words = [{"word": w["word"],
                          "start": round(cursor + a["start"], 3),
                          "end": round(cursor + a["end"], 3)}
                         for w, a in zip(words, aligned)]
            entries.append({
                "index": i,
                "text": sentence,
                "audio": str(audio_path.resolve()),
                "duration": duration,
                "start": round(cursor, 3),
                "end": round(cursor + duration, 3),
                "words": words,
                "api_words": api_words,
                "regions": [[round(a, 3), round(b, 3)]
                            for a, b in word_align.detect_regions(str(audio_path), duration)],
            })
            cursor = round(cursor + duration, 3)
            total_chars += len(sentence)
            logger.log(f"{sid}[{i}] {duration:.2f}s | {sentence[:40]}")
        index["sections"][sid] = {"duration": round(cursor, 3), "sentences": entries}
        logger.log(f"{sid} 完成：{len(entries)} 句 / {cursor:.2f}s")

    index["total_duration"] = round(sum(s["duration"] for s in index["sections"].values()), 3)
    index["total_chars"] = total_chars
    save_json(out_dir / "index.json", index)
    logger.log(f"全部完成：{total_chars} 字 / {index['total_duration']:.1f}s → {out_dir / 'index.json'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StageError as err:
        print(f"[ERROR] {err}", file=sys.stderr)
        if err.hint:
            print(f"[HINT]  {err.hint}", file=sys.stderr)
        raise SystemExit(2)
