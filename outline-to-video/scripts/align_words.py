"""修复 tts/index.json 里的字级时间戳（豆包 TTS 的中英混排时间戳缺陷）。

阶段 6 的 doubao_tts.py 已经会做这个校正；本脚本用于修复**已经生成**的
index.json（例如脚本改动后想重跑字幕，不必重新配音）。

用法:
  python scripts/align_words.py --work-dir <work>
  python scripts/align_words.py --work-dir <work> --dry-run
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from common import StageLogger, load_json  # noqa: E402
import word_align  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="用音频校正字级时间戳")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--index", default="tts/index.json")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写回")
    args = ap.parse_args()

    work_dir = pathlib.Path(args.work_dir)
    logger = StageLogger(work_dir, "align")
    index_path = work_dir / args.index
    if not index_path.exists():
        raise SystemExit(f"找不到 {index_path}")

    index = load_json(index_path)
    stats = word_align.repair_index(index, None if args.dry_run else index_path)

    logger.log(f"句子 {stats['sentences']}，已校正 {stats['fixed']}")
    logger.log(f"词元间空档：{stats['holes_before']:.1f}s → {stats['holes_after']:.1f}s")
    logger.log(f"闪烁词元：{stats['flash_before']} → {stats['flash_after']}")
    if args.dry_run:
        logger.log("dry-run：未写回")
    else:
        logger.log(f"已写回 {index_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
