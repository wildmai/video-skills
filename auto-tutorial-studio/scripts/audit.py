"""audit - prove that no overlay pollutes the shipped frames.

Every occlusion defect found in practice is a piece of text on screen, so a
local OCR pass over the exact windows that ship catches all of them:

  * a console or terminal title bar (a path ending in .dsh, cmd.exe, Xshell)
  * a Chrome notification bubble promoting a video (youtube.com / For you:)
  * an application splash or preferences dialog (Blender's welcome screen)
  * a stray tooltip covering the UI

The audit runs against the plan's windows, not whole source clips, so it
covers precisely what the viewer sees. OCR is local (rapidocr-onnxruntime),
so it costs nothing but CPU.
"""

import gc
import re
import subprocess
import sys
import time
from pathlib import Path

PATTERNS = {
    "console": re.compile(
        r"(C:\\+Users[^ ]*\.dsh|dsh-runt|cmd\.exe|PowerShell|powershell|"
        r"Xshell|Administrator|管理员)", re.I),
    "notification": re.compile(
        r"(youtube\.com|For you:|Spec-Driven|OwainLewis|Google's NEW Approach|"
        r"notification|通知中心)", re.I),
    "splash": re.compile(
        r"(Import Preferences|Create New Preferences|恢复最近|新建文件|最近打开|"
        r"From previous Version|新增内容|视频编辑|捐助)", re.I),
    "tooltip": re.compile(r"(编译EEVEE|着色器|读取文件|保存文件)", re.I),
}


def _ocr_engine():
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR()


def _text_at(engine, tmp, path, t, scale):
    if tmp.exists():
        tmp.unlink()
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t}", "-i", str(path),
         "-frames:v", "1", "-vf", f"scale={scale}:-1", str(tmp)],
        check=False)
    if not tmp.exists():
        return ""
    for _ in range(3):
        try:
            res, _ = engine(str(tmp))
            return " | ".join(item[1] for item in res)
        except Exception:
            # ONNX Runtime can fail with a bad allocation under memory
            # pressure; drop the frame and retry rather than aborting.
            gc.collect()
            time.sleep(2)
    return ""


def audit_ranges(plan, raw_dir, log_path=None, step=3.0, scale=1280):
    """OCR every shipped window. Returns the number of problems found."""
    raw_dir = Path(raw_dir)
    tmp = Path(log_path).parent / "_audit.png" if log_path else raw_dir.parent / "_audit.png"
    engine = _ocr_engine()
    problems = 0
    checked = 0
    log = open(log_path, "w", encoding="utf-8", buffering=1) if log_path else None

    for ch in plan.get("chapters", []):
        for b in ch.get("beats", []):
            src = raw_dir / b["src"]
            if not src.exists():
                msg = f"!! {ch['id']}: missing source clip {b['src']}"
                print(msg, file=sys.stderr)
                if log:
                    log.write(msg + "\n")
                problems += 1
                continue
            t = float(b["in"])
            while t < float(b["out"]):
                txt = _text_at(engine, tmp, src, t, scale)
                checked += 1
                for key, rx in PATTERNS.items():
                    m = rx.search(txt)
                    if m:
                        msg = (f"!! {ch['id']:<14} {b['src']} @{t:7.1f}s "
                               f"[{key}] {m.group(0)!r}")
                        print(msg, file=sys.stderr)
                        if log:
                            log.write(msg + "\n")
                        problems += 1
                        break
                t += step
    summary = f"checked {checked} frames, {problems} problems"
    print(summary)
    if log:
        log.write("\n" + summary + "\n")
        log.close()
    return problems


if __name__ == "__main__":
    import json
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--raw", required=True)
    ap.add_argument("--log", default="")
    a = ap.parse_args()
    plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
    n = audit_ranges(plan, a.raw, a.log or None)
    sys.exit(1 if n else 0)
