"""阶段 5b：预览配音（Edge TTS，免费）。

脚本名故意不叫 edge_tts.py——那会和 edge-tts 库同名，
运行时 `import edge_tts` 会把本脚本自己导进来（自引用），报
「module has no attribute 'Communicate'」。

为什么要有这一步
----------------
豆包声音复刻是按字计费的，正式配音前往往要反复改口播、试节奏。
先用免费的 Edge TTS 念一遍，用户确认文案与节奏没问题了，再用豆包合成正式版，
这一轮就只付一次钱。

Edge TTS 不需要 API Key、不计费、不限次数，中文音色有 8 个。
代价是音色不是用户自己的复刻音色，只能当「试听稿」。

产物
----
和 `doubao_tts.py` 完全同一套 `tts/index.json` 结构，
所以下游的 `build_timeline.py` / `render_video.py` 一行都不用改，
可以直接拿它渲一版预览片。

但索引里会记 `"engine": "edge-tts"`：
最终配音必须整篇重来，`tts_patch.py` 也会因此拒绝复用预览音频。

关于字级时间戳
--------------
Edge TTS 对中文只返回一个句级边界，没有词级边界。
所以这里自己把文本切成词元、按字符位置摊出「伪时间戳」，
再用 `word_align.align_words` 拿音频里的真实发声区间校正回去——
和豆包那条路走的是同一套校正逻辑，字幕时间轴因此一致。

用法:
  python scripts/tts_preview.py --work-dir <work> [--script <work>/script.json]
                              [--out-dir <work>/tts] [--voice zh-CN-XiaoxiaoNeural]
                              [--rate +0%] [--pitch +0Hz] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from common import (  # noqa: E402
    StageError,
    StageLogger,
    ffmpeg_bin,
    ffprobe_bin,
    load_json,
    run,
    save_json,
)
import doubao_tts as doubao  # noqa: E402
import word_align  # noqa: E402

ENGINE = "edge-tts"
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

# Edge 的中文音色（全部免费，无需 API Key）
ZH_VOICES = [
    "zh-CN-XiaoxiaoNeural",   # 女声，温暖亲和，通用首选
    "zh-CN-YunxiNeural",      # 男声，青年，节奏偏快
    "zh-CN-XiaoyiNeural",     # 女声，活泼
    "zh-CN-YunyangNeural",    # 男声，播音腔，稳
    "zh-CN-YunjianNeural",    # 男声，浑厚
    "zh-CN-YunxiaNeural",     # 女声，童声感
    "zh-CN-liaoning-XiaobeiNeural",
    "zh-CN-shaanxi-XiaoniNeural",
]


def measure_duration(path: pathlib.Path) -> float:
    proc = run([ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    try:
        return round(float(proc.stdout.strip()), 3)
    except ValueError:
        return 0.0


async def _synthesize(text: str, voice: str, rate: str, pitch: str) -> tuple[bytes, list[dict]]:
    try:
        import edge_tts
    except ImportError as exc:
        raise StageError(
            "没装 edge-tts，无法生成预览配音",
            hint="pip install edge-tts（免费开源，不需要 API Key）",
        ) from exc

    audio = bytearray()
    boundaries: list[dict] = []
    # boundary="WordBoundary" 才会吐词级边界（默认只有句级），
    # 中文也一样能拿到——这是预览稿能有准确字幕的关键。
    comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch,
                                 boundary="WordBoundary")
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
        elif chunk["type"] == "WordBoundary":
            boundaries.append({
                "text": chunk["text"],
                "start": round(chunk["offset"] / 1e7, 3),
                "end": round((chunk["offset"] + chunk["duration"]) / 1e7, 3),
            })
    if not audio:
        raise StageError("Edge TTS 返回空音频", hint="检查网络，或换一个音色重试")
    return bytes(audio), boundaries


def synth_one(text: str, voice: str, rate: str, pitch: str) -> tuple[bytes, list[dict]]:
    """edge-tts 是异步的，统一在这里包一层同步入口。"""
    try:
        return asyncio.run(_synthesize(text, voice, rate, pitch))
    except StageError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StageError(
            f"Edge TTS 合成失败：{type(exc).__name__}: {exc}",
            hint="edge-tts 需要能连到微软的语音服务；网络不通时改用 --voice 重试或退回豆包",
        ) from exc


def merge_preview(sentences: list[dict], out_path: pathlib.Path, work_dir: pathlib.Path) -> None:
    """把整篇口播拼成一个音频，方便从头听一遍。"""
    files = [pathlib.Path(s["audio"]) for s in sentences]
    if not files:
        return
    list_file = out_path.parent / "preview_concat.txt"
    list_file.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in files) + "\n", encoding="utf-8"
    )
    run([ffmpeg_bin(), "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", list_file.resolve().as_posix(), "-c:a", "libmp3lame", "-q:a", "3",
         str(out_path.resolve())], timeout=1800)


def main() -> int:
    ap = argparse.ArgumentParser(description="用 Edge TTS 生成预览配音（免费）")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--script", default="")
    ap.add_argument("--out-dir", default="",
                    help="音频输出目录，默认 <work>/tts/preview。"
                         "**不能和正式配音共用目录**——文件名一样（s5_004.mp3）但内容不同，"
                         "会互相覆盖，把正式配音的音频换成预览音色")
    ap.add_argument("--voice", default=DEFAULT_VOICE, help=f"Edge 音色，默认 {DEFAULT_VOICE}")
    ap.add_argument("--rate", default="+0%", help="语速，如 +10%% / -10%%")
    ap.add_argument("--pitch", default="+0Hz", help="音调，如 +5Hz")
    ap.add_argument("--dry-run", action="store_true", help="只报告要合成哪些句子")
    ap.add_argument("--index", default="tts/index_preview.json",
                    help="预览索引路径（相对 --work-dir）。用单独的文件，"
                         "避免和正式配音的 tts/index.json 互相覆盖")
    ap.add_argument("--no-render", action="store_true",
                    help="只出音频，不渲预览片（默认会渲）")
    ap.add_argument("--preview-out", default="preview.mp4", help="预览片路径（相对 --work-dir）")
    ap.add_argument("--timeline-out", default="timeline_preview.json")
    ap.add_argument("--ass-out", default="subtitles_preview.ass")
    ap.add_argument("--crf", type=int, default=22, help="预览片画质，预览够用就行")
    ap.add_argument("--preset", default="veryfast", help="预览片编码预设，默认 faster 出片更快")
    args = ap.parse_args()

    work_dir = pathlib.Path(args.work_dir).resolve()
    script_path = pathlib.Path(args.script) if args.script else work_dir / "script.json"
    out_dir = pathlib.Path(args.out_dir) if args.out_dir else work_dir / "tts" / "preview"
    index_path = pathlib.Path(args.index)
    if not index_path.is_absolute():
        index_path = work_dir / index_path

    logger = StageLogger(work_dir, "edge_tts")
    script = load_json(script_path)
    sections = script.get("sections") or []
    if not sections:
        raise StageError("脚本里没有 sections", hint="先写 script.json")

    if args.voice not in ZH_VOICES:
        logger.warn(f"音色 {args.voice} 不在常用中文音色列表里，确认名字是否正确：{ZH_VOICES}")

    # 目录归属检查：预览配音和正式配音**必须分目录**。
    # 两边文件名相同（s5_004.mp3）但内容不同，共用一个目录会互相覆盖——
    # 实测踩过：预览音频把正式配音全换掉了，成片配音全错，索引却还指着旧时长。
    final_dir = (work_dir / "tts").resolve()
    if out_dir.resolve() == final_dir:
        raise StageError(
            "预览配音不能写进正式配音目录",
            hint=f"用独立的目录（默认 {work_dir / 'tts' / 'preview'}），"
                 f"否则会覆盖 {final_dir} 里的正式配音",
        )
    other_index = out_dir / "index.json"
    if other_index.exists():
        try:
            other_engine = load_json(other_index).get("engine")
        except Exception:  # noqa: BLE001
            other_engine = None
        if other_engine and other_engine != ENGINE:
            raise StageError(
                f"{other_index} 是 {other_engine} 的索引，不能用预览配音覆盖",
                hint="换一个 --out-dir，或先删掉那个索引",
            )

    out_dir.mkdir(parents=True, exist_ok=True)
    index = {
        "engine": ENGINE,
        "speaker": args.voice,
        "resource_id": ENGINE,
        "model": "",
        "format": "mp3",
        "sample_rate": 24000,
        "sections": {},
    }
    total_chars = 0
    planned = 0
    all_sentences: list[dict] = []

    for sec in sections:
        sid = sec.get("id") or f"s{len(index['sections']) + 1}"
        raw_vo = (sec.get("vo") or "").strip()
        if not raw_vo:
            logger.warn(f"{sid} 没有 vo 文本，跳过")
            index["sections"][sid] = {"duration": 0.0, "sentences": []}
            continue
        vo, notes = doubao.sanitize_text(raw_vo)
        for note in notes:
            logger.warn(f"{sid}: {note}")
        sentences = doubao.split_sentences(vo)
        planned += len(sentences)

        entries = []
        cursor = 0.0
        for i, text in enumerate(sentences, start=1):
            name = f"{sid}_{i:03d}.mp3"
            audio_path = out_dir / name
            if args.dry_run:
                logger.log(f"  {sid}[{i}] 将合成：{text[:40]}")
                total_chars += len(text)
                continue
            audio, boundaries = synth_one(text, args.voice, args.rate, args.pitch)
            audio_path.write_bytes(audio)
            duration = measure_duration(audio_path)
            if duration <= 0:
                duration = round(len(audio) / 32000, 3)

            # Edge 的词边界文本不含标点、粒度也和豆包不同，
            # 先套回我们自己的词元切分（文本与原文严格一致），再交给
            # align_words 用音频里的真实发声区间校正。
            tokens = word_align.tokenize_for_timing(text)
            api_words = word_align.align_tokens_to_boundaries(tokens, text, boundaries)
            if not api_words:
                api_words = word_align.pseudo_api_words(tokens, duration)
            words = word_align.align_words([dict(w) for w in api_words], str(audio_path), duration)
            regions = word_align.detect_regions(str(audio_path), duration)

            entries.append({
                "index": i,
                "text": text,
                "audio": str(audio_path.resolve()),
                "duration": round(duration, 3),
                "start": round(cursor, 3),
                "end": round(cursor + duration, 3),
                "words": [{"word": w["word"], "start": round(cursor + w["start"], 3),
                           "end": round(cursor + w["end"], 3)} for w in words],
                "api_words": api_words,
                "regions": [[round(a, 3), round(b, 3)] for a, b in regions],
            })
            cursor = round(cursor + duration, 3)
            total_chars += len(text)
            logger.log(f"  {sid}[{i}] {duration:.2f}s | {text[:40]}")

        index["sections"][sid] = {"duration": round(cursor, 3), "sentences": entries}
        all_sentences.extend(entries)
        logger.log(f"{sid} 完成：{len(entries)} 句 / {cursor:.2f}s")

    index["total_duration"] = round(sum(s["duration"] for s in index["sections"].values()), 3)
    index["total_chars"] = total_chars

    if args.dry_run:
        logger.log(f"dry-run：将合成 {planned} 句 / {total_chars} 字（未调用 Edge TTS）")
        return 0

    index_path.parent.mkdir(parents=True, exist_ok=True)
    save_json(index_path, index)

    # 整篇拼成一条，方便用户一次听完
    preview = out_dir / "preview_vo.mp3"
    try:
        merge_preview(all_sentences, preview, work_dir)
        logger.log(f"预览音频 → {preview}")
    except StageError as err:
        logger.warn(f"预览音频拼接失败（不影响配音本身）：{err}")

    logger.log(f"预览配音完成：{total_chars} 字 / {index['total_duration']:.1f}s / "
               f"音色 {args.voice}（免费，未调用豆包）")

    if args.no_render:
        logger.log("按要求只出音频，不渲预览片。")
        return 0

    render_preview(work_dir, script_path, index_path, args, logger)
    logger.log("这是试听稿：画面、字幕、节奏都能看，能直接对着改文案和镜头。"
               "用户确认后，再用 doubao_tts.py 合成正式版。")
    return 0


def render_preview(work_dir: pathlib.Path, script_path: pathlib.Path,
                   index_path: pathlib.Path, args, logger) -> None:
    """把预览配音编成一条可直接看的 MP4，并做一次质检。

    预览片和正式片的产物**完全分开**（preview.mp4 / timeline_preview.json /
    subtitles_preview.ass / tts/index_preview.json），
    不会覆盖用户已经做好的正式成片。

    预览片用 veryfast 预设、稍高的 CRF：出片快一倍，文字照样看得清，
    够判断「画面配不配得上口播」就行。
    """
    import subprocess
    here = pathlib.Path(__file__).resolve().parent
    py = sys.executable

    timeline_out = work_dir / args.timeline_out
    ass_out = work_dir / args.ass_out
    preview_out = work_dir / args.preview_out

    def step(title: str, cmd: list[str]) -> None:
        logger.log(f"--- {title} ---")
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(work_dir))
        if proc.returncode != 0:
            raise StageError(
                f"{title}失败：{(proc.stderr or '')[-800:]}",
                hint="预览片没渲出来，但配音已经生成好了，可以先只听音频",
            )

    step("编排时间线", [py, str(here / "build_timeline.py"),
                         "--work-dir", str(work_dir), "--script", str(script_path),
                         "--tts-index", str(index_path), "--out", args.timeline_out,
                         "--ass-out", args.ass_out])
    step("渲染预览片", [py, str(here / "render_video.py"),
                        "--work-dir", str(work_dir), "--timeline", args.timeline_out,
                        "--subtitles", args.ass_out, "--out", args.preview_out,
                        "--crf", str(args.crf), "--preset", args.preset,
                        "--ken-burns", "0"])
    step("质检预览片", [py, str(here / "qc_video.py"),
                        "--work-dir", str(work_dir), "--video", args.preview_out,
                        "--timeline", args.timeline_out])
    logger.log(f"预览片 → {preview_out}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StageError as err:
        print(f"[ERROR] {err}", file=sys.stderr)
        if err.hint:
            print(f"[HINT]  {err.hint}", file=sys.stderr)
        raise SystemExit(2)
