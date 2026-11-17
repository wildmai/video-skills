"""Keep console windows off the captured display while an operation runs.

The user records externally, so every window that appears on the capture
display ends up in the recording. A terminal flashed by the agent, a stray
`cmd.exe`, an IME candidate window - all of it becomes footage.

This watcher runs detached, polls the window list, and hides anything that
looks like a console (or belongs to the agent's own shell) as soon as it
appears. It remembers what it hid so the windows can be restored afterwards.

It never hides the application being demonstrated: protected titles and
classes are given at start-up.
"""

import ctypes
import json
import sys
import time
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.windll.user32

EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

SW_HIDE = 0
SW_SHOW = 5

CONSOLE_CLASSES = (
    "consolewindowclass", "pythonconsole", "pythonshell", "cmd.exe",
    "conhost", "windows terminal", "windowsterminal", "terminal",
    "xshell", "mobaxterm", "conemu", "putty", "mintty", "gnome-terminal",
    "cygwin", "msys", "bash", "shell", "wt", "windowscmd",
)

CONSOLE_TITLES = (
    "cmd.exe", "powershell", "windows powershell", "terminal", "xshell",
    "mobaxtreme", "mobaxterm", "conemu", "putty", "bash", "wsl", "python",
)

OVERLAY_CLASSES = (
    "ime", "notifyiconoverflowwindow", "windows.ui.core.corewindow",
    "shell_experience_host_window", "applicationframewindow", "tasklistthumbnail",
    "toastwindow", "notifyicon",
)


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


def text_of(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(hwnd, buf, n + 2)
    return buf.value


def class_of(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def pid_of(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def rect_of(hwnd):
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return [r.left, r.top, r.right, r.bottom]


def visible_windows():
    out = []

    def cb(hwnd, _l):
        if user32.IsWindowVisible(hwnd):
            out.append({
                "hwnd": int(hwnd), "pid": pid_of(hwnd), "title": text_of(hwnd),
                "class": class_of(hwnd), "rect": rect_of(hwnd),
            })
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return out


def overlaps(rect, region):
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


def looks_like_console(window):
    cls = window["class"].lower()
    title = window["title"].lower()
    if any(c in cls for c in CONSOLE_CLASSES):
        return True
    if any(t in title for t in CONSOLE_TITLES):
        # A browser tab may legitimately be titled "Python" - only treat a
        # title as a console when the class is not a normal app class.
        return not any(c in cls for c in ("chrome_widgetwin", "mozillawindowclass"))
    return False


def looks_like_overlay(window):
    cls = window["class"].lower()
    return any(c in cls for c in OVERLAY_CLASSES)


def main():
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
    region = config["region"]
    protect_titles = [t.lower() for t in config.get("protect_titles", [])]
    protect_classes = [c.lower() for c in config.get("protect_classes", [])]
    own_pids = set(config.get("own_pids", []))
    hide_own = config.get("hide_own", True)
    hide_own_gui = config.get("hide_own_gui", False)
    state_path = Path(config["state"])
    interval = float(config.get("interval", 0.25))

    hidden = {}
    if state_path.exists():
        try:
            for entry in json.loads(state_path.read_text(encoding="utf-8-sig")):
                hidden[entry["hwnd"]] = entry
        except Exception:
            hidden = {}

    def remember(window):
        hidden[window["hwnd"]] = {
            "hwnd": window["hwnd"], "pid": window["pid"],
            "title": window["title"], "class": window["class"],
            "maximized": bool(user32.IsZoomed(wintypes.HWND(window["hwnd"]))),
        }
        try:
            state_path.write_text(json.dumps(list(hidden.values()), ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        except OSError:
            pass

    while True:
        try:
            for window in visible_windows():
                hwnd = window["hwnd"]
                if hwnd in hidden:
                    continue
                l, t, r, b = window["rect"]
                if (r - l) < 220 or (b - t) < 140:
                    continue
                if not overlaps(window["rect"], region):
                    continue
                cls = window["class"].lower()
                title = window["title"].lower()
                if any(t in title for t in protect_titles):
                    continue
                if any(c in cls for c in protect_classes):
                    continue
                mine = hide_own and window["pid"] in own_pids and (
                    looks_like_console(window) or hide_own_gui)
                if mine or looks_like_console(window) or looks_like_overlay(window):
                    user32.ShowWindow(wintypes.HWND(hwnd), SW_HIDE)
                    remember(window)
        except Exception:
            pass
        time.sleep(interval)


if __name__ == "__main__":
    main()
