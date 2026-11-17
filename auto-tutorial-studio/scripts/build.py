"""build - assemble the final cut from a shot plan.

The plan binds every shot to a narration paragraph, so the picture always
matches what the voice says at that moment. Nothing is timed by guesswork:
each beat receives exactly the duration of the paragraph it illustrates, and a
chapter is padded to its narration length so the voice never drifts.

Pipeline: cut -> motion graphics -> cross-dissolve tree -> ducked mix -> audit.
"""

import argparse
import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

SKILL = Path(__file__).resolve().parent.parent
TITLE_HOLD = 2.4
TRANSITION = 0.45
W, H = 1920, 1080


# A console-subsystem child creates a NEW visible window when the parent has
# no console. CREATE_NO_WINDOW prevents that: ffmpeg runs headless, so a
# build never puts a window on the screen being recorded.
CREATE_NO_WINDOW = 0x08000000


def run(cmd, **kw):
    kw["creationflags"] = kw.get("creationflags", 0) | CREATE_NO_WINDOW
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if p.returncode != 0:
        tail = "\n".join((p.stderr or "").strip().splitlines()[-12:])
        raise RuntimeError(f"command failed ({p.returncode}):\n{tail}")
    return p


def ffprobe(path, *entries):
    q = ",".join(entries)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", q, "-of", "default=noprint_wrappers=1", str(path)],
        capture_output=True, text=True,
        creationflags=CREATE_NO_WINDOW).stdout
    return out.strip()


def duration(path):
    try:
        return float(subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            text=True,
            creationflags=CREATE_NO_WINDOW).strip())
    except Exception:
        return 0.0


# ------------------------------------------------------------------ audio --

def read_wav(path):
    with wave.open(str(path), "rb") as w:
        n, ch, sw, sr = w.getnframes(), w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(n)
    if sw != 2:
        raise SystemExit(f"expected 16-bit wav, got {sw*8}-bit: {path}")
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return data.reshape(-1, ch), sr


def write_wav(path, data, sr):
    pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(data.shape[1]); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def duck_bgm(bgm_path, voice_path, out_path, base_gain=0.85, depth=0.62, hop=512):
    """Sidechain-duck the music under the narration, computed in numpy.

    ffmpeg's sidechaincompress does not configure on every build, so the
    envelope is derived here: the narration's short-term RMS drives a
    fast-attack / slow-release gain curve for the music bed.
    """
    bgm, sr = read_wav(bgm_path)
    voice, vsr = read_wav(voice_path)
    if vsr != sr:
        raise SystemExit(f"sample rate mismatch: bgm {sr} vs voice {vsr}")

    mono = voice.mean(axis=1)
    n = len(mono) // hop
    vr = np.sqrt((mono[: n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)
    hop_b = sr // hop
    if len(vr) != len(bgm) // hop_b:
        vr = np.interp(np.linspace(0, len(vr) - 1, len(bgm) // hop_b),
                       np.arange(len(vr)), vr)
    else:
        vr = vr[: len(bgm) // hop_b]

    ref = np.percentile(vr, 90) or 1.0
    drive = np.clip(vr / ref, 0.0, 1.5) / 1.5
    gain = np.empty_like(drive)
    g = 1.0
    for i, d in enumerate(drive):
        target = 1.0 - depth * float(d)
        g = target if target < g else g + (target - g) * 0.55
        gain[i] = g
    gain = np.clip(gain, 1.0 - depth, 1.0)
    k = np.hanning(9); k /= k.sum()
    gain = np.convolve(gain, k, mode="same")
    per_sample = np.interp(np.arange(len(bgm)), np.arange(len(gain)) * hop_b, gain).astype(np.float32)
    write_wav(out_path, bgm * per_sample[:, None] * base_gain, sr)
    return gain.min(), gain.max()


# ----------------------------------------------------------------- video --

def zoom_filter(factor):
    """Static punch-in. crop has no per-frame evaluation on every ffmpeg build."""
    if not factor or factor <= 1.0:
        return None
    z = f"{float(factor):.4f}"
    return (f"crop=w='iw/{z}':h='ih/{z}':x='(iw-iw/{z})/2':y='(ih-ih/{z})/2',scale={W}:{H}")


def cut_range(src, tin, tout, zoom, dest):
    if dest.exists() and dest.stat().st_size > 1000:
        return
    vf = zoom_filter(zoom) or (
        f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-ss", f"{tin}", "-i", str(src), "-t", f"{max(0.05, tout - tin)}",
         "-an", "-vf", vf,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
         "-pix_fmt", "yuv420p", "-r", "30", str(dest)])


def concat_plain(parts, dest):
    listing = dest.parent / "_concat.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(dest)])
    return dest


def xfade2(a, b, crossfade, dest):
    """Cross-dissolve two clips. Two inputs only: an N-input chain exhausts memory."""
    da, db = duration(a), duration(b)
    offset = max(0.05, da - crossfade - 0.20)
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(a), "-i", str(b),
         "-filter_complex",
         f"[0:v]fps=30,format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS[v0];"
         f"[1:v]fps=30,format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS[v1];"
         f"[v0][v1]xfade=transition=fade:duration={crossfade}:offset={offset:.3f}[v]",
         "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
         "-pix_fmt", "yuv420p", "-threads", "2", str(dest)])
    return dest


def concat_xfade(parts, dest, crossfade):
    """Balanced tree of cross-dissolves: at most three inputs alive at once."""
    level, rnd = list(parts), 0
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level) - 1, 2):
            out = dest.parent / f"_xf{rnd}_{i}.mp4"
            xfade2(level[i], level[i + 1], crossfade, out)
            nxt.append(out)
        if len(level) % 2:
            nxt.append(level[-1])
        level, rnd = nxt, rnd + 1
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(level[0]), "-c", "copy", str(dest)])
    return dest


def overlay(video, png, start, dur, dest, x=None, y=None, fade_in=0.35, fade_out=0.35):
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(video), "-i", str(png),
         "-filter_complex",
         f"[1:v]format=rgba,fade=t=in:st=0:d={fade_in}:alpha=1,"
         f"fade=t=out:st={max(0, dur - fade_out):.3f}:d={fade_out}:alpha=1[ov];"
         f"[0:v][ov]overlay=x={0 if x is None else x}:y={0 if y is None else y}:"
         f"enable='between(t,{start},{start + dur})'[v]",
         "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
         "-pix_fmt", "yuv420p", "-r", "30", str(dest)])
    return dest


def group_segments(audio_rows, chapter_id):
    rows = [r for r in audio_rows
            if r["id"] == chapter_id or r["id"].startswith(chapter_id + "_")]
    return sorted(rows, key=lambda r: (r["id"] != chapter_id, r["id"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--audit-only", action="store_true")
    ap.add_argument("--skip-audit", action="store_true")
    args = ap.parse_args()

    root = Path(args.project).resolve()
    raw, build, voice_dir = root / "raw", root / "build", root / "voiceover"
    gfx = build / "gfx"
    gfx.mkdir(parents=True, exist_ok=True)
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8-sig"))
    audio_rows = json.loads((build / "audio_map.json").read_text(encoding="utf-8-sig"))

    # ---- audit the exact footage that ships ------------------------------
    if not args.skip_audit:
        from audit import audit_ranges
        problems = audit_ranges(plan, raw, build / "audit.txt")
        if problems:
            print(f"AUDIT FAILED: {problems} problem(s) - see {build/'audit.txt'}",
                  file=sys.stderr)
            if not args.audit_only:
                sys.exit(2)
        else:
            print("audit: clean")
        if args.audit_only:
            return

    # ---- voice track -----------------------------------------------------
    voice = build / "voice.wav"
    inputs, filters, labels, idx = [], [], [], 0
    for ch in plan["chapters"]:
        for seg in group_segments(audio_rows, ch["id"]):
            inputs += ["-i", seg["mp3"]]
            filters.append(f"[{idx}]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                           f"apad=pad_dur=0.10[a{idx}]")
            labels.append(f"[a{idx}]")
            idx += 1
    filters.append("".join(labels) + f"concat=n={idx}:v=0:a=1[vo]")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + inputs +
        ["-filter_complex", ";".join(filters), "-map", "[vo]", str(voice)])
    voice_dur = duration(voice)
    print(f"voice track {voice_dur:.2f}s")

    # ---- picture ----------------------------------------------------------
    chapter_clips = []
    for ch in plan["chapters"]:
        segs = group_segments(audio_rows, ch["id"])
        seg_dur = [s["dur"] for s in segs]
        seg_start, acc = [], 0.0
        for d in seg_dur:
            seg_start.append(acc); acc += d
        target = acc

        by_seg = {}
        for i, b in enumerate(ch.get("beats", [])):
            by_seg.setdefault(int(b["seg"]), []).append((i, b))

        parts = []
        for si in sorted(by_seg):
            group = by_seg[si]
            budget = seg_dur[si] if si < len(seg_dur) else 0.0
            nat = sum(max(0.05, float(b["out"]) - float(b["in"])) for _i, b in group)
            for i, b in group:
                tin, tout = float(b["in"]), float(b["out"])
                take = min(budget * max(0.05, tout - tin) / nat, tout - tin)
                part = build / f"_c_{ch['id']}_{i}.mp4"
                cut_range(raw / b["src"], tin, tin + take, float(b.get("zoom", 0)), part)
                parts.append(part)

        clip = build / f"_chap_{ch['id']}.mp4"
        concat_plain(parts, clip)
        got = duration(clip)
        if got < target - 0.05:
            padded = build / f"_pad_{ch['id']}.mp4"
            run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-i", str(clip), "-vf", f"tpad=stop_mode=clone:stop_duration={target-got:.3f}",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
                 "-pix_fmt", "yuv420p", str(padded)])
            clip = padded
            got = target
        chapter_clips.append((ch, clip, got, seg_start))
        print(f"  {ch['id']:<18} {got:7.2f}s (narration {target:.2f}s)")

    # ---- motion graphics --------------------------------------------------
    for ch, clip, dur, seg_start in chapter_clips:
        card = gfx / f"title_{ch['id']}.png"
        if not card.exists():
            run([sys.executable, str(SKILL / "scripts" / "mgfx.py"), "title",
                 "--num", str(ch.get("num", "")), "--title", ch["title"],
                 "--sub", ch.get("sub", ""), "--out", str(card)])
        out = build / f"_mg_{ch['id']}.mp4"
        overlay(clip, card, 0.0, TITLE_HOLD, out, fade_in=0.4, fade_out=0.5)
        ch["_clip"] = out
        for j, c in enumerate(ch.get("callouts", [])):
            t_rel = seg_start[int(c["seg"])] + float(c.get("offset", 0.0))
            if t_rel >= dur - 1.0:
                continue
            png = gfx / f"callout_{ch['id']}_{j}.png"
            if not png.exists():
                run([sys.executable, str(SKILL / "scripts" / "mgfx.py"), "callout",
                     "--text", c["text"], "--pointer", c.get("pointer", "up"), "--out", str(png)])
            out2 = build / f"_mg_{ch['id']}_{j}.mp4"
            overlay(ch["_clip"], png, t_rel, min(5.5, dur - t_rel), out2,
                    x=c.get("x", 120), y=c.get("y", 690), fade_in=0.3, fade_out=0.4)
            ch["_clip"] = out2

    # ---- assemble ---------------------------------------------------------
    picture = build / "_picture.mp4"
    concat_xfade([c[0]["_clip"] for c in chapter_clips], picture, TRANSITION)
    pdur = duration(picture)
    print(f"picture {pdur:.2f}s")

    # ---- mix --------------------------------------------------------------
    bgm_src = build / "bgm.wav"
    if not bgm_src.exists():
        alt = build / "bgm_final.wav"
        bgm_src = alt if alt.exists() else None
    ducked = build / "bgm_ducked.wav"
    if bgm_src:
        lo, hi = duck_bgm(bgm_src, voice, ducked)
        print(f"bgm ducked, gain {lo:.2f}..{hi:.2f}")
        chain = (f"[2:a]atrim=0:{pdur:.2f},asetpts=N/SR/TB,"
                 f"afade=t=in:st=0:d=3,afade=t=out:st={max(0,pdur-4):.2f}:d=4[bg];"
                 f"[1:a]aformat=sample_fmts=fltp:channel_layouts=stereo[vo];"
                 f"[vo][bg]amix=inputs=2:duration=first:normalize=0[mix];"
                 f"[mix]loudnorm=I=-16:TP=-1.5:LRA=11[a]")
        audio_inputs = ["-i", str(picture), "-i", str(voice), "-i", str(ducked)]
    else:
        print("WARNING: no bgm.wav found, exporting without music", file=sys.stderr)
        chain = ("[1:a]aformat=sample_fmts=fltp:channel_layouts=stereo[vo];"
                 "[vo]loudnorm=I=-16:TP=-1.5:LRA=11[a]")
        audio_inputs = ["-i", str(picture), "-i", str(voice)]

    final = root / "final.mp4"
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + audio_inputs +
        ["-filter_complex", chain, "-map", "0:v", "-map", "[a]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         "-shortest", str(final)])
    print(f"FINAL {final} ({final.stat().st_size/1e6:.1f} MB, {duration(final):.2f}s)")


if __name__ == "__main__":
    sys.path.insert(0, str(SKILL / "scripts"))
    main()
