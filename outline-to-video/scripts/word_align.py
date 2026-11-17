"""字级时间戳修复：用音频本身把豆包 TTS 的 word 时间戳校正回正轨。

背景（实测缺陷）
----------------
豆包 TTS 对「中英混排」文本返回的 word 时间戳不可靠。实测
「高级功能四块：Agent、MCP、3D 游戏开发、Blender 编辑。」：

    '高'      0.415 ~ 0.575     ← 汉字：时间准确
    '块：'    1.125 ~ 1.155
    'Agent、' 2.365 ~ 2.395     ← 拉丁词元：时长被压成 30ms
    'MCP、'   4.215 ~ 4.245     ← 真正的发声时间被丢进了词元间的空洞

规律：凡含拉丁字母/数字的词元，startTime 落在自己发声区间的**末尾**，
而 startTime~endTime 只有 30~40ms；本该属于它的时间变成了前后空洞。
更糟的是这个错误时间会把词元指到**错误的发声区间**——
直接「就近归属」会把 MCP 排到「游戏开发」那一段里去。

后果（实测成片）
----------------
字幕按这些时间戳切分后出现两类可见缺陷：
  1) 说话过程中字幕消失：203 处空档，累计 148s；
  2) 30~90ms 的闪烁字幕条：20 处。

做法
----
以**音频**为基准重新排布词元时间，而不是相信 API 的绝对时间：

  1. 用 ffmpeg silencedetect 找出句子里的发声区间（region）；
  2. 把「词元 → 发声区间」当成序列对齐问题用 DP 求解：
       - 汉字词元有可信的 API 时间，用它与区间中心的距离做代价；
       - 拉丁词元没有可信信息，代价为 0；
       - 约束：每个区间至少分到一个词元，且词元顺序与区间顺序一致。
     DP 的硬约束「每区间至少一个词元」正是把 Agent/MCP/3D 拨回正确区间的关键；
  3. 区间内按时长摊分：汉字用 API 时长（比例可信），拉丁词元按字符权重估算；
  4. 纯标点词元不单独占时间，紧贴前一个词元。

对齐后每个词元都落在真实的发声区间上，字幕全程覆盖。
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess

# 词元权重：一个汉字约 1 个音节，一个拉丁字母约半个音节
CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
LATIN_RE = re.compile(r"[A-Za-z]")
DIGIT_RE = re.compile(r"[0-9]")
PUNCT_ONLY_RE = re.compile(
    r"^[\s，。、；：？！“”‘’（）《》〈〉「」『』【】…—～·,.;:?!\-–—_/\\|@#$%^&*+=~`\"'()\[\]{}<>]+$"
)

SILENCE_DB = "-35dB"    # 判定静音的阈值
SILENCE_MIN = 0.06     # 短于 60ms 的静音并入相邻发声区间
TRUST_DUR = 0.08       # 词元时长 >= 80ms 才认为 API 时长可信


PUNCT_CHARS = "，。！？；：、,.!?;:、，！？；："
LATIN_RUN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+/-]*")


def tokenize_for_timing(text: str) -> list[str]:
    """把文本切成词元：汉字逐字、拉丁词整体、标点跟在前一个词元后。

    豆包返回的 word 时间戳就是这种粒度（'这' / '是' / 'Hi3D，'）。
    Edge TTS 对中文不返回词级边界（只有一句一个 SentenceBoundary），
    需要自己按同样粒度切，再按字符位置摊出「伪时间戳」，
    最后交给 align_words 用音频里的真实发声区间校正。
    """
    tokens: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if LATIN_RUN_RE.match(ch):
            j = i
            while j < n and LATIN_RUN_RE.match(text[j]):
                j += 1
            # 拉丁词后面紧跟的中文标点归到这个词元（和豆包的切法一致）
            if j < n and text[j] in PUNCT_CHARS:
                j += 1
            tokens.append(text[i:j])
            i = j
            continue
        if ch in PUNCT_CHARS and tokens:
            tokens[-1] += ch
            i += 1
            continue
        tokens.append(ch)
        i += 1
    return tokens


def pseudo_api_words(tokens: list[str], duration: float) -> list[dict]:
    """按字符权重把时长摊到各词元上，得到「等比例」的伪时间戳。

    只反映字符位置，不反映真实语速；
    align_words 会用音频里的发声区间把它校正到真实位置。
    """
    weights = [weight(t) for t in tokens]
    total = sum(weights) or 1.0
    out: list[dict] = []
    cursor = 0.0
    for tok, w in zip(tokens, weights):
        d = duration * w / total
        out.append({"word": tok, "start": round(cursor, 3), "end": round(cursor + d, 3)})
        cursor += d
    return out


def align_tokens_to_boundaries(tokens: list[str], text: str,
                               boundaries: list[dict]) -> list[dict]:
    """把 Edge TTS 的词边界时间戳套回我们自己的词元切分上。

    Edge 的边界有两个特点：
      - 文本里**不含标点**（"遍" 而不是 "遍。"）；
      - 粒度与豆包不同（"Hi3D" 会拆成 "Hi" / "3" / "D"，"接下来" 反而是一个词元）。
    直接拿它当字幕文本会丢标点、也和原文对不上。

    做法：先在原文里定位每个边界覆盖的字符区间，
    再把我们自己的词元（逐字/整词切分，文本与原文严格一致）
    映射到这些区间上——起点取第一个相交边界的起点，
    终点取最后一个相交边界的终点。纯标点词元没有边界，
    就贴住前一个词元的终点。

    这样既有真实语速的时间戳，文本又和原文一字不差。
    """
    if not tokens or not boundaries:
        return []

    # 1) 在原文里定位每个边界的字符区间
    spans: list[tuple[int, int, float, float]] = []
    cursor = 0
    for b in boundaries:
        frag = str(b.get("text") or "")
        if not frag:
            continue
        pos = text.find(frag, cursor)
        if pos < 0:                      # 边界文本在原文里找不到（服务做了归一化）
            continue
        spans.append((pos, pos + len(frag), float(b["start"]), float(b["end"])))
        cursor = pos + len(frag)

    # 2) 定位我们自己每个词元的字符区间
    tok_spans: list[tuple[int, int]] = []
    cursor = 0
    for tok in tokens:
        pos = text.find(tok, cursor)
        if pos < 0:
            pos = cursor
        tok_spans.append((pos, pos + len(tok)))
        cursor = pos + len(tok)

    # 3) 映射
    out: list[dict] = []
    for i, tok in enumerate(tokens):
        a, b = tok_spans[i]
        hit = [s for s in spans if s[0] < b and s[1] > a]
        if hit:
            start = hit[0][2]
            end = hit[-1][3]
        else:
            # 没有边界覆盖：纯标点或服务漏掉的片段，贴住前一个词元
            prev_end = out[-1]["end"] if out else 0.0
            nxt = next((s for s in spans if s[0] >= b), None)
            start = prev_end
            end = nxt[2] if nxt else prev_end
        out.append({"word": tok, "start": round(start, 3), "end": round(end, 3)})
    return out


def has_latin(text: str) -> bool:
    return bool(LATIN_RE.search(text) or DIGIT_RE.search(text))


def weight(text: str) -> float:
    """发音时长权重。"""
    cjk = len(CJK_RE.findall(text))
    latin = len(LATIN_RE.findall(text))
    digit = len(DIGIT_RE.findall(text))
    return max(0.15, cjk + 0.5 * latin + 0.8 * digit)


def detect_regions(audio: str, duration: float) -> list[tuple[float, float]]:
    """用 silencedetect 找出发声区间（静音的补集）。"""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", audio,
         "-af", f"silencedetect=n={SILENCE_DB}:d={SILENCE_MIN}", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    stderr = proc.stderr or ""
    starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", stderr)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", stderr)]
    silences = list(zip(starts, ends))

    regions: list[list[float]] = []
    cursor = 0.0
    for a, b in silences:
        if a > cursor:
            regions.append([cursor, a])
        cursor = max(cursor, b)
    if cursor < duration:
        regions.append([cursor, duration])

    # 合并相邻区间（很短的静音不算断句）
    merged: list[list[float]] = []
    for r in regions:
        if merged and r[0] - merged[-1][1] < SILENCE_MIN:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    return [(a, b) for a, b in merged]


def _nearest_region(mid: float, regions: list[tuple[float, float]]) -> int:
    """离 mid 最近的区间序号。"""
    best, best_d = 0, float("inf")
    for i, (a, b) in enumerate(regions):
        if a <= mid <= b:
            return i
        d = min(abs(mid - a), abs(mid - b))
        if d < best_d:
            best, best_d = i, d
    return best


def _pava(values: list[float]) -> list[float]:
    """保序回归：把序列改成单调不减，且尽量贴近原值。

    朗读顺序与发声区间顺序必然一致，所以词元归属的区间序号必须单调；
    但 API 时间有误差，直接取整会出现顺序颠倒，用保序回归做最小代价修正。
    """
    blocks: list[list] = []  # [sum, count, first_index]
    for i, v in enumerate(values):
        blocks.append([v, 1, i])
        while len(blocks) >= 2:
            prev, cur = blocks[-2], blocks[-1]
            if prev[0] / prev[1] <= cur[0] / cur[1]:
                break
            blocks[-2:] = [[prev[0] + cur[0], prev[2] + cur[2], prev[3]]]
    out = [0.0] * len(values)
    for total, count, first in blocks:
        for j in range(first, first + count):
            out[j] = total / count
    return out


def _segment_align(words: list[dict], regions: list[tuple[float, float]]) -> list[int]:
    """把词元序列对齐到发声区间序列，返回每个词元所属的区间序号。

    思路：
      1. 汉字词元的 API 时间可信 → 按 API 中点就近锚定到发声区间，
         再用保序回归保证区间序号单调不减（朗读顺序与区间顺序必然一致）；
      2. 拉丁词元的 API 时间完全不可信 → 它们夹在两个可信词元之间，
         就平均分配到这两个锚点之间的空区间里。
         典型场景「1:1、16:9、9:16、4:3、3:4」：10 个词元、5 个区间，
         平均分配正好每个区间 2 个词元，与朗读的 5 个比例一一对应。
    """
    n_words, n_regions = len(words), len(regions)
    if n_regions == 0:
        return [0] * n_words

    centers = [(a + b) / 2 for a, b in regions]

    def trusted(i: int) -> bool:
        w = words[i]
        return not (has_latin(w["word"]) or (w["api_end"] - w["api_start"]) < TRUST_DUR)

    anchor_idx = [i for i in range(n_words) if trusted(i)]
    assign = [0] * n_words

    if anchor_idx:
        # 1) 锚点：按 API 中点就近归属 + 保序回归
        mids = [(words[i]["api_start"] + words[i]["api_end"]) / 2 for i in anchor_idx]
        regs = [float(_nearest_region(m, regions)) for m in mids]
        fitted = _pava(regs)
        for slot, i in enumerate(anchor_idx):
            assign[i] = int(round(fitted[slot]))

        # 2) 不可信词元：在左右锚点之间的空区间里平均分配
        run_start = 0
        while run_start < n_words:
            if run_start in anchor_idx:
                run_start += 1
                continue
            run_end = run_start
            while run_end < n_words and run_end not in anchor_idx:
                run_end += 1
            left = assign[run_start - 1] if run_start > 0 else -1
            right = assign[run_end] if run_end < n_words else n_regions
            slots = list(range(left + 1, right))
            if not slots:
                slots = [max(0, min(left, n_regions - 1))]
            for k, i in enumerate(range(run_start, run_end)):
                assign[i] = slots[min(k * len(slots) // max(1, (run_end - run_start)),
                                      len(slots) - 1)]
            run_start = run_end
    else:
        # 全是不可信词元：按数量在区间间均分
        for k, i in enumerate(range(n_words)):
            assign[i] = min(n_regions - 1, k * n_regions // n_words)
    return _fill_empty_regions(assign, words, n_regions)


def _fill_empty_regions(assign: list[int], words: list[dict], n_regions: int) -> list[int]:
    """保证每个发声区间至少分到一个词元。

    区间是音频里真实有声音的一段。某个区间没分到词元，
    字幕在这段就没有内容——人说话时字幕会凭空消失。
    修法：把相邻区间的词元挪一个过来，优先挪「不可信」的
    （拉丁/数字词元的 API 时间本来就是坏的，挪走不心疼），
    挪动后区间序号仍然单调不减，朗读顺序不会乱。
    """
    by_region: dict[int, list[int]] = {}
    for i, r in enumerate(assign):
        by_region.setdefault(r, []).append(i)

    for r in range(n_regions):
        if by_region.get(r):
            continue
        # 候选：前一个区间的最后一个词元 / 后一个区间的第一个词元
        cands: list[tuple[int, int]] = []
        prev = by_region.get(r - 1)
        if prev:
            cands.append((r - 1, prev[-1]))
        nxt = by_region.get(r + 1)
        if nxt:
            cands.append((r + 1, nxt[0]))
        # 不可信的词元优先
        cands.sort(key=lambda x: 0 if words[x[1]].get("untrusted") else 1)
        for src_region, token_idx in cands:
            src_list = by_region[src_region]
            if len(src_list) <= 1:
                continue        # 源区间不能被搬空
            src_list.remove(token_idx)
            target = by_region.setdefault(r, [])
            # 保持词元在区间内的先后顺序：从左邻搬来的排在最前，从右邻搬来的排在最后
            if src_region < r:
                target.insert(0, token_idx)
            else:
                target.append(token_idx)
            assign[token_idx] = r
            break
    return assign


def _close_gaps(out: list[dict], regions: list[tuple[float, float]]) -> None:
    """让词元时间铺满所有发声区间。

    有些句子的末尾词元时间戳会提前收尾（尾音拖长、或 API 把最后一个词
    的结束时间报早了），音频里还有一段实声却没有词元覆盖——
    字幕在这段会凭空消失。实测「提示词下面就是尺寸……」这句，
    最后一个词元排到 7.498s，音频却还在说到 8.308s。

    这里把相邻词元的时间撑开，把没人认领的发声区间补上。
    往前撑不超过下一个词元的起点，避免把后面的词元挤走。
    """
    for a, b in regions:
        if any(w["end"] > a + 1e-3 and w["start"] < b - 1e-3 for w in out):
            continue
        prev = [w for w in out if w["end"] <= a + 1e-3]
        nxt = [w for w in out if w["start"] >= b - 1e-3]
        if prev:
            w = max(prev, key=lambda x: x["end"])
            limit = min([x["start"] for x in nxt], default=b)
            w["end"] = round(max(w["end"], min(b, limit)), 3)
        elif nxt:
            w = min(nxt, key=lambda x: x["start"])
            limit = max([x["end"] for x in prev], default=a)
            w["start"] = round(min(w["start"], max(a, limit)), 3)


def align_words(words: list[dict], audio: str, duration: float) -> list[dict]:
    """校正词元时间戳。words 里的 start/end 是句内相对秒。

    返回新的 words 列表（结构与入参一致）。音频不可用时原样返回。
    """
    if not words:
        return words
    regions = detect_regions(audio, duration)
    if not regions:
        return words

    toks = []
    for w in words:
        text = w["word"]
        dur = float(w["end"]) - float(w["start"])
        toks.append({
            "word": text,
            "api_start": float(w["start"]),
            "api_end": float(w["end"]),
            "punct_only": bool(PUNCT_ONLY_RE.match(text)) and not CJK_RE.search(text),
            "untrusted": has_latin(text) or dur < TRUST_DUR,
        })

    assign = _segment_align(toks, regions)

    # 区间内按时长摊分
    by_region: dict[int, list[int]] = {}
    for i, r in enumerate(assign):
        by_region.setdefault(r, []).append(i)

    out = [dict(w) for w in words]
    for r, idxs in by_region.items():
        a, b = regions[r]
        span = b - a

        # 汉字用 API 时长（比例可信），拉丁词元按字符权重估算
        trusted = [i for i in idxs if not toks[i]["untrusted"] and not toks[i]["punct_only"]]
        raw: list[float] = []
        for i in idxs:
            if toks[i]["punct_only"]:
                raw.append(0.0)
            elif i in trusted:
                raw.append(max(0.02, toks[i]["api_end"] - toks[i]["api_start"]))
            else:
                raw.append(weight(toks[i]["word"]) * 0.13)
        if sum(raw) <= 0:
            raw = [weight(toks[i]["word"]) for i in idxs]
        scale = span / sum(raw)

        cursor = a
        for slot, i in enumerate(idxs):
            dur = raw[slot] * scale
            out[i]["start"] = round(cursor, 3)
            out[i]["end"] = round(cursor + dur, 3)
            cursor += dur

    # 补齐没人认领的发声区间：末尾词元提前收尾时，字幕会凭空消失一段
    _close_gaps(out, regions)

    # 纯标点：贴住前一个词元，不额外占时间
    for i, t in enumerate(toks):
        if not t["punct_only"]:
            continue
        prev = next((out[k]["end"] for k in range(i - 1, -1, -1) if not toks[k]["punct_only"]), None)
        if prev is None:
            nxt = next((out[k]["start"] for k in range(i + 1, len(out)) if not toks[k]["punct_only"]), 0.0)
            out[i]["start"] = out[i]["end"] = nxt
        else:
            out[i]["start"] = out[i]["end"] = prev

    # 保证严格递增（不出现负时长或倒序）
    for i in range(1, len(out)):
        if out[i]["start"] < out[i - 1]["end"]:
            out[i]["start"] = out[i - 1]["end"]
        if out[i]["end"] < out[i]["start"]:
            out[i]["end"] = out[i]["start"]
    return out


def repair_index(index: dict, index_path: pathlib.Path | None = None) -> dict:
    """就地修复 index.json 里所有句子的词元时间戳，返回统计。

    始终以 API 原始时间戳（sent["api_words"]）为输入：
    对齐结果会改写 words，若拿对齐后的 words 再对齐一次，
    「可信/不可信」的判定就会漂移，结果不再幂等。
    没有 api_words 的旧文件则退回用 words。
    """
    stats = {"sentences": 0, "fixed": 0, "holes_before": 0.0, "holes_after": 0.0,
             "flash_before": 0, "flash_after": 0}
    for sid, sec in index.get("sections", {}).items():
        for sent in sec.get("sentences", []):
            words = sent.get("words") or []
            audio = sent.get("audio") or ""
            if not words or not audio or not pathlib.Path(audio).exists():
                continue
            stats["sentences"] += 1
            base = float(sent["start"])

            source = sent.get("api_words") or words
            rel = [{"word": w["word"],
                    "start": float(w["start"]) - (base if "api_words" not in sent else 0.0),
                    "end": float(w["end"]) - (base if "api_words" not in sent else 0.0)}
                   for w in source]
            stats["holes_before"] += sum(max(0.0, rel[i + 1]["start"] - rel[i]["end"])
                                        for i in range(len(rel) - 1))
            stats["flash_before"] += sum(1 for w in rel if 0 < w["end"] - w["start"] < 0.15)

            new_rel = align_words(rel, audio, float(sent["duration"]))
            stats["holes_after"] += sum(max(0.0, new_rel[i + 1]["start"] - new_rel[i]["end"])
                                       for i in range(len(new_rel) - 1))
            stats["flash_after"] += sum(1 for w in new_rel if 0 < w["end"] - w["start"] < 0.15)

            for w, a in zip(words, new_rel):
                w["start"] = round(base + a["start"], 3)
                w["end"] = round(base + a["end"], 3)
            stats["fixed"] += 1

    if index_path is not None:
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    return stats
