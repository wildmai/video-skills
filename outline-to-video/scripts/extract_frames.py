"""阶段 3：画面感知。

为每个素材视频产出：
1. 联系表（contact sheet）：把时间轴上的抽帧拼成大图，每格标注时间戳，供多模态直接看画面。
2. 分镜表（shots.json）：按镜头切点把视频切成 shot 区间，供脚本引用具体片段。
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from common import (  # noqa: E402
    StageError,
    StageLogger,
    ensure_dir,
    ffmpeg_bin,
    load_json,
    run,
    save_json,
)

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\consola.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def find_font() -> str | None:
    for cand in FONT_CANDIDATES:
        if pathlib.Path(cand).exists():
            return cand
    return None


def extract_frames(src: str, out_dir: pathlib.Path, interval: float, cell_width: int) -> list[pathlib.Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("frame_*.jpg"):
        old.unlink()
    cmd = [
        ffmpeg_bin(), "-y", "-v", "error", "-i", src,
        "-vf", f"fps=1/{interval},scale={cell_width}:-2",
        "-q:v", "3", str(out_dir / "frame_%05d.jpg"),
    ]
    run(cmd, timeout=3600)
    return sorted(out_dir.glob("frame_*.jpg"))


def detect_shots(src: str, duration: float, threshold: float, logger: StageLogger) -> list[dict]:
    cmd = [
        ffmpeg_bin(), "-v", "info", "-i", src,
        "-filter:v", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-",
    ]
    proc = run(cmd, check=False, timeout=3600)
    times = []
    for m in re.finditer(r"pts_time:([0-9]+(?:\.[0-9]+)?)", proc.stderr or ""):
        t = float(m.group(1))
        if 0 < t < duration:
            times.append(round(t, 3))
    times = sorted(set(times))
    bounds = [0.0] + times + [round(duration, 3)]
    shots = []
    for i in range(len(bounds) - 1):
        start, end = bounds[i], bounds[i + 1]
        if end - start < 0.2:
            continue
        shots.append({"index": len(shots), "start": start, "end": end, "duration": round(end - start, 3)})
    logger.log(f"检测到 {len(shots)} 个镜头切点")
    return shots


def compose_sheets(frames: list[pathlib.Path], out_dir: pathlib.Path, cols: int, rows: int,
                   cell_width: int, interval: float, font_path: str | None, logger: StageLogger) -> list[dict]:
    from PIL import Image, ImageDraw, ImageFont

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("sheet_*.jpg"):
        old.unlink()

    font = None
    if font_path:
        try:
            font = ImageFont.truetype(font_path, 22)
        except Exception:  # noqa: BLE001
            font = None

    per_sheet = cols * rows
    sheets: list[dict] = []
    label_h = 30
    cell_h = int(cell_width * 9 / 16) + label_h

    for sheet_idx in range(0, len(frames), per_sheet):
        chunk = frames[sheet_idx:sheet_idx + per_sheet]
        canvas = Image.new("RGB", (cols * cell_width, rows * cell_h), (16, 18, 22))
        draw = ImageDraw.Draw(canvas)
        labels = []
        for i, frame_path in enumerate(chunk):
            frame_index = sheet_idx + i
            t = frame_index * interval
            label = f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{t % 60:06.3f}"
            labels.append(label)
            try:
                img = Image.open(frame_path).convert("RGB")
            except Exception:  # noqa: BLE001
                continue
            img.thumbnail((cell_width, cell_h - label_h))
            col, row = i % cols, i // cols
            x = col * cell_width + (cell_width - img.width) // 2
            y = row * cell_h + (cell_h - label_h - img.height) // 2
            canvas.paste(img, (x, y))
            draw.rectangle(
                [col * cell_width, row * cell_h + cell_h - label_h,
                 (col + 1) * cell_width, row * cell_h + cell_h],
                fill=(8, 9, 11),
            )
            draw.text((col * cell_width + 8, row * cell_h + cell_h - label_h + 4), label,
                      fill=(255, 214, 102), font=font)
        out_path = out_dir / f"sheet_{sheet_idx // per_sheet:03d}.jpg"
        canvas.save(out_path, quality=82)
        sheets.append({
            "file": str(out_path),
            "start": round(sheet_idx * interval, 3),
            "end": round((sheet_idx + len(chunk)) * interval, 3),
            "labels": labels,
        })
        logger.log(f"生成联系表 {out_path.name}（{len(chunk)} 格，{sheets[-1]['start']:.1f}s → {sheets[-1]['end']:.1f}s）")
    return sheets


def main() -> int:
    ap = argparse.ArgumentParser(description="抽帧生成联系表并检测镜头切点")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--interval", type=float, default=6.0, help="抽帧间隔（秒）")
    ap.add_argument("--cols", type=int, default=4)
    ap.add_argument("--rows", type=int, default=4)
    ap.add_argument("--cell-width", type=int, default=480)
    ap.add_argument("--scene-threshold", type=float, default=0.35)
    ap.add_argument("--max-sheets", type=int, default=60, help="单视频联系表数量上限，超出则自动放大抽帧间隔")
    ap.add_argument("--skip-shots", action="store_true")
    args = ap.parse_args()

    work_dir = pathlib.Path(args.work_dir)
    logger = StageLogger(work_dir, "frames")
    inventory = load_json(work_dir / "inventory.json")
    font_path = find_font()
    if font_path:
        logger.log(f"标注字体：{font_path}")
    else:
        logger.warn("没找到可用字体，联系表将不带时间戳标签（仍按顺序对应时间）")

    frames_root = ensure_dir(work_dir / "frames")
    index = {}

    for item in inventory["videos"]:
        vid = item["id"]
        out_dir = frames_root / vid
        ensure_dir(out_dir)
        interval = args.interval
        if item["duration"] > 0:
            est = int(item["duration"] // interval) + 1
            per_sheet = args.cols * args.rows
            if est / per_sheet > args.max_sheets:
                new_interval = (item["duration"] / (args.max_sheets * per_sheet)) + interval
                logger.warn(f"{vid} 时长 {item['duration'] / 60:.1f} 分钟，抽帧间隔由 {interval}s 放大到 {new_interval:.1f}s")
                interval = round(new_interval, 2)

        frames = extract_frames(item["path"], out_dir, interval, args.cell_width)
        if not frames:
            logger.warn(f"{vid} 未抽到帧，跳过")
            continue
        sheets = compose_sheets(frames, out_dir, args.cols, args.rows, args.cell_width, interval, font_path, logger)

        shots = []
        if not args.skip_shots:
            shots = detect_shots(item["path"], item["duration"], args.scene_threshold, logger)
        save_json(out_dir / "shots.json", {"id": vid, "interval": interval, "shots": shots})

        index[vid] = {
            "interval": interval,
            "sheets": sheets,
            "shots_file": str(out_dir / "shots.json"),
            "shot_count": len(shots),
        }
        for frame in frames:
            frame.unlink()
        shutil.rmtree(out_dir / "frame_*.jpg", ignore_errors=True)

    save_json(frames_root / "index.json", index)
    logger.log(f"完成 → {frames_root / 'index.json'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StageError as err:
        print(f"[ERROR] {err}", file=sys.stderr)
        if err.hint:
            print(f"[HINT]  {err.hint}", file=sys.stderr)
        raise SystemExit(2)
