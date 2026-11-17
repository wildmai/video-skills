"""studio - the production control plane for an automated tutorial video.

The user records the screen externally, at native resolution and with their
own tooling. This skill drives the software, then edits what they captured.

    init      scaffold a project
    windows   list visible top-level windows (find the operating target)
    prep      hide foreign windows so they are not captured
    shot      grab one frame, to verify state without asking the user
    aim       record where an action happened, for placing callouts later
    import    register a user-recorded video as a source clip
    index     OCR-scan a source clip so shot timings can be looked up
    voice     split script.md -> Edge TTS -> measured segment durations
    build     cut, add motion graphics, transition and mix into the final cut
    audit     OCR every shipped frame and fail on console / popup / splash
    verify    loudness, decode integrity and duration checks
    status    what is done and what is missing
"""

import argparse
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent

# A console-subsystem child inherits the parent's console when there is one
# and creates a NEW visible window when there is not. CREATE_NO_WINDOW stops
# that second case: the child runs headless no matter who launched it.
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def run_hidden(cmd, capture=False, **kwargs):
    """Run a child process without ever putting a window on screen.

    With capture=False the child writes straight to the parent's console, so
    long jobs still stream their progress - and still never create a window
    of their own.
    """
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
    if capture:
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    return subprocess.run(cmd, **kwargs)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
ntdll = ctypes.windll.ntdll

EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


# ---------------------------------------------------------------- Win32 ----

def window_text(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(hwnd, buf, n + 2)
    return buf.value


def window_class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_pid(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def window_rect(hwnd):
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def enumerate_windows(visible_only=True):
    out = []

    def cb(hwnd, _lparam):
        if visible_only and not user32.IsWindowVisible(hwnd):
            return True
        l, t, r, b = window_rect(hwnd)
        out.append({
            "hwnd": int(hwnd), "pid": window_pid(hwnd), "title": window_text(hwnd),
            "class": window_class(hwnd), "rect": [l, t, r, b], "size": [r - l, b - t],
        })
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return out


def ancestor_pids():
    """PIDs of this process and its ancestors - never hide our own shell."""
    pids = set()
    pid = os.getpid()
    for _ in range(12):
        pids.add(pid)
        parent = _parent_pid(pid)
        if not parent or parent in pids:
            break
        pid = parent
    return pids


def _parent_pid(pid):
    PROCESS_BASIC_INFORMATION = 0

    class PBI(ctypes.Structure):
        _fields_ = [("Reserved1", ctypes.c_void_p), ("PebBaseAddress", ctypes.c_void_p),
                    ("Reserved2", ctypes.c_void_p * 2), ("UniqueProcessId", ctypes.c_void_p),
                    ("InheritedFromUniqueProcessId", ctypes.c_void_p)]

    buf = PBI()
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return 0
    try:
        if ntdll.NtQueryInformationProcess(handle, PROCESS_BASIC_INFORMATION,
                                           ctypes.byref(buf), ctypes.sizeof(buf), None) != 0:
            return 0
        return int(buf.InheritedFromUniqueProcessId or 0)
    finally:
        kernel32.CloseHandle(handle)


def overlaps_capture(rect, region):
    """True when the window really sits on the capture display.

    A window on the neighbouring display can share a few pixels of the
    boundary; treating that as an overlap would hide windows the user is
    still looking at. Require a real intersection, not a sliver.
    """
    rx, ry, rw, rh = region
    l, t, r, b = rect
    iw = min(r, rx + rw) - max(l, rx)
    ih = min(b, ry + rh) - max(t, ry)
    return iw >= 120 and ih >= 90


def parse_region(text):
    if isinstance(text, (list, tuple)):
        return [int(v) for v in text]
    return [int(v) for v in str(text).split(",")]


# --------------------------------------------------------------- project ---

def project_paths(root):
    root = Path(root).resolve()
    return {
        "root": root,
        "raw": root / "raw",
        "build": root / "build",
        "voice": root / "voiceover",
        "script": root / "build" / "script.md",
        "plan": root / "build" / "plan.json",
        "narration": root / "build" / "narration.json",
        "audio_map": root / "build" / "audio_map.json",
        "library": root / "build" / "library.json",
        "aim": root / "build" / "aim.json",
        "final": root / "final.mp4",
    }


def load_plan(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


# ------------------------------------------------------------- commands ----

def cmd_init(args):
    p = project_paths(args.project)
    for key in ("raw", "build", "voice"):
        p[key].mkdir(parents=True, exist_ok=True)
    if not p["script"].exists():
        p["script"].write_text(
            "# 教程脚本\n\n"
            "## ch00 | 开场\n\n"
            "这里写第一段口播。空行结束一段。\n\n"
            "## ch01 | 第一个功能\n\n"
            "这里写第二段口播。\n\n"
            "## ch01 | 第一个功能\n\n"
            "同一章可以有多段，第二段会自动编成 ch01_1。\n",
            encoding="utf-8")
    if not p["plan"].exists():
        plan = {
            "display": {"region": [0, 0, 1920, 1080]},
            "voice": {"voice": "zh-CN-XiaoxiaoNeural", "rate": "+8%"},
            "chapters": [
                {"id": "ch00", "num": "00", "title": "开场", "sub": "",
                 "beats": [{"seg": 0, "src": "recording.mp4", "in": 0, "out": 10}],
                 "callouts": []},
            ],
        }
        p["plan"].write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if not p["library"].exists():
        p["library"].write_text(json.dumps({"clips": []}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"project ready: {p['root']}")
    for k, v in p.items():
        if isinstance(v, Path):
            print(f"  {k:10s} {v}")


def cmd_windows(args):
    region = parse_region(args.region)
    for w in enumerate_windows(visible_only=True):
        if w["size"][0] < 200 or w["size"][1] < 150:
            continue
        tag = "OVERLAPS-CAPTURE" if overlaps_capture(w["rect"], region) else ""
        print(f"hwnd={w['hwnd']:<9} pid={w['pid']:<7} {w['rect']}  "
              f"{w['size'][0]}x{w['size'][1]:<5} {w['class'][:28]:<28} "
              f"{w['title'][:44]:<44} {tag}")


def cmd_prep(args):
    p = project_paths(args.project)
    plan = load_plan(args.plan) if Path(args.plan).exists() else {}
    region = parse_region(plan.get("display", {}).get("region", [0, 0, 1920, 1080]))
    own = set(ancestor_pids()) | {os.getpid()}
    keep_pids = set(own)
    if args.keep_pid:
        keep_pids.add(args.keep_pid)

    hidden, kept = [], []
    for w in enumerate_windows(visible_only=True):
        if not overlaps_capture(w["rect"], region):
            continue
        if w["pid"] in keep_pids:
            # Our own shell. It stays visible unless the caller asked for it
            # to go: on a single-display setup it would otherwise be recorded.
            if not (args.hide_own and w["pid"] in own):
                kept.append(w)
                continue
        if args.keep_title and args.keep_title.lower() in w["title"].lower():
            kept.append(w)
            continue
        user32.ShowWindow(wintypes.HWND(w["hwnd"]), 0)   # SW_HIDE, never close
        hidden.append(w)

    if args.maximize_title:
        for w in enumerate_windows(visible_only=True):
            if args.maximize_title.lower() in w["title"].lower():
                user32.ShowWindow(wintypes.HWND(w["hwnd"]), 3)   # SW_MAXIMIZE
                user32.SetForegroundWindow(wintypes.HWND(w["hwnd"]))
                print(f"maximized hwnd={w['hwnd']} '{w['title']}'")
                break

    for w in hidden:
        print(f"HIDDEN  hwnd={w['hwnd']:<9} pid={w['pid']:<7} {w['size'][0]}x{w['size'][1]:<5} "
              f"{w['class'][:26]:<26} {w['title'][:50]}")
    for w in kept:
        print(f"KEPT    hwnd={w['hwnd']:<9} pid={w['pid']:<7} {w['title'][:60]}")
    print(f"hidden={len(hidden)} kept={len(kept)}")


def cmd_shot(args):
    """One frame, to verify what is on screen."""
    out = Path(args.out)
    region = parse_region(args.region)
    run_hidden([sys.executable, str(SKILL / "scripts" / "capture.py"), "--out", str(out),
                "--region", ",".join(str(v) for v in region)], capture=True)


def cmd_guard(args):
    if args.action == "start":
        cmd_guard_start(args)
    elif args.action == "stop":
        cmd_guard_stop(args)
    else:
        cmd_guard_status(args)


def cmd_guard_start(args):
    """Start the window guard: keep consoles off the captured display."""
    p = project_paths(args.project)
    plan = load_plan(args.plan) if Path(args.plan).exists() else {}
    region = parse_region(plan.get("display", {}).get("region", [0, 0, 1920, 1080]))
    state = p["build"] / "guard_state.json"
    config = p["build"] / "guard.json"
    if state.exists():
        state.unlink()
    payload = {
        "region": region,
        "protect_titles": args.protect or [],
        "protect_classes": args.protect_class or [],
        "own_pids": sorted(ancestor_pids()),
        "hide_own": not args.keep_own,
        "hide_own_gui": args.hide_own_gui,
        "state": str(state),
        "interval": args.interval,
    }
    config.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    python = str(pythonw) if pythonw.is_file() else sys.executable
    proc = subprocess.Popen([python, str(SKILL / "scripts" / "guard.py"), str(config)],
                            close_fds=True, creationflags=flags)
    (p["build"] / "guard.pid").write_text(str(proc.pid), encoding="utf-8")
    time.sleep(1.0)
    print(f"guard started pid={proc.pid} region={region}")
    print(f"  protects: titles={args.protect or []} classes={args.protect_class or []}")
    print(f"  hides own shell: {payload['hide_own']}")
    print("  stop with: studio guard stop --project <项目>")


def cmd_guard_stop(args):
    """Stop the guard and restore every window it hid."""
    p = project_paths(args.project)
    pidfile = p["build"] / "guard.pid"
    if pidfile.exists():
        pid = pidfile.read_text(encoding="utf-8").strip()
        if pid.isdigit():
            subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True,
                       creationflags=CREATE_NO_WINDOW)
        pidfile.unlink()
    state = p["build"] / "guard_state.json"
    restored = 0
    if state.exists():
        try:
            entries = json.loads(state.read_text(encoding="utf-8-sig"))
        except Exception:
            entries = []
        for entry in entries:
            hwnd = wintypes.HWND(entry["hwnd"])
            if user32.IsWindow(hwnd):
                # Put a maximized window back the way it was found.
                user32.ShowWindow(hwnd, 3 if entry.get("maximized") else 5)
                restored += 1
        state.unlink()
    print(f"guard stopped, restored {restored} window(s)")


def cmd_guard_status(args):
    p = project_paths(args.project)
    pidfile = p["build"] / "guard.pid"
    running = False
    if pidfile.exists():
        pid = pidfile.read_text(encoding="utf-8").strip()
        if pid.isdigit():
            running = any(re.search(rf"\b{pid}\b", line)
                          for line in run_hidden(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                                 capture=True).stdout.splitlines())
    state = p["build"] / "guard_state.json"
    hidden = 0
    if state.exists():
        try:
            hidden = len(json.loads(state.read_text(encoding="utf-8-sig")))
        except Exception:
            hidden = 0
    print(f"guard  running={running}  hidden_now={hidden}")
    region = [0, 0, 1920, 1080]
    if Path(args.plan).exists() if args.plan else False:
        region = parse_region(load_plan(args.plan).get("display", {}).get("region", region))
    stragglers = []
    for w in enumerate_windows(visible_only=True):
        if not overlaps_capture(w["rect"], region):
            continue
        cls = w["class"].lower()
        title = w["title"].lower()
        if "console" in cls or "cmd.exe" in cls or "terminal" in cls or "powershell" in title:
            stragglers.append(w)
    for w in stragglers:
        print(f"  STILL VISIBLE hwnd={w['hwnd']} {w['class']} '{w['title'][:50]}'")
    if not stragglers:
        print("  no console-like window on the capture display")


def cmd_aim(args):
    """Record where an action happened, so a callout can be placed there."""
    p = project_paths(args.project)
    p["build"].mkdir(parents=True, exist_ok=True)
    data = json.loads(p["aim"].read_text(encoding="utf-8-sig")) if p["aim"].exists() else []
    data.append({"x": args.x, "y": args.y, "note": args.note or "", "at": args.at or ""})
    p["aim"].write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"aim ({args.x},{args.y}) {args.note or ''}")


def cmd_import(args):
    """Register a user-recorded video as a source clip."""
    p = project_paths(args.project)
    src = Path(args.file)
    if not src.exists():
        sys.exit(f"no such file: {src}")
    p["raw"].mkdir(parents=True, exist_ok=True)
    name = args.name or src.name
    dst = p["raw"] / name
    if src.resolve() != dst.resolve():
        shutil.copy2(src, dst)
    info = run_hidden(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=width,height,r_frame_rate,codec_name", "-of", "default=noprint_wrappers=1", str(dst)], capture=True).stdout
    library = json.loads(p["library"].read_text(encoding="utf-8-sig"))
    entry = {"name": name, "path": str(dst), "probe": info.strip()}
    library["clips"] = [c for c in library["clips"] if c["name"] != name] + [entry]
    p["library"].write_text(json.dumps(library, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"imported {name}\n{info.strip()}")
    print(f"  next: studio index --project {p['root']} --clip {name}")


def cmd_index(args):
    p = project_paths(args.project)
    clip = p["raw"] / args.clip
    if not clip.exists():
        clip = Path(args.clip)
    _run_reported([sys.executable, str(SKILL / "scripts" / "index.py"),
                   "--clip", str(clip), "--out-dir", str(p["build"]),
                   "--step", str(args.step)])


def cmd_voice(args):
    p = project_paths(args.project)
    plan = load_plan(args.plan)
    segs = split_script(p["script"])
    if not segs:
        print("no segments found in script.md", file=sys.stderr)
        sys.exit(1)
    narration = {
        "voice": plan.get("voice", {}).get("voice", "zh-CN-XiaoxiaoNeural"),
        "rate": plan.get("voice", {}).get("rate", "+0%"),
        "segments": [{"id": cid, "text": txt} for cid, txt in segs],
    }
    p["narration"].write_text(json.dumps(narration, ensure_ascii=False, indent=2), encoding="utf-8")
    _run_reported([sys.executable, str(SKILL / "scripts" / "edge_voiceover.py"),
                   str(p["narration"]), str(p["voice"])])

    rows = []
    for seg in narration["segments"]:
        mp3 = p["voice"] / f"{seg['id']}.mp3"
        if not mp3.exists():
            hits = list(p["voice"].glob(f"{seg['id']}-*.mp3"))
            if not hits:
                print(f"missing audio for {seg['id']}", file=sys.stderr)
                sys.exit(1)
            mp3 = hits[0]
        dur = float(run_hidden(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp3)],
            capture=True).stdout.strip())
        rows.append({"id": seg["id"], "text": seg["text"], "mp3": str(mp3), "dur": dur})
    p["audio_map"].write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(r["dur"] for r in rows)
    print(f"voice: {len(rows)} segments, {total:.2f}s total")
    for r in rows:
        rate = len(r["text"]) / r["dur"] if r["dur"] else 0
        flag = "  <-- SLOW, regenerate" if 0 < rate < 3.0 else ""
        print(f"  {r['id']:<22} {r['dur']:6.2f}s  {rate:4.1f} chars/s{flag}")


def split_script(path):
    """Split script.md into (segment_id, text) pairs.

    A '## ' heading starts a chapter. Its form is '## <slug> | <Title>':
    the slug is the ASCII id used by the plan and the TTS manifest, the title
    is what the chapter card displays. '## <slug>' alone is also accepted.

    A blank line ends a spoken paragraph. Fenced code blocks and '>' notes
    are never spoken.
    """
    out = []
    order = []
    paras = {}
    cur = None
    buf = []
    in_fence = False

    def flush():
        nonlocal buf
        if cur and buf:
            txt = " ".join(buf).strip()
            if txt:
                if cur not in paras:
                    paras[cur] = []
                    order.append(cur)
                paras[cur].append(txt)
        buf = []

    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or line.startswith(">"):
            continue
        if line.startswith("## "):
            flush()
            cur = line[3:].strip().split("|")[0].strip()
            continue
        if not line:
            flush()
            continue
        buf.append(line)
    flush()

    # Paragraph n of a chapter becomes the segment id '<slug>', '<slug>_1', ...
    for slug in order:
        for i, text in enumerate(paras[slug]):
            out.append((slug if i == 0 else f"{slug}_{i}", text))
    return out


def _run_reported(cmd):
    """Run a child, echo its output, and fail loudly if it did."""
    result = run_hidden(cmd, capture=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode != 0:
        sys.stderr.write(result.stderr or "")
        sys.exit(result.returncode)


def cmd_build(args):
    _run_reported([sys.executable, str(SKILL / "scripts" / "build.py"),
                   "--project", args.project, "--plan", args.plan])


def cmd_audit(args):
    _run_reported([sys.executable, str(SKILL / "scripts" / "build.py"),
                   "--project", args.project, "--plan", args.plan, "--audit-only"])


def cmd_verify(args):
    video = Path(args.video)
    if not video.exists():
        print(f"no video at {video}", file=sys.stderr)
        sys.exit(1)
    dur = float(run_hidden(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)], capture=True).stdout.strip())
    info = run_hidden(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height,r_frame_rate,sample_rate,channels", "-of", "default=noprint_wrappers=1", str(video)], capture=True).stdout
    loud = run_hidden(["ffmpeg", "-hide_banner", "-nostats", "-i", str(video), "-af", "ebur128=peak=true", "-f", "null", "NUL"], capture=True).stderr
    integ = run_hidden(["ffmpeg", "-v", "error", "-i", str(video), "-f", "null", "NUL"], capture=True)
    # ebur128 prints a per-frame stream and a final summary block. Only the
    # summary is meaningful - the first streamed line reads -70 LUFS silence.
    lufs = re.findall(r"^\s+I:\s*(-?[\d.]+)\s+LUFS", loud, re.M)
    peak = re.findall(r"^\s+Peak:\s*(-?[\d.]+)\s+dBFS", loud, re.M)
    lufs_v = lufs[-1] if lufs else "?"
    peak_v = peak[-1] if peak else "?"

    print(f"file     {video}")
    print(f"duration {dur:.2f}s ({dur/60:.1f} min)")
    print(f"streams  {info.strip()}")
    print(f"loudness I={lufs_v} LUFS  peak={peak_v} dBFS")
    print(f"decode   {'OK' if integ.returncode == 0 and not integ.stderr.strip() else integ.stderr.strip()[:200]}")

    problems = []
    if lufs and not (-17.0 <= float(lufs_v) <= -15.0):
        problems.append(f"integrated loudness {lufs_v} LUFS outside -17..-15")
    if peak and float(peak_v) > -1.0:
        problems.append(f"true peak {peak_v} dBFS above -1.0")
    if integ.returncode != 0 or integ.stderr.strip():
        problems.append("decode errors")
    if problems:
        for p in problems:
            print(f"FAIL     {p}", file=sys.stderr)
        sys.exit(1)
    if args.expect_duration:
        want = float(args.expect_duration)
        drift = dur - want
        print(f"duration drift vs {want:.1f}s: {drift:+.2f}s")
        if abs(drift) > 1.0:
            print("WARNING: duration drift", file=sys.stderr)
            sys.exit(1)


def cmd_status(args):
    p = project_paths(args.project)
    print(f"project  {p['root']}")
    if p["script"].exists():
        segs = split_script(p["script"])
        print(f"script   {len(segs)} paragraphs")
    if p["audio_map"].exists():
        rows = json.loads(p["audio_map"].read_text(encoding="utf-8-sig"))
        print(f"audio    {len(rows)} measured clips, {sum(r['dur'] for r in rows):.2f}s")
    registered = set()
    if p["library"].exists():
        lib = json.loads(p["library"].read_text(encoding="utf-8-sig"))
        registered = {c["name"] for c in lib.get("clips", [])}
        print(f"library  {len(registered)} imported clips")
        for c in lib.get("clips", []):
            idx = p["build"] / f"index_{Path(c['name']).stem}.txt"
            print(f"  {c['name']:<32} {'indexed' if idx.exists() else 'NOT INDEXED'}")
    loose = [c for c in sorted(p["raw"].glob("*.mp4")) if c.name not in registered]
    if loose:
        print(f"raw      {len(loose)} unregistered clips")
        for c in loose:
            dur = run_hidden(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(c)], capture=True).stdout.strip()
            print(f"  {c.name:<32} {dur}s")
    if p["plan"].exists():
        plan = load_plan(p["plan"])
        beats = sum(len(ch.get("beats", [])) for ch in plan.get("chapters", []))
        print(f"plan     {len(plan.get('chapters', []))} chapters, {beats} beats")
        missing = []
        for ch in plan.get("chapters", []):
            for b in ch.get("beats", []):
                if not (p["raw"] / b["src"]).exists():
                    missing.append(b["src"])
        if missing:
            print(f"  MISSING SOURCE CLIPS: {sorted(set(missing))}")
    if p["final"].exists():
        print(f"final    {p['final']} ({p['final'].stat().st_size/1e6:.1f} MB)")


def main():
    ap = argparse.ArgumentParser(prog="studio")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init"); s.add_argument("project"); s.set_defaults(fn=cmd_init)

    s = sub.add_parser("windows"); s.add_argument("--region", default="0,0,1920,1080")
    s.set_defaults(fn=cmd_windows)

    s = sub.add_parser("prep"); s.add_argument("--project", required=True)
    s.add_argument("--plan", default=""); s.add_argument("--keep-pid", type=int, default=0)
    s.add_argument("--keep-title", default=""); s.add_argument("--maximize-title", default="")
    s.add_argument("--hide-own", action="store_true",
                   help="also hide this agent's own shell if it is on the capture display")
    s.set_defaults(fn=cmd_prep)

    s = sub.add_parser("guard"); s.add_argument("action", choices=["start", "stop", "status"])
    s.add_argument("--project", required=True); s.add_argument("--plan", default="")
    s.add_argument("--protect", action="append", default=[])
    s.add_argument("--protect-class", action="append", default=[])
    s.add_argument("--keep-own", action="store_true")
    s.add_argument("--hide-own-gui", action="store_true",
                   help="also hide our own GUI windows, not just consoles")
    s.add_argument("--interval", type=float, default=0.25)
    s.set_defaults(fn=cmd_guard)

    s = sub.add_parser("shot"); s.add_argument("--out", required=True)
    s.add_argument("--region", default="0,0,1920,1080"); s.set_defaults(fn=cmd_shot)

    s = sub.add_parser("aim"); s.add_argument("--project", required=True)
    s.add_argument("x", type=int); s.add_argument("y", type=int)
    s.add_argument("--note", default=""); s.add_argument("--at", default="")
    s.set_defaults(fn=cmd_aim)

    s = sub.add_parser("import"); s.add_argument("--project", required=True)
    s.add_argument("--file", required=True); s.add_argument("--name", default="")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("index"); s.add_argument("--project", required=True)
    s.add_argument("--clip", required=True); s.add_argument("--step", type=float, default=2.0)
    s.set_defaults(fn=cmd_index)

    s = sub.add_parser("voice"); s.add_argument("--project", required=True)
    s.add_argument("--plan", default=""); s.set_defaults(fn=cmd_voice)

    s = sub.add_parser("build"); s.add_argument("--project", required=True)
    s.add_argument("--plan", default=""); s.set_defaults(fn=cmd_build)

    s = sub.add_parser("audit"); s.add_argument("--project", required=True)
    s.add_argument("--plan", default=""); s.set_defaults(fn=cmd_audit)

    s = sub.add_parser("verify"); s.add_argument("--video", required=True)
    s.add_argument("--expect-duration", default=""); s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("status"); s.add_argument("--project", required=True)
    s.set_defaults(fn=cmd_status)

    args = ap.parse_args()
    if args.cmd in ("prep", "voice", "build", "audit") and not args.plan:
        args.plan = str(project_paths(args.project)["plan"])
    if args.cmd == "guard" and not args.plan:
        args.plan = str(project_paths(args.project)["plan"])
    args.fn(args)


if __name__ == "__main__":
    main()
