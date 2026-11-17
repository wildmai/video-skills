"""Index a user-recorded video so the agent can plan cuts against it.

When the recording comes from outside, nobody knows in advance which second
holds which control. This walks the clip, OCRs sampled frames and records
what is on screen, so a shot plan can be written by searching real content
instead of guessing timestamps.

It also flags polluted intervals (console windows, notification bubbles,
splash dialogs) so the plan can avoid them, and lists change points, which
are the natural places to cut.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

from audit import PATTERNS, _ocr_engine

# ffmpeg must never create a console window on the screen being recorded.
CREATE_NO_WINDOW = 0x08000000

TMP = Path("index_frame.png")


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=width,height,r_frame_rate,codec_name",
         "-of", "default=noprint_wrappers=1", str(path)],
        capture_output=True, text=True,
        creationflags=CREATE_NO_WINDOW).stdout
    info = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip()
    return info


def fps_of(text):
    try:
        num, den = text.split("/")
        return float(num) / float(den)
    except Exception:
        return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clip", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--step", type=float, default=2.0)
    ap.add_argument("--scale", type=int, default=1280)
    args = ap.parse_args()

    clip = Path(args.clip)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "_index_frame.png"

    info = probe(clip)
    duration = float(info.get("duration", 0.0))
    engine = _ocr_engine()

    frames = []
    change_points = []
    polluted = []
    prev = None
    prev_tokens = None
    t = 0.0
    while t < duration:
        img = None
        if tmp.exists():
            tmp.unlink()
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t}", "-i", str(clip),
             "-frames:v", "1", "-vf", f"scale={args.scale}:-1", str(tmp)],
            check=False, creationflags=CREATE_NO_WINDOW)
        text = ""
        if tmp.exists():
            gray = np.array(Image.open(tmp).convert("L"))
            try:
                res, _ = engine(str(tmp))
                text = " | ".join(item[1] for item in res)
            except Exception:
                gc_collect()
                time.sleep(2)
            # A cut candidate is either a visible change or a change in what
            # the screen says. Text changes are the stronger signal: a menu
            # opening or a page loading changes the words long before it
            # changes enough pixels to notice.
            #
            # Compare token sets, not raw strings: OCR misreads one glyph
            # between samples ("GL8B" / "GLB") and that must not count as a
            # change. A handful of differing tokens is real movement.
            visual = False
            if prev is not None:
                diff = np.abs(gray.astype(np.int16) - prev)
                visual = float((diff > 30).mean()) > 0.010
            tokens = {t.strip() for t in text.split("|") if t.strip()}
            delta = len(tokens ^ prev_tokens) if prev_tokens is not None else 0
            need = max(8, int(0.15 * max(len(tokens), 1)))
            changed = visual or delta >= need
            prev = gray
            prev_tokens = tokens
        else:
            changed = False
        if changed:
            change_points.append(round(t, 1))
        hit = next((k for k, rx in PATTERNS.items() if rx.search(text)), None)
        if hit:
            if polluted and polluted[-1][1] >= t - args.step:
                polluted[-1][1] = round(t, 1)
            else:
                polluted.append([round(t, 1), round(t, 1)])
        frames.append({"t": round(t, 1), "text": text, "changed": bool(changed),
                       "polluted": hit})
        t += args.step

    payload = {
        "clip": clip.name,
        "duration": round(duration, 2),
        "width": int(info.get("width", 0) or 0),
        "height": int(info.get("height", 0) or 0),
        "fps": round(fps_of(info.get("r_frame_rate", "0/1")), 3),
        "step": args.step,
        "change_points": change_points,
        "polluted": polluted,
        "frames": frames,
    }
    stem = clip.stem
    (out_dir / f"index_{stem}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = []
    for f in frames:
        mark = "*" if f["changed"] else " "
        flag = f"  <<{f['polluted']}" if f["polluted"] else ""
        lines.append(f"[{mark} {f['t']:8.1f}]{flag} {f['text']}")
    header = [
        f"# index of {clip.name}  dur={payload['duration']}s  "
        f"{payload['width']}x{payload['height']} @{payload['fps']}fps  step={args.step}s",
        "# '*' marks a visible change (a natural cut point); '<<label' marks pollution.",
        f"# polluted intervals: {polluted if polluted else 'none'}",
        "",
    ]
    (out_dir / f"index_{stem}.txt").write_text("\n".join(header + lines), encoding="utf-8")
    print(f"indexed {clip.name}: {len(frames)} samples, "
          f"{len(change_points)} change points, {len(polluted)} polluted intervals")
    print(f"  {out_dir / ('index_' + stem + '.txt')}")


def gc_collect():
    import gc
    gc.collect()


if __name__ == "__main__":
    main()
