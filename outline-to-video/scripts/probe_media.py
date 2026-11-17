"""阶段 1：清点素材。

扫描素材目录下的全部视频，抽取时长/分辨率/帧率/音轨等信息，
并识别同名外挂字幕，产出 work/inventory.json。
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from common import (  # noqa: E402
    StageError,
    StageLogger,
    find_sidecar,
    list_videos,
    media_info,
    rel,
    save_json,
)


def build_id(index: int, stem: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem)[:32]
    return f"v{index:03d}_{safe}" if safe else f"v{index:03d}"


def main() -> int:
    ap = argparse.ArgumentParser(description="清点素材视频并生成 inventory.json")
    ap.add_argument("--media-dir", required=True, help="素材视频目录（递归扫描）")
    ap.add_argument("--work-dir", required=True, help="工作目录，会生成 inventory.json")
    args = ap.parse_args()

    work_dir = pathlib.Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    logger = StageLogger(work_dir, "probe")

    videos = list_videos(args.media_dir)
    logger.log(f"发现 {len(videos)} 个视频文件：{args.media_dir}")

    entries = []
    total_duration = 0.0
    for idx, path in enumerate(videos, start=1):
        vid = build_id(idx, path.stem)
        try:
            info = media_info(path)
        except StageError as exc:
            logger.warn(f"跳过无法解析的文件 {path.name}：{exc}")
            continue
        if not info["has_video"]:
            logger.warn(f"跳过无视频轨的文件：{path.name}")
            continue
        sidecar = find_sidecar(path)
        entry = {
            "id": vid,
            "name": path.name,
            "stem": path.stem,
            "path": str(path.resolve()),
            "rel_path": rel(path.resolve(), work_dir),
            "duration": info["duration"],
            "width": info["width"],
            "height": info["height"],
            "fps": info["fps"],
            "rotation": info["rotation"],
            "has_audio": info["has_audio"],
            "audio_codec": info["audio_codec"],
            "size_mb": info["size_mb"],
            "subtitle_sidecar": str(sidecar.resolve()) if sidecar else None,
        }
        entries.append(entry)
        total_duration += entry["duration"]
        logger.log(
            f"{vid} | {entry['name']} | {entry['duration']:.1f}s | "
            f"{entry['width']}x{entry['height']}@{entry['fps']} | "
            f"音轨={'有' if entry['has_audio'] else '无'} | 外挂字幕={entry['subtitle_sidecar'] or '无'}"
        )

    if not entries:
        raise StageError("没有可用视频", hint="确认素材目录里有可解析的视频文件")

    inventory = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "media_dir": str(pathlib.Path(args.media_dir).expanduser().resolve()),
        "video_count": len(entries),
        "total_duration": round(total_duration, 2),
        "videos": entries,
    }
    out = work_dir / "inventory.json"
    save_json(out, inventory)
    logger.log(f"完成：{len(entries)} 个视频，总时长 {total_duration / 60:.1f} 分钟 → {out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StageError as err:
        print(f"[ERROR] {err}", file=sys.stderr)
        if err.hint:
            print(f"[HINT]  {err.hint}", file=sys.stderr)
        raise SystemExit(2)
