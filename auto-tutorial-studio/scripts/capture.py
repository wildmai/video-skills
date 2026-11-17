"""Single-frame screen capture, for verifying state during an operation.

This is NOT the deliverable recorder. The user records externally (OBS,
ShareX, the Windows Game Bar, ...) at native resolution; this helper only
grabs one frame so the agent can confirm what is on screen without asking
the user to describe it.

It deliberately creates no console window and does not touch the mouse.
"""

import argparse
import time
from pathlib import Path

import dxcam
import numpy as np
from PIL import Image


def grab(region, out, retries=12, delay=0.5):
    """Grab one frame, retrying until the screen produces content."""
    rx, ry, rw, rh = region
    cam = dxcam.create(output_idx=0, output_color="RGB", region=(rx, ry, rx + rw, ry + rh))
    try:
        cam.start(region=(rx, ry, rx + rw, ry + rh), target_fps=5, video_mode=True)
        for attempt in range(retries):
            img = cam.get_latest_frame()
            if img is not None:
                frame = np.asarray(img)
                if frame.shape[0] == rh and frame.shape[1] == rw:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    Image.fromarray(frame).save(out)
                    return out
            time.sleep(delay)
    finally:
        cam.release()
    raise SystemExit(f"could not capture {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--region", default="0,0,1920,1080")
    args = ap.parse_args()
    region = [int(v) for v in args.region.split(",")]
    out = Path(args.out)
    grab(region, out)
    print(f"captured {out}")


if __name__ == "__main__":
    main()
