"""阶段 7：时间线编排。

把「脚本 + 配音」对齐成一条可渲染的时间线：
按口播时长重新分配画面时长（画面不够就顺延取材源后续素材，源用尽才定格），
并用豆包返回的字级时间戳生成字幕。
产出 work/timeline.json 与 work/subtitles.ass。
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from common import (  # noqa: E402
    StageError,
    StageLogger,
    load_json,
    save_json,
)

MAX_LINE_WIDTH = 22.0   # 单行最大显示宽度（以一个汉字为一个单位；1080p 下约 1120px）
MIN_LINE_CHARS = 6      # 单行最少字数，低于此值读起来像碎片
MIN_CLIP = 0.6
MAX_TAKE_FROM_SOURCE = 8.0
MIN_CUE_SEC = 0.35      # 字幕行最短显示时长：低于此值会一闪而过，看着像故障
BLACK_MARGIN = 0.12     # 避让黑帧时多退一点：定格克隆的是窗口最后一帧，退少了克隆到黑帧
CLAUSE_END = "，。！？；：、,.!?;:"   # 句读标点：这些位置在音频里会停一下
# 不能起头的后缀（跟在前一个词后面）
BAD_START_CHARS = "的地得们儿"
# 不能留在行尾的介词/连词/副词（后面还要接成分）
BAD_END_CHARS = "把被给让对从向跟和与或在为是就都也还又再很更最会能要想该用以及按据除比朝往"
# 全角字符（汉字与中文标点）都按一个汉字的宽度算
CJK_RE = re.compile(r"[⺀-鿿　-〿＀-￯]")


def _interval_covered(a: float, b: float, cues: list[dict]) -> bool:
    """区间 [a, b] 是否被字幕覆盖（允许多条字幕接力覆盖）。"""
    segs = sorted((c["start"], c["end"]) for c in cues
                  if c["end"] > a - 1e-3 and c["start"] < b + 1e-3)
    reach = a - 1e-3
    for s0, e0 in segs:
        if s0 > reach + 1e-3:
            return False          # 中间有洞
        reach = max(reach, e0)
        if reach >= b - 1e-3:
            return True
    return False


def ensure_region_coverage(cues: list[dict], regions: list[list[float]]) -> list[dict]:
    """保证每一段发声都有字幕覆盖。

    词元时间戳已经按发声区间校正过，正常情况下不会漏；
    这里做最后一道兜底：漏掉的区间由相邻字幕行延伸覆盖，
    绝不让「人说话时字幕消失」这种事漏到成片里。
    """
    if not cues or not regions:
        return cues
    out = [dict(c) for c in cues]
    for a, b in regions:
        if _interval_covered(a, b, out):
            continue
        # 优先延伸「区间内开始的那条字幕」；没有就延伸区间前最后一条
        inner = [c for c in out if c["start"] >= a - 1e-3 and c["start"] < b - 1e-3]
        if inner:
            c = min(inner, key=lambda x: x["start"])
        else:
            prev = [c for c in out if c["end"] <= a + 1e-3]
            if not prev:
                continue
            c = max(prev, key=lambda x: x["end"])
        # 延伸不能压到下一条字幕上，否则两条会同时出现、字叠在一起
        nxt = min((x["start"] for x in out if x["start"] > c["end"] + 1e-3), default=None)
        if nxt is None or nxt >= b:
            c["end"] = round(max(c["end"], b), 3)
    return out


def merge_short_cues(cues: list[dict]) -> list[dict]:
    """把过短的字幕行并进相邻行，消除「一闪而过」的碎片行。

    切分按字数上限走，会在句中留下两三个字的孤儿行（如「遍。」），
    而这几个字的发声只有 0.1~0.2s，字幕会闪一下就消失。
    这里把不足 MIN_CUE_SEC 的行并进前一行（首行则并进后一行）。
    """
    if len(cues) < 2:
        return cues
    merged: list[dict] = []
    for cue in cues:
        dur = cue["end"] - cue["start"]
        if merged and dur < MIN_CUE_SEC:
            merged[-1]["text"] += cue["text"]
            merged[-1]["end"] = cue["end"]
        else:
            merged.append(cue)
    # 首行过短时并进后一行
    if len(merged) >= 2 and (merged[0]["end"] - merged[0]["start"]) < MIN_CUE_SEC:
        merged[1]["text"] = merged[0]["text"] + merged[1]["text"]
        merged[1]["start"] = merged[0]["start"]
        merged.pop(0)
    return merged


def _display_width(text: str) -> float:
    """字幕的显示宽度：一个汉字算 1 个单位。

    拉丁字母只有汉字的一半宽，数字略窄——按字符个数算会把
    「Nano-Banana 有 Pro 和 2 两版」这种其实很短的行误判成超长，
    于是在词中间硬断行。
    """
    w = 0.0
    for ch in text:
        if CJK_RE.match(ch):
            w += 1.0
        elif ch == " ":
            w += 0.3
        elif ch.isascii() and ch.isalnum():
            w += 0.55
        else:
            w += 0.5
    return w


def _line_width(words: list[dict]) -> float:
    return _display_width("".join(w["word"] for w in words))


def _split_clauses(words: list[dict]) -> list[list[dict]]:
    """按句读标点把词元切成小句。

    TTS 在这些标点处会停顿，所以小句边界天然对应音频里的发声区间边界。
    """
    out: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        cur.append(w)
        if w["word"][-1:] in CLAUSE_END:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _break_penalty(words: list[dict], i: int) -> int:
    """在第 i 个词元前断行有多别扭。

    中文没有空格，标点才是天然的断点。硬按字数均分很容易出现
    「……给你 | 写好……」「官方的 | Blender」这种把词拆散的断法，
    观众要拼一下才读得顺。这里给「后缀开头」「介词结尾」的断点加惩罚，
    让切分点尽量落在词与词之间。
    """
    left = words[i - 1]["word"]
    right = words[i]["word"]
    pen = 0
    if right[:1] in BAD_START_CHARS:
        pen += 1        # 「的」「地」这类后缀不能单独起一行
    if left[-1:] in BAD_END_CHARS:
        pen += 1        # 「把」「和」这类介词/连词不能孤零零留在行尾
    return pen


def _split_balanced(words: list[dict], max_chars: float) -> list[list[dict]]:
    """把一组词元切成若干行，行长尽量均衡，断点也尽量自然。

    按字数上限贪心切会在句尾留下「遍。」这种两字碎片行；
    纯按字数均分又会切在词中间。这里以均分为基准，
    在容许的失衡范围内挑一个「不别扭」的断点。
    """
    total = _line_width(words)
    if total <= max_chars or len(words) < 2:
        return [words]
    n_parts = max(1, int(-(-total // max_chars)))   # 向上取整
    cum: list[float] = []
    acc = 0.0
    for w in words:
        acc += _display_width(w["word"])
        cum.append(acc)

    out: list[list[dict]] = []
    start = 0
    n = len(words)
    for k in range(1, n_parts):
        target = total * k / n_parts
        lo = start + 1
        hi = n - (n_parts - k)            # 剩下的行至少各留一个词元
        if hi <= lo:
            hi = lo + 1
        base = cum[start - 1] if start else 0
        cands = []
        for i in range(lo, hi):
            left = cum[i] - base
            rest = total - cum[i]
            if left > max_chars:
                continue
            if rest > (n_parts - k) * max_chars:
                continue
            cands.append(i)
        if not cands:
            cands = list(range(lo, hi))
        slack = max(2.0, target * 0.35)
        cands = [i for i in cands if abs(cum[i] - target) <= slack] or cands
        best = min(cands, key=lambda i: _break_penalty(words, i) * 12.0 + abs(cum[i] - target))
        out.append(words[start:best])
        start = best
    out.append(words[start:])
    return [part for part in out if part]


def _split_text_by_width(text: str, max_width: float) -> list[str]:
    """没有词元时间戳时的兜底：按显示宽度把纯文本切成若干段。"""
    out: list[str] = []
    buf = ""
    for ch in text:
        if _display_width(buf + ch) > max_width and buf:
            out.append(buf)
            buf = ch
        else:
            buf += ch
    if buf:
        out.append(buf)
    return out


def split_cue(words: list[dict], text: str, fallback_start: float, fallback_end: float) -> list[dict]:
    """把一句（可能很长）拆成若干条字幕行。

    时间直接取词元时间戳：词元时间戳已经按音频的发声区间做过校正
    （见 word_align.py），相邻词元首尾相接，正好铺满每一段发声，
    因此字幕在人说话时不会消失，行与行的先后也一定等于口播顺序。
    """
    if not words:
        # 没有词元时间戳（极少见）：按显示宽度等分兜底
        if _display_width(text) <= MAX_LINE_WIDTH:
            return [{"start": fallback_start, "end": fallback_end, "text": text}]
        ratio = _display_width(text)
        cues = []
        cursor = fallback_start
        for piece in _split_text_by_width(text, MAX_LINE_WIDTH):
            dur = (fallback_end - fallback_start) * (_display_width(piece) / ratio)
            cues.append({"start": round(cursor, 3), "end": round(cursor + dur, 3), "text": piece})
            cursor += dur
        return cues

    # 1) 小句 → 行单元（超长的小句再均分）
    units: list[list[dict]] = []
    for clause in _split_clauses(words):
        if _line_width(clause) <= MAX_LINE_WIDTH:
            units.append(clause)
        else:
            units.extend(_split_balanced(clause, MAX_LINE_WIDTH))

    # 2) 相邻单元能拼进一行就拼，避免出现「文生图、」这种半截行
    lines: list[list[dict]] = []
    for unit in units:
        if lines and _line_width(lines[-1]) + _line_width(unit) <= MAX_LINE_WIDTH:
            lines[-1] = lines[-1] + unit
        else:
            lines.append(unit)

    # 3) 过短的行并进相邻行（允许略微超长：宁可有 26 个字，也不要一行只有两个字）
    merged: list[list[dict]] = []
    for line in lines:
        if (merged and _line_width(line) < MIN_LINE_CHARS
                and _line_width(merged[-1]) + _line_width(line) <= MAX_LINE_WIDTH + 4):
            merged[-1] = merged[-1] + line
        else:
            merged.append(line)
    if len(merged) >= 2 and _line_width(merged[0]) < MIN_LINE_CHARS:
        merged[1] = merged[0] + merged[1]
        merged.pop(0)

    cues = []
    for line in merged:
        text_line = "".join(w["word"] for w in line)
        start = line[0]["start"]
        end = line[-1]["end"]
        if end <= start:
            # 整行都是零时长的标点词元，并进上一行
            if cues:
                cues[-1]["end"] = max(cues[-1]["end"], end)
            continue
        cues.append({"start": round(start, 3), "end": round(end, 3), "text": text_line})
    return cues


def write_ass(path: pathlib.Path, cues: list[dict], width: int, height: int, font_name: str) -> None:
    def stamp(t: float) -> str:
        t = max(0.0, t)
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = t % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    font_size = max(28, int(height * 0.048))
    margin_v = int(height * 0.075)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},&H00FFFFFF,&H000000FF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,3.2,1.2,2,{int(width * 0.06)},{int(width * 0.06)},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for cue in cues:
        text = cue["text"].replace("\n", " ").replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{stamp(cue['start'])},{stamp(cue['end'])},Default,,0,0,0,,{text}")
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


def _probe_duration(path: pathlib.Path) -> float:
    """量一个音频文件的真实时长（秒）。"""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True)
        return round(float(proc.stdout.strip()), 3)
    except Exception:  # noqa: BLE001
        return 0.0


def _plan_per_sentence(clips: list[dict], sentences: list[dict],
                       videos: dict[str, dict],
                       black_regions: dict[str, list[list[float]]] | None = None) -> list[dict]:
    """第 i 个画面严格配第 i 句口播：镜头时长 = 该句配音时长。

    这是「画面跟着口播走」的前提。
    早先的做法是把整节的口播时长按各画面自然长度比例摊下去，
    但各句配音长短差别很大（1.5s ~ 13s），画面边界就会和句子边界越走越偏，
    实测最大能偏 6.3s——观众听到「选好目录，一路下一步」时，
    屏幕上已经在放下一段了。

    画面从锚点（clip["start"]，也就是该内容出现在屏幕上的那一刻）开始取，
    源素材往后不够就往前挪，都不够才定格补齐。

    还会避开素材里的黑帧区间（录屏掉帧造成的纯黑）：
    取到黑帧就把窗口截断在黑帧之前，剩下的时长用定格补上——
    宁可画面停住，也不能让成片上闪过一屏黑。
    """
    black_regions = black_regions or {}
    plan = []
    for clip, sent in zip(clips, sentences):
        dur = float(sent["duration"])
        vlen = float(videos[clip["video"]]["duration"])
        anchor = float(clip["start"])
        src_in = min(max(0.0, anchor), max(0.0, vlen - 0.05))
        take = min(dur, max(0.0, vlen - src_in))
        if take < dur:
            # 往后不够，往前挪（内容仍在锚点附近），挪不动才定格
            back = min(src_in, dur - take)
            src_in -= back
            take += back

        # 撞上素材里的黑帧：截断窗口，剩下的定格补齐。
        # 要比黑帧起点再早一点：trim 的 duration 是「保留 pts < D 的帧」，
        # 卡在黑帧起点上会把黑帧本身留在窗口末尾，
        # 定格克隆的就是那张黑帧——等于没避开。
        hit_black = None
        for a, b in black_regions.get(clip["video"], []):
            if b <= src_in or a >= src_in + take:
                continue
            cut = max(0.0, a - src_in - BLACK_MARGIN)
            if cut >= take:
                break
            take = cut
            hit_black = [a, b]
            break

        freeze = max(0.0, dur - take)
        plan.append({"clip": clip, "in": src_in, "dur": take, "freeze": freeze,
                     "sentence": sent.get("text", ""), "black": hit_black})
    return plan


def _plan_proportional(clips: list[dict], sentences: list[dict],
                       videos: dict[str, dict]) -> list[dict]:
    """画面与口播句数不一致时的退化分配（按总时长比例）。"""
    vo_total = sum(float(s["duration"]) for s in sentences)
    naturals = [max(0.0, float(c["end"]) - float(c["start"])) for c in clips]
    natural_total = sum(naturals) or 1.0
    scale = vo_total / natural_total
    plan = []
    for c, nat in zip(clips, naturals):
        vlen = float(videos[c["video"]]["duration"])
        src_in = float(c["start"])
        avail = max(0.0, vlen - src_in)
        dur = max(MIN_CLIP, nat * scale)
        take = min(dur, avail)
        plan.append({"clip": c, "in": src_in, "dur": take,
                     "freeze": max(0.0, dur - take), "sentence": ""})
    return plan


def main() -> int:
    ap = argparse.ArgumentParser(description="生成渲染时间线与字幕")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--script", required=True)
    ap.add_argument("--tts-index", required=True)
    ap.add_argument("--out", default="timeline.json")
    ap.add_argument("--width", type=int, default=0, help="覆盖输出宽度")
    ap.add_argument("--height", type=int, default=0)
    ap.add_argument("--fps", type=int, default=0)
    ap.add_argument("--font", default="Microsoft YaHei")
    ap.add_argument("--no-subtitles", action="store_true")
    ap.add_argument("--ass-out", default="subtitles.ass",
                    help="字幕文件名（相对 --work-dir）。预览片要用别的名字，别覆盖正式字幕")
    ap.add_argument("--black-regions", default="black_regions.json",
                    help="素材黑帧区间表（相对 --work-dir）。没有就不做避让。"
                         "用 scan_black.py 生成")
    args = ap.parse_args()

    work_dir = pathlib.Path(args.work_dir)
    logger = StageLogger(work_dir, "timeline")
    script = load_json(args.script)
    tts = load_json(args.tts_index)
    inventory = load_json(work_dir / "inventory.json")
    videos = {v["id"]: v for v in inventory["videos"]}

    meta = script.get("meta") or {}
    width = args.width or script.get("width") or meta.get("width") or 1920
    height = args.height or script.get("height") or meta.get("height") or 1080
    fps = args.fps or script.get("fps") or meta.get("fps") or 30

    shots: list[dict] = []
    audio: list[dict] = []
    cues: list[dict] = []
    warnings: list[str] = []
    cursor = 0.0

    # 配音文件自检：索引记的时长必须和文件实际时长一致。
    # 不一致说明音频文件被换掉了（最常见的原因是预览配音和正式配音
    # 共用一个目录，文件名相同、内容不同，互相覆盖），
    # 这时候按索引排的时间线会把音画对错位置，必须报出来。
    bad_audio = []
    for sid, sec in (tts.get("sections") or {}).items():
        for s in sec.get("sentences") or []:
            path = pathlib.Path(s.get("audio") or "")
            want = float(s.get("duration") or 0.0)
            if not path.exists():
                bad_audio.append(f"{sid}[{s.get('index')}]: 音频文件不存在 {path}")
                continue
            got = _probe_duration(path)
            if got and abs(got - want) > 0.15:
                idx = s.get("index")
                bad_audio.append(f"{sid}[{idx}]: 索引 {want:.2f}s 但文件 {got:.2f}s"
                                 f"（{path.name}）")
    if bad_audio:
        logger.warn(f"配音文件与索引不一致 {len(bad_audio)} 处，前 5 条：")
        for line in bad_audio[:5]:
            logger.warn(f"  {line}")
        warnings.append(f"配音文件与索引不一致 {len(bad_audio)} 处——"
                        f"音频被覆盖或换过了，时间线不可信，请重新配音后再渲染")

    # 素材里的黑帧（录屏掉帧造成）——镜头碰到就截断并定格补齐，
    # 免得成片上闪过一屏黑。
    black_path = pathlib.Path(args.black_regions)
    if not black_path.is_absolute():
        black_path = work_dir / black_path
    black_regions: dict[str, list[list[float]]] = {}
    if black_path.exists():
        try:
            raw = load_json(black_path).get("videos", {})
            black_regions = {k: v.get("regions", []) for k, v in raw.items()}
            logger.log(f"读入黑帧区间表：{len(black_regions)} 个素材")
        except Exception as exc:  # noqa: BLE001
            logger.warn(f"黑帧区间表读不了（{exc}），本次不做黑帧避让")
    else:
        logger.warn(f"没有 {black_path}，本次不做黑帧避让。"
                    f"跑 scan_black.py 生成后可自动避开素材里的黑闪")

    for sec in script.get("sections", []):
        sid = sec.get("id") or ""
        tts_sec = tts.get("sections", {}).get(sid)
        if not tts_sec or not tts_sec.get("sentences"):
            warnings.append(f"{sid}: 没有配音，已跳过")
            continue
        vo_dur = float(tts_sec.get("duration") or 0.0)
        clips = [c for c in (sec.get("clips") or []) if c.get("video") in videos]
        if not clips:
            warnings.append(f"{sid}: 没有可用画面，已跳过")
            continue

        sentences = tts_sec["sentences"]
        if len(clips) != len(sentences):
            warnings.append(f"{sid}: 画面 {len(clips)} 个 vs 口播 {len(sentences)} 句，"
                            f"无法一一对应，已按总时长比例分配（画面会与口播错位，建议改成一一对应）")
            plan = _plan_proportional(clips, sentences, videos)
        else:
            plan = _plan_per_sentence(clips, sentences, videos, black_regions)

        section_start = cursor
        for i, p in enumerate(plan, start=1):
            c = p["clip"]
            # duration = 这一镜在成片里占的总时长（含定格尾巴），
            # freeze = 其中靠后的定格时长，渲染时用它补帧
            shot_len = p["dur"] + p["freeze"]
            shots.append({
                "id": f"{sid}_{i}",
                "section": sid,
                "video": c["video"],
                "src": videos[c["video"]]["path"],
                "in": round(p["in"], 3),
                "out": round(p["in"] + p["dur"], 3),
                "duration": round(shot_len, 3),
                "freeze": round(p["freeze"], 3),
                "note": c.get("note", ""),
                "muted": c.get("muted", True),
                "start": round(cursor, 3),
                "sentence": p.get("sentence", ""),
            })
            cursor += shot_len
            if p.get("black"):
                a, b = p["black"]
                warnings.append(
                    f"{sid}_{i}: 素材 {c['video'][:4]} 在 {a:.1f}-{b:.1f}s 有黑帧（录屏掉帧），"
                    f"该镜已截断并定格 {p['freeze']:.1f}s。画面停在最后一帧而不是闪黑，"
                    f"更好的做法是换一个锚点")

        audio.append({
            "section": sid,
            "files": [s["audio"] for s in tts_sec["sentences"]],
            "sentences": [
                {
                    "audio": s["audio"],
                    "start": float(s.get("start") or 0.0),
                    "duration": float(s["duration"]),
                }
                for s in tts_sec["sentences"]
            ],
            "start": round(section_start, 3),
            "duration": round(vo_dur, 3),
        })

        for s in tts_sec["sentences"]:
            rel_start = float(s.get("start") or 0.0)
            rel_end = rel_start + float(s["duration"])
            sentence_cues = split_cue(s.get("words") or [], s["text"], rel_start, rel_end)
            # 兜底：词元时间戳已按发声区间校正过，正常情况下每段发声都有字幕；
            # 万一有区间漏了，把前一条字幕行的末端延伸过去，绝不让人说话时字幕消失
            regions = s.get("regions") or []
            if regions:
                abs_regions = [[rel_start + float(a), rel_start + float(b)] for a, b in regions]
                sentence_cues = ensure_region_coverage(sentence_cues, abs_regions)
            # 合并碎片行：消除「一闪而过」的短行
            sentence_cues = merge_short_cues(sentence_cues)
            for cue in sentence_cues:
                cues.append({
                    "start": round(section_start + cue["start"], 3),
                    "end": round(section_start + cue["end"], 3),
                    "text": cue["text"],
                })

    if not shots:
        raise StageError("时间线为空", hint="检查 script.json 的 sections 是否都有 vo 与 clips")

    # 字幕不能重叠：ASS 会把同时出现的两条一起画出来，文字叠成一团
    for i in range(1, len(cues)):
        if cues[i]["start"] < cues[i - 1]["end"]:
            cues[i - 1]["end"] = round(cues[i]["start"], 3)
    # 字幕连续显示：每条字幕一直显示到下一条出现。
    # 这套配音在每个标点后有 0.6~0.9s 的停顿，若让字幕跟着语音断开，
    # 字幕会以约 1Hz 的频率闪烁，看着像故障而不是字幕。
    for i in range(len(cues) - 1):
        if cues[i + 1]["start"] > cues[i]["end"]:
            cues[i]["end"] = cues[i + 1]["start"]

    total = round(cursor, 3)
    if cues:
        cues[0]["start"] = 0.0
        cues[-1]["end"] = max(cues[-1]["end"], total)
    audio_total = round(sum(a["duration"] for a in audio), 3)
    if abs(total - audio_total) > 1.0:
        warnings.append(f"画面总长 {total:.1f}s 与配音总长 {audio_total:.1f}s 相差 {total - audio_total:+.1f}s")

    timeline = {
        "meta": {
            "title": script.get("title", ""),
            "width": width,
            "height": height,
            "fps": fps,
            "duration": total,
            "audio_duration": audio_total,
            "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        },
        "shots": shots,
        "audio": audio,
        "subtitles": [] if args.no_subtitles else cues,
        "warnings": warnings,
    }
    out_path = work_dir / args.out
    save_json(out_path, timeline)

    ass_path = work_dir / args.ass_out
    if not args.no_subtitles:
        write_ass(ass_path, cues, width, height, args.font)
        logger.log(f"字幕 {len(cues)} 条 → {ass_path}")

    for w in warnings:
        logger.warn(w)
    logger.log(f"时间线完成：{len(shots)} 个镜头 / {total:.1f}s → {out_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StageError as err:
        print(f"[ERROR] {err}", file=sys.stderr)
        if err.hint:
            print(f"[HINT]  {err.hint}", file=sys.stderr)
        raise SystemExit(2)
