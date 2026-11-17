"""阶段 2：内容转写。

优先使用视频同名的外挂字幕；没有则用本地 faster-whisper 转写。
产出 work/transcripts/<id>.json 与 <id>.txt，供大纲匹配与脚本撰写使用。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from common import (  # noqa: E402
    StageError,
    StageLogger,
    ensure_dir,
    ffmpeg_bin,
    load_json,
    parse_subtitle_file,
    run,
    save_json,
)


def extract_wav(src: str, dst: str) -> None:
    cmd = [ffmpeg_bin(), "-y", "-v", "error", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", dst]
    run(cmd, timeout=3600)


def transcribe_with_whisper(path: str, model, language: str, logger: StageLogger, vad: bool = True) -> list[dict]:
    try:
        segments_iter, info = model.transcribe(
            path, language=language, vad_filter=vad, beam_size=5,
            condition_on_previous_text=False,
        )
        segments = []
        for seg in segments_iter:
            text = (seg.text or "").strip()
            if not text:
                continue
            segments.append({"start": round(float(seg.start), 3), "end": round(float(seg.end), 3), "text": text})
            logger.log(f"  {seg.start:8.2f} → {seg.end:8.2f} | {text[:60]}")
        if not segments:
            logger.warn("转写结果为空")
        return segments
    except Exception as exc:  # noqa: BLE001
        logger.warn(f"直接转写失败（{exc}），改用抽取的 16k wav 重试")
        with tempfile.TemporaryDirectory() as tmp:
            wav = os.path.join(tmp, "audio.wav")
            extract_wav(path, wav)
            segments_iter, _ = model.transcribe(
                wav, language=language, vad_filter=vad, beam_size=5,
                condition_on_previous_text=False,
            )
            segments = []
            for seg in segments_iter:
                text = (seg.text or "").strip()
                if not text:
                    continue
                segments.append({"start": round(float(seg.start), 3), "end": round(float(seg.end), 3), "text": text})
            return segments


def main() -> int:
    ap = argparse.ArgumentParser(description="转写素材视频内容")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--model", default="small", help="faster-whisper 模型：tiny/base/small/medium/large-v3")
    ap.add_argument("--language", default="zh", help="语言，auto 表示自动识别")
    ap.add_argument("--device", default="auto", help="cpu / cuda / auto")
    ap.add_argument("--no-vad", action="store_true", help="关闭 VAD 静音过滤")
    ap.add_argument("--hf-mirror", action="store_true", help="用 hf-mirror.com 镜像下载模型")
    ap.add_argument("--force", action="store_true", help="已有转写文件也重新生成")
    args = ap.parse_args()

    work_dir = pathlib.Path(args.work_dir)
    logger = StageLogger(work_dir, "transcribe")
    inventory = load_json(work_dir / "inventory.json")

    out_dir = ensure_dir(work_dir / "transcripts")
    model = None
    summary = []

    for item in inventory["videos"]:
        vid = item["id"]
        out_json = out_dir / f"{vid}.json"
        if out_json.exists() and not args.force:
            logger.log(f"{vid} 已存在转写，跳过（--force 可重建）")
            summary.append({"id": vid, "source": "cached", "segments": len(load_json(out_json)["segments"])})
            continue

        if not item["has_audio"]:
            logger.warn(f"{vid} 无音轨，标记为 no_audio")
            payload = {"id": vid, "source": "no_audio", "language": None, "segments": [], "full_text": ""}
            save_json(out_json, payload)
            (out_dir / f"{vid}.txt").write_text("", encoding="utf-8")
            summary.append({"id": vid, "source": "no_audio", "segments": 0})
            continue

        sidecar = item.get("subtitle_sidecar")
        if sidecar and pathlib.Path(sidecar).exists():
            segments = parse_subtitle_file(sidecar)
            source = f"sidecar:{pathlib.Path(sidecar).suffix}"
            logger.log(f"{vid} 使用外挂字幕 {pathlib.Path(sidecar).name}，共 {len(segments)} 条")
        else:
            if model is None:
                if args.hf_mirror:
                    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
                try:
                    from faster_whisper import WhisperModel
                except ImportError as exc:
                    raise StageError(
                        "未安装 faster-whisper",
                        hint="pip install faster-whisper，或为视频准备同名外挂 .srt 字幕",
                    ) from exc
                device = args.device
                if device == "auto":
                    device = "cuda" if _has_cuda() else "cpu"
                compute_type = "float16" if device == "cuda" else "int8"
                logger.log(f"加载 faster-whisper 模型 {args.model}（device={device}, compute={compute_type}）")
                model = WhisperModel(args.model, device=device, compute_type=compute_type)
            logger.log(f"{vid} 开始转写：{item['name']}")
            segments = transcribe_with_whisper(
                item["path"], model, args.language, logger, vad=not args.no_vad
            )
            source = f"whisper:{args.model}"

        full_text = " ".join(s["text"] for s in segments)
        payload = {
            "id": vid,
            "source": source,
            "language": args.language if source.startswith("whisper") else None,
            "duration": item["duration"],
            "segments": segments,
            "full_text": full_text,
        }
        save_json(out_json, payload)
        (out_dir / f"{vid}.txt").write_text(
            "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segments),
            encoding="utf-8",
        )
        summary.append({"id": vid, "source": source, "segments": len(segments)})
        logger.log(f"{vid} 完成：{len(segments)} 条 / {len(full_text)} 字")

    save_json(out_dir / "index.json", summary)
    logger.log(f"全部完成 → {out_dir}")
    return 0


def _has_cuda() -> bool:
    try:
        import ctranslate2  # noqa: F401
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StageError as err:
        print(f"[ERROR] {err}", file=sys.stderr)
        if err.hint:
            print(f"[HINT]  {err.hint}", file=sys.stderr)
        raise SystemExit(2)
