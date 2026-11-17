"""扫描素材里的黑帧区间，写进 black_regions.json。

背景
----
录屏软件偶尔会掉帧，素材里会夹着一小段纯黑。
这种黑帧一旦落在镜头里，成片上就是一闪而过的黑屏——
观众会以为播放器出了问题。

qc_video.py 只能在成片上发现问题，而成片已经渲染完了（十分钟起步）。
这个脚本在渲染之前就把问题找出来，并给出「哪个镜头会被波及」。

用法:
  python scripts/scan_black.py --work-dir <work>
  python scripts/scan_black.py --work-dir <work> --force   # 忽略缓存重扫
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from common import StageLogger, load_json, save_json  # noqa: E402

# 3 帧（30fps 下 0.1s）以上的连续黑帧才算问题。
# 单帧偶发黑是编码噪声，不必上报。
MIN_BLACK = 0.1
BLACK_RE = re.compile(r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)")


def scan_video(path: str, duration: float) -> list[list[float]]:
    """返回该视频的黑帧区间列表（秒）。"""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
           "-vf", f"blackdetect=d={MIN_BLACK}:pix_th=0.10", "-an", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = []
    for a, b, d in BLACK_RE.findall(proc.stderr or ""):
        if float(d) >= MIN_BLACK:
            out.append([round(float(a), 3), round(float(b), 3)])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="扫描素材里的黑帧区间")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--out", default="black_regions.json")
    ap.add_argument("--force", action="store_true", help="忽略缓存，全量重扫")
    args = ap.parse_args()

    work_dir = pathlib.Path(args.work_dir).resolve()
    logger = StageLogger(work_dir, "scan_black")
    inventory = load_json(work_dir / "inventory.json")
    out_path = work_dir / args.out

    cache: dict = {}
    if out_path.exists() and not args.force:
        try:
            cache = load_json(out_path).get("videos", {})
        except Exception:  # noqa: BLE001
            cache = {}

    videos = inventory.get("videos") or []
    result: dict[str, list[list[float]]] = {}
    total = 0
    for v in videos:
        vid = v["id"]
        path = v["path"]
        try:
            mtime = pathlib.Path(path).stat().st_mtime
        except OSError:
            continue
        hit = cache.get(vid)
        if hit and abs(float(hit.get("mtime", -1)) - mtime) < 1e-6:
            result[vid] = hit.get("regions", [])
            continue
        t0 = time.time()
        regions = scan_video(path, float(v.get("duration") or 0))
        result[vid] = regions
        cache[vid] = {"mtime": mtime, "regions": regions}
        total += len(regions)
        if regions:
            logger.warn(f"{vid[:4]} {len(regions)} 段黑帧："
                        + "；".join(f"{a:.1f}-{b:.1f}s" for a, b in regions[:4]))
        logger.log(f"  {vid[:4]} 扫完，用时 {time.time() - t0:.0f}s")

    save_json(out_path, {"min_black": MIN_BLACK, "videos": cache})
    logger.log(f"黑帧区间共 {total} 段 → {out_path}")
    logger.log("编排时间线时会自动避开这些区间（改为定格补齐），成片里不会出现黑闪。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
