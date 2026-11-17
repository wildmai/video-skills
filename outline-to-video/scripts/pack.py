"""把 skill 打包成 .skill 文件（自动排除本地密钥与运行时产物）。

skill-creator 自带的 package_skill.py 会把目录里所有文件都塞进包里，
包括 .env —— 那会把用户的 API Key 打进分发件。
这个脚本在做同样的事，但会排除敏感文件。

用法：python scripts/pack.py [输出目录]
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
SKILL_ROOT = HERE.parent

EXCLUDE_NAMES = {".env", ".env.example", "__pycache__", ".git", ".DS_Store"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log"}
EXCLUDE_DIRS = {"logs", "__pycache__", ".git"}


def should_skip(path: pathlib.Path) -> bool:
    if path.name in EXCLUDE_NAMES:
        return True
    if path.suffix in EXCLUDE_SUFFIXES:
        return True
    return any(part in EXCLUDE_DIRS for part in path.parts)


def project_root() -> pathlib.Path:
    """skill 通常装在 <项目>/.claude/skills/<name>/，往上找带 .claude 的那一层就是项目根。"""
    for parent in SKILL_ROOT.parents:
        if parent.name == ".claude":
            return parent.parent
    return SKILL_ROOT.parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 outline-to-video skill")
    ap.add_argument("output", nargs="?", default="", help="输出目录，默认放在项目根的 dist/")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.output).resolve() if args.output else project_root() / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{SKILL_ROOT.name}.skill"

    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(SKILL_ROOT.rglob("*")):
            if not path.is_file():
                continue
            if should_skip(path):
                print(f"  跳过 {path.relative_to(SKILL_ROOT)}")
                continue
            zf.write(path, path.relative_to(SKILL_ROOT.parent))
            print(f"  加入 {path.relative_to(SKILL_ROOT.parent)}")
            count += 1

    print(f"\n打包完成：{target}（{count} 个文件，已排除 .env / logs / __pycache__）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
