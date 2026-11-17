#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mgfx.py - motion-graphics overlay generator + ffmpeg compositor.

Produces the graphic overlays for a Chinese screen-recording tutorial
(1920x1080, 30 fps) and burns them onto the raw footage.

Three asset types (RGBA PNG, rendered with Pillow):

  title       1920x1080 chapter title card. Dark / premium. Big chapter
              number, Chinese chapter title, optional one-line subtitle, a
              thin accent rule and a subtle dark gradient so it can be
              cross-dissolved over footage. Deliberately NOT fully opaque -
              the top-right of the frame keeps the footage visible so the
              card can sit over a screen recording for ~1.2 s.
  callout     tight rounded label sized to its text plus a small pointer.
  lowerthird  900x150 lower-third bar: title left, subtitle underneath,
              accent edge.

CLI
---
  python mgfx.py title --num 03 --title "2D 图生 3D" \
      --sub "上传一张图，直接转高精度模型" --out title.png
  python mgfx.py callout --text "缩放 0.620" --out callout.png
  python mgfx.py lowerthird --title "Hi3D MCP" \
      --sub "名称 hi3d · Streamable HTTP" --out lowerthird.png
  python mgfx.py composite --in video.mp4 --overlay title.png \
      --start 12.0 --duration 1.4 --out out.mp4

Palette (dark, modern product-video look)
------------------------------------------
  bg gradient  #0B0F17 -> #131A26
  primary text #F2F5FA
  secondary    #8FA0B8
  accent blue  #4DA3FF      (rules, edges)
  accent amber #FFB454      (chapter number)

Fonts (hard-coded absolute paths, all verified present on this machine)
---------------------------------------------------------------------
  C:\\Windows\\Fonts\\msyhbd.ttc  - Microsoft YaHei Bold   (primary)
  C:\\Windows\\Fonts\\msyh.ttc    - Microsoft YaHei Regular (secondary weight)
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

# --------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------
BG_TOP = (0x0B, 0x0F, 0x17)     # #0B0F17
BG_BOT = (0x13, 0x1A, 0x26)     # #131A26
TEXT = (0xF2, 0xF5, 0xFA)       # #F2F5FA
TEXT_DIM = (0x8F, 0xA0, 0xB8)   # #8FA0B8
ACCENT = (0x4D, 0xA3, 0xFF)     # #4DA3FF  blue
ACCENT2 = (0xFF, 0xB4, 0x54)    # #FFB454  amber
PANEL = (0x10, 0x16, 0x22)      # panel body (callout / lower third)

# --------------------------------------------------------------------------
# Fonts - hard-coded paths
# --------------------------------------------------------------------------
FONT_BOLD_PATH = r"C:\Windows\Fonts\msyhbd.ttc"      # Microsoft YaHei Bold
FONT_REG_PATH = r"C:\Windows\Fonts\msyh.ttc"         # Microsoft YaHei Regular
FONT_BOLD_INDEX = 0
FONT_REG_INDEX = 0

# Fallbacks, only used if the primary file is missing on some machine.
FONT_FALLBACKS = (
    r"C:\Windows\Fonts\Dengb.ttf",            # DengXian Bold
    r"C:\Windows\Fonts\NotoSansSC-VF.ttf",    # Noto Sans SC
)

_FONT_CACHE: dict = {}


def _resolve_font(prefer_bold: bool = True) -> str:
    """Return an existing font path. Primary is always the hard-coded one."""
    primary = FONT_BOLD_PATH if prefer_bold else FONT_REG_PATH
    if Path(primary).exists():
        return primary
    for cand in (FONT_BOLD_PATH, FONT_REG_PATH, *FONT_FALLBACKS):
        if Path(cand).exists():
            return cand
    raise FileNotFoundError(
        "No usable CJK font found. Expected %s (Microsoft YaHei Bold)." % FONT_BOLD_PATH
    )


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    """Cached font loader. bold=True -> YaHei Bold, bold=False -> YaHei Regular."""
    path = _resolve_font(prefer_bold=bold)
    index = FONT_BOLD_INDEX if bold else FONT_REG_INDEX
    if bold and path != FONT_BOLD_PATH:
        index = 0
    key = (path, size, index)
    if key not in _FONT_CACHE:
        _FONT_CACHE[key] = ImageFont.truetype(path, size, index=index)
    return _FONT_CACHE[key]


# --------------------------------------------------------------------------
# Small drawing helpers
# --------------------------------------------------------------------------
def _smoothstep(v: float) -> float:
    v = 0.0 if v < 0 else (1.0 if v > 1 else v)
    return v * v * (3.0 - 2.0 * v)


def _ramp(w: int, h: int, axis: str, start: float, end: float,
          p0: float = 0.0, p1: float = 1.0) -> Image.Image:
    """Smooth 0..1 ramp as an 'L' image. axis 'v' = top->bottom, 'h' = left->right."""
    n = 256
    vals = []
    for i in range(n):
        t = i / (n - 1)
        u = (t - p0) / (p1 - p0) if p1 != p0 else (0.0 if t < p0 else 1.0)
        vals.append(int(round(start + (end - start) * _smoothstep(u))))
    if axis == "v":
        im = Image.new("L", (1, n))
        im.putdata(vals)
    else:
        im = Image.new("L", (n, 1))
        im.putdata(vals)
    return im.resize((w, h), Image.BILINEAR)


def _grad_img(w: int, h: int, c0, c1, horizontal: bool = False) -> Image.Image:
    """Linear two-colour gradient as an RGB image."""
    return _grad_stops(w, h, [(0.0, c0), (1.0, c1)], horizontal)


def _grad_stops(w: int, h: int, stops, horizontal: bool = False) -> Image.Image:
    """Multi-stop gradient.

    Stops are (position 0..1, rgb) and are interpolated in between.  Going
    straight from blue to amber in RGB space passes through grey, so the
    palette is normally given as blue -> blue -> amber to keep it clean.
    """
    stops = sorted(stops, key=lambda s: s[0])
    n = 256
    vals = []
    for i in range(n):
        t = i / (n - 1)
        lo, hi = stops[0], stops[-1]
        for j in range(len(stops) - 1):
            if stops[j][0] <= t <= stops[j + 1][0]:
                lo, hi = stops[j], stops[j + 1]
                break
        span = (hi[0] - lo[0]) or 1.0
        u = (t - lo[0]) / span
        vals.append(tuple(int(round(lo[1][k] + (hi[1][k] - lo[1][k]) * u))
                          for k in range(3)))
    if horizontal:
        im = Image.new("RGB", (n, 1))
        im.putdata(vals)
    else:
        im = Image.new("RGB", (1, n))
        im.putdata(vals)
    return im.resize((w, h), Image.BILINEAR)


def _rounded_mask(size, radius: int, ss: int = 4) -> Image.Image:
    """Anti-aliased rounded-rectangle mask ('L')."""
    w, h = size
    m = Image.new("L", (w * ss, h * ss), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, w * ss - 1, h * ss - 1], radius=radius * ss, fill=255)
    return m.resize((w, h), Image.LANCZOS)


def _drop_shadow(base: Image.Image, mask: Image.Image, offset=(0, 10),
                 blur: int = 18, alpha: int = 120) -> None:
    """Composite a soft dark shadow onto `base` using `mask` (in place)."""
    sh = Image.new("RGBA", base.size, (0, 0, 0, 0))
    a = mask.filter(ImageFilter.GaussianBlur(blur))
    if alpha < 255:
        a = a.point(lambda p: int(p * alpha / 255))
    sh.paste((0, 0, 0, 255), (offset[0], offset[1]), a)
    base.alpha_composite(sh)


def _text_w(draw: ImageDraw.ImageDraw, text: str, f) -> float:
    return draw.textlength(text, font=f)


def _draw_tracked(draw: ImageDraw.ImageDraw, xy, text: str, f, fill,
                  tracking: float = 0.0, anchor: str = "ls") -> float:
    """Draw text with extra letter spacing. Returns the total advance width."""
    x, y = xy
    if tracking <= 0:
        draw.text((x, y), text, font=f, fill=fill, anchor=anchor)
        return draw.textlength(text, font=f)
    widths = [draw.textlength(ch, font=f) for ch in text]
    total = sum(widths) + tracking * max(0, len(text) - 1)
    ha = anchor[0]
    if ha == "m":
        x -= total / 2.0
    elif ha == "r":
        x -= total
    v_anchor = anchor[1]
    for ch, w in zip(text, widths):
        draw.text((x, y), ch, font=f, fill=fill, anchor=ha + v_anchor)
        x += w + tracking
    return total


def _fit_font(draw: ImageDraw.ImageDraw, text: str, size: int, max_w: float,
              bold: bool = True, min_size: int = 28) -> ImageFont.FreeTypeFont:
    """Shrink the font until `text` fits into max_w."""
    f = font(size, bold=bold)
    while size > min_size and draw.textlength(text, font=f) > max_w:
        size -= 2
        f = font(size, bold=bold)
    return f


# --------------------------------------------------------------------------
# Asset 1 - chapter title card (1920x1080)
# --------------------------------------------------------------------------
def render_title(num: str = "03", title: str = "", sub: str = "",
                 size=(1920, 1080)) -> Image.Image:
    """Dark, premium chapter title card.

    Composition (left aligned, lower third of the frame):
        CHAPTER  (tracked kicker, secondary colour)
        03  |  2D 图生 3D        (amber number, thin blue rule, white title)
        ----                      (thin accent bar, blue -> amber)
        上传一张图，直接转高精度模型   (secondary colour)
    plus a huge ghosted chapter number bleeding off the right edge and a
    soft blue glow.  The card never becomes fully opaque: the top-right of
    the frame keeps the footage visible for a cross-dissolve.
    """
    W, H = size
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # --- background: #0B0F17 -> #131A26 vertical gradient ----------------
    grad = _grad_img(W, H, BG_TOP, BG_BOT)
    m_left = _ramp(W, H, "h", 255, 0, 0.0, 0.62)   # 255 at left  -> 0 at 62% W
    m_bot = _ramp(W, H, "v", 0, 255, 0.30, 0.98)    # 0 at 30% H   -> 255 at 98% H
    combined = ImageChops.blend(m_left, m_bot, 0.38)
    # boost the left column so the copy always sits on a dark base
    combined = ImageChops.lighter(combined, m_left.point(lambda p: int(p * 0.45)))
    alpha = combined.point(lambda p: int(round(108 + 132 * p / 255.0)))
    img.paste(grad, (0, 0), alpha)

    # --- soft blue glow behind the copy (depth) --------------------------
    glow = Image.new("L", (W, H), 0)
    gd = ImageDraw.Draw(glow)
    gd.ellipse([-260, 420, 1180, 1120], fill=58)
    glow = glow.filter(ImageFilter.GaussianBlur(160))
    img.paste(Image.new("RGB", (W, H), ACCENT), (0, 0), glow)

    # --- ghosted chapter number bleeding off the right edge --------------
    if num:
        gf = _fit_font(draw, num, 520, 900, bold=True, min_size=180)
        gmask = Image.new("L", (W, H), 0)
        gm = ImageDraw.Draw(gmask)
        gw = draw.textlength(num, font=gf)
        gm.text((W + 60 - gw, 1040), num, font=gf, fill=255, anchor="ls")
        gmask = gmask.filter(ImageFilter.GaussianBlur(2))
        gmask = gmask.point(lambda p: int(p * 0.085))
        img.paste(Image.new("RGB", (W, H), ACCENT2), (0, 0), gmask)

    # --- copy block ------------------------------------------------------
    x0 = 140
    kicker_f = font(26, bold=False)
    num_size = 172
    title_size = 84

    # shrink to fit the available width
    avail = W - x0 - 120
    num_f = font(num_size, bold=True)
    title_f = _fit_font(draw, title, title_size, avail - 520, bold=True, min_size=40) if title else None
    num_w = draw.textlength(num, font=num_f) if num else 0.0
    title_w = draw.textlength(title, font=title_f) if title else 0.0

    rule_x = x0 + num_w + 54
    title_x = rule_x + 44

    # Vertical rhythm is laid out from the bottom edge upwards so the block
    # always clears the frame edge whatever the font metrics are.
    num_asc = num_f.getmetrics()[0] if num else 0
    sub_base = H - 128          # subtitle baseline
    bar_y = sub_base - 62       # accent bar top
    base_y = bar_y - 58         # number + title baseline
    kicker_base = base_y - 52 - num_asc

    # kicker: small blue square + tracked "CHAPTER"
    draw.rounded_rectangle([x0, kicker_base - 13, x0 + 9, kicker_base - 4],
                           radius=2, fill=ACCENT + (255,))
    _draw_tracked(draw, (x0 + 26, kicker_base), "CHAPTER", kicker_f,
                  TEXT_DIM + (255,), tracking=11.0)

    if num:
        draw.text((x0, base_y), num, font=num_f, fill=ACCENT2 + (255,), anchor="ls")

    # thin vertical rule between number and title
    rule_top = base_y - num_asc - 18
    rule_bot = base_y + 20
    rule_h = rule_bot - rule_top
    rm = _ramp(3, rule_h, "v", 40, 255, 0.0, 1.0)
    img.paste(_grad_img(3, rule_h, ACCENT, ACCENT, horizontal=False), (int(rule_x), int(rule_top)), rm)

    if title:
        draw.text((title_x, base_y), title, font=title_f, fill=TEXT + (255,), anchor="ls")

    # thin horizontal accent bar (blue, warming to amber at the tip)
    bar_y = base_y + 56
    bar_w = 420
    bar_mask = _rounded_mask((bar_w, 4), 2)
    img.paste(_grad_stops(bar_w, 4, [(0.0, ACCENT), (0.75, ACCENT), (1.0, ACCENT2)],
                          horizontal=True), (x0, bar_y), bar_mask)

    # subtitle
    if sub:
        sub_f = _fit_font(draw, sub, 32, W - x0 - 140, bold=False, min_size=22)
        draw.text((x0, sub_base), sub, font=sub_f, fill=TEXT_DIM + (255,), anchor="ls")

    return img


# --------------------------------------------------------------------------
# Asset 2 - callout label
# --------------------------------------------------------------------------
def render_callout(text: str, pointer: str = "up", font_size: int = 36,
                   accent=ACCENT) -> Image.Image:
    """Tight rounded label sized to its text + a small pointer/arrow."""
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    f = font(font_size, bold=True)
    lines = text.split("\n")
    widths = [probe.textlength(ln, font=f) for ln in lines]
    asc, desc = f.getmetrics()
    line_h = asc + desc + int(font_size * 0.18)
    text_h = line_h * len(lines)

    pad_x = 34
    pad_y = 22
    pw = 30   # pointer triangle width
    ph = 18   # pointer triangle height

    body_w = int(max(widths)) + pad_x * 2
    body_h = text_h + pad_y * 2

    total_w = body_w
    total_h = body_h + (ph if pointer == "up" else 0) + (ph if pointer == "down" else 0)
    if pointer == "left":
        total_w += pw
    if pointer == "right":
        total_w += pw

    img = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))

    body_x = pw if pointer == "left" else 0
    body_y = ph if pointer == "up" else 0
    if pointer == "down":
        body_y = 0

    radius = 14
    body_mask = _rounded_mask((body_w, body_h), radius)
    full_mask = Image.new("L", (total_w, total_h), 0)
    full_mask.paste(body_mask, (body_x, body_y))

    # soft shadow
    _drop_shadow(img, full_mask, offset=(0, 10), blur=16, alpha=130)

    # panel body
    body = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
    body.paste(PANEL + (232,), (body_x, body_y), body_mask)
    # 2 px accent border
    edge = Image.new("L", (total_w, total_h), 0)
    ed = ImageDraw.Draw(edge)
    ed.rounded_rectangle([body_x + 1, body_y + 1, body_x + body_w - 2, body_y + body_h - 2],
                         radius=radius - 1, outline=255, width=2)
    edge = edge.point(lambda p: int(p * 150 / 255))
    body.paste(Image.new("RGB", (total_w, total_h), accent), (0, 0), edge)
    img.alpha_composite(body)

    # pointer / arrow
    if pointer in ("up", "down", "left", "right"):
        tri = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
        td = ImageDraw.Draw(tri)
        if pointer == "up":
            tip = (body_x + 42, 0)
            p1 = (body_x + 42 - pw // 2, ph)
            p2 = (body_x + 42 + pw // 2, ph)
        elif pointer == "down":
            tip = (body_x + 42, total_h - 1)
            p1 = (body_x + 42 - pw // 2, total_h - ph)
            p2 = (body_x + 42 + pw // 2, total_h - ph)
        elif pointer == "left":
            tip = (0, body_y + 30)
            p1 = (pw, body_y + 30 - pw // 2)
            p2 = (pw, body_y + 30 + pw // 2)
        else:
            tip = (total_w - 1, body_y + 30)
            p1 = (total_w - pw, body_y + 30 - pw // 2)
            p2 = (total_w - pw, body_y + 30 + pw // 2)
        td.polygon([tip, p1, p2], fill=PANEL + (232,), outline=accent + (150,), width=2)
        # the tip overlaps the body so the two read as one shape
        img.alpha_composite(tri)

    # text
    d = ImageDraw.Draw(img)
    tx = body_x + pad_x
    ty = body_y + pad_y
    for i, ln in enumerate(lines):
        d.text((tx, ty + i * line_h), ln, font=f, fill=TEXT + (255,), anchor="la")

    # small accent tick inside the label, left of the text
    accent_mask = Image.new("L", (total_w, total_h), 0)
    ad = ImageDraw.Draw(accent_mask)
    ad.rounded_rectangle([body_x + 14, body_y + pad_y + 4,
                          body_x + 18, body_y + pad_y + text_h - 4],
                         radius=2, fill=255)
    img.paste(Image.new("RGB", (total_w, total_h), accent), (0, 0), accent_mask)

    return img


# --------------------------------------------------------------------------
# Asset 3 - lower third
# --------------------------------------------------------------------------
def render_lowerthird(title: str, sub: str = "", width: int = 900,
                      height: int = 150, accent=ACCENT, accent2=ACCENT2) -> Image.Image:
    """Lower-third bar: title left, subtitle underneath, accent edge."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    radius = 14
    panel_mask = _rounded_mask((width, height), radius)
    _drop_shadow(img, panel_mask, offset=(0, 12), blur=20, alpha=135)

    panel = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    panel.paste(PANEL + (226,), (0, 0), panel_mask)

    # accent edge on the left: blue, warming to amber towards the bottom
    edge_w = 9
    edge_mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(edge_mask).rectangle([0, 0, edge_w, height], fill=255)
    edge_mask = ImageChops.multiply(edge_mask, panel_mask)   # follow the corners
    panel.paste(_grad_stops(width, height,
                            [(0.0, accent), (0.70, accent), (1.0, accent2)]),
                (0, 0), edge_mask)

    # hairline top highlight
    hl = Image.new("L", (width, height), 0)
    ImageDraw.Draw(hl).rounded_rectangle([edge_w, 0, width - 1, height - 1],
                                         radius=radius, outline=255, width=1)
    hl = hl.point(lambda p: int(p * 30 / 255))
    panel.paste(Image.new("RGB", (width, height), (255, 255, 255)), (0, 0), hl)

    img.alpha_composite(panel)

    d = ImageDraw.Draw(img)
    x = edge_w + 38
    t_f = _fit_font(d, title, 42, width - x - 40, bold=True, min_size=24)
    asc, desc = t_f.getmetrics()
    title_base = 40 + asc
    d.text((x, title_base), title, font=t_f, fill=TEXT + (255,), anchor="ls")

    if sub:
        s_f = _fit_font(d, sub, 26, width - x - 40, bold=False, min_size=18)
        s_asc = s_f.getmetrics()[0]
        d.text((x, title_base + 20 + s_asc), sub, font=s_f, fill=TEXT_DIM + (255,), anchor="ls")

    return img


# --------------------------------------------------------------------------
# ffmpeg compositor
# --------------------------------------------------------------------------
def _run(cmd) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def _ffprobe_video_size(path) -> tuple:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)])
    parts = out.decode().strip().split("x")
    return int(parts[0]), int(parts[1])


def _has_audio(path) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    return bool(out.stdout.strip())


def composite(video, overlay, out, start: float = 0.0, duration: float = 2.0,
              x=None, y=None, kind: str = "auto", fade_in: float = 0.35,
              fade_out: float = 0.35, zoom: float = 0.06, fps: int = 30,
              crf: int = 18, preset: str = "medium") -> dict:
    """Burn an overlay PNG onto a video with a smooth fade in/out.

    * full-frame 1920x1080 overlays are treated as title cards: centred and
      given a slow scale-up (zoom) over the visible window.
    * everything else is placed in the bottom-left safe area (--x/--y override).
    Output is H.264 / yuv420p / 30 fps with the original audio stream kept.
    """
    video = Path(video)
    overlay = Path(overlay)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(overlay) as im:
        ow, oh = im.size
    vw, vh = _ffprobe_video_size(video)

    if kind == "auto":
        kind = "title" if (ow, oh) == (1920, 1080) else "lowerthird"

    start = max(0.0, float(start))
    duration = max(0.05, float(duration))
    fade_in = min(fade_in, duration / 2.0)
    fade_out = min(fade_out, duration / 2.0)
    fade_in = max(fade_in, 0.01)
    fade_out = max(fade_out, 0.01)

    # ---- filter graph ----------------------------------------------------
    parts = []
    parts.append("[0:v]fps=%d,format=yuv420p[base]" % fps)

    ov = "[1:v]format=rgba,fps=%d" % fps
    if kind == "title" and zoom > 0:
        s0 = 1.0 - zoom
        expr = "(%f+%f*t/%f)" % (s0, zoom, duration)
        ov += (",scale=w='trunc(iw*%s/2)*2':h='trunc(ih*%s/2)*2':eval=frame" % (expr, expr))
    ov += ",trim=duration=%f,setpts=PTS-STARTPTS" % duration
    ov += ",fade=t=in:st=0:d=%f:alpha=1" % fade_in
    ov += ",fade=t=out:st=%f:d=%f:alpha=1" % (max(0.0, duration - fade_out), fade_out)
    ov += ",setpts=PTS-STARTPTS+%f/TB" % start
    ov += "[ovf]"
    parts.append(ov)

    if x is None or y is None:
        if kind == "title":
            # keep the card centred while it scales
            px = "x=(main_w-overlay_w)/2" if x is None else "x=%d" % int(x)
            py = "y=(main_h-overlay_h)/2" if y is None else "y=%d" % int(y)
        else:
            px = "x=%d" % int(x if x is not None else 96)
            py = "y=%d" % int(y if y is not None else (vh - oh - 96))
    else:
        px = "x=%d" % int(x)
        py = "y=%d" % int(y)

    parts.append(
        "[base][ovf]overlay=%s:%s:eof_action=pass:repeatlast=0[outv]" % (px, py))

    graph = ";".join(parts)

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(video),
           "-framerate", str(fps), "-loop", "1", "-i", str(overlay),
           "-filter_complex", graph, "-map", "[outv]"]

    audio = _has_audio(video)
    if audio:
        cmd += ["-map", "0:a?", "-c:a", "copy"]
    cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-r", str(fps), "-movflags", "+faststart",
            str(out)]

    res = _run(cmd)
    if res.returncode != 0 and audio:
        # retry with a re-encoded audio track (source codec may not fit mp4)
        cmd2 = list(cmd)
        i = cmd2.index("-c:a")
        cmd2[i:i + 2] = ["-c:a", "aac", "-b:a", "192k"]
        res = _run(cmd2)
    if res.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n%s\n%s" % (res.stdout, res.stderr))

    return {"out": str(out), "graph": graph, "kind": kind,
            "overlay_size": (ow, oh), "video_size": (vw, vh)}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _cmd_title(a) -> int:
    img = render_title(num=a.num, title=a.title, sub=a.sub or "")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    img.save(a.out)
    print("WROTE %s  %dx%d" % (a.out, img.width, img.height))
    return 0


def _cmd_callout(a) -> int:
    img = render_callout(text=a.text, pointer=a.pointer, font_size=a.size)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    img.save(a.out)
    print("WROTE %s  %dx%d" % (a.out, img.width, img.height))
    return 0


def _cmd_lowerthird(a) -> int:
    img = render_lowerthird(title=a.title, sub=a.sub or "", width=a.width,
                            height=a.height)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    img.save(a.out)
    print("WROTE %s  %dx%d" % (a.out, img.width, img.height))
    return 0


def _cmd_composite(a) -> int:
    info = composite(a.input, a.overlay, a.out, start=a.start,
                     duration=a.duration, x=a.x, y=a.y, kind=a.kind,
                     fade_in=a.fade_in, fade_out=a.fade_out, zoom=a.zoom,
                     fps=a.fps, crf=a.crf)
    print("WROTE %s" % info["out"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mgfx.py",
        description="Motion-graphics asset generator + ffmpeg compositor "
                    "for a screen-recorded tutorial video.")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("title", help="1920x1080 chapter title card")
    t.add_argument("--num", default="03", help="chapter number, e.g. 03")
    t.add_argument("--title", required=True, help="chapter title (Chinese)")
    t.add_argument("--sub", default="", help="optional one-line subtitle")
    t.add_argument("--out", required=True, help="output PNG")
    t.set_defaults(func=_cmd_title)

    c = sub.add_parser("callout", help="tight label + pointer")
    c.add_argument("--text", required=True, help="label text")
    c.add_argument("--pointer", default="up",
                   choices=["up", "down", "left", "right", "none"])
    c.add_argument("--size", type=int, default=36, help="font size in px")
    c.add_argument("--out", required=True, help="output PNG")
    c.set_defaults(func=_cmd_callout)

    l = sub.add_parser("lowerthird", help="lower-third bar")
    l.add_argument("--title", required=True)
    l.add_argument("--sub", default="")
    l.add_argument("--width", type=int, default=900)
    l.add_argument("--height", type=int, default=150)
    l.add_argument("--out", required=True, help="output PNG")
    l.set_defaults(func=_cmd_lowerthird)

    k = sub.add_parser("composite", help="burn an overlay onto footage")
    k.add_argument("--in", dest="input", required=True, help="source video")
    k.add_argument("--overlay", required=True, help="overlay PNG")
    k.add_argument("--start", type=float, default=0.0, help="start time (s)")
    k.add_argument("--duration", type=float, default=2.0, help="duration (s)")
    k.add_argument("--out", required=True, help="output mp4")
    k.add_argument("--x", type=int, default=None, help="overlay x in pixels")
    k.add_argument("--y", type=int, default=None, help="overlay y in pixels")
    k.add_argument("--kind", default="auto",
                   choices=["auto", "title", "callout", "lowerthird"])
    k.add_argument("--fade-in", dest="fade_in", type=float, default=0.35)
    k.add_argument("--fade-out", dest="fade_out", type=float, default=0.35)
    k.add_argument("--zoom", type=float, default=0.06,
                   help="slow scale-up amount for title cards (0 = off)")
    k.add_argument("--fps", type=int, default=30)
    k.add_argument("--crf", type=int, default=18)
    k.set_defaults(func=_cmd_composite)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
