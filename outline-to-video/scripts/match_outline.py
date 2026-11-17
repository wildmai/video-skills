"""阶段 4：大纲 ↔ 素材匹配。

把大纲条目拆成关键词，在各素材的转写文本上做加权匹配，
给出每个大纲条目最可能的素材区间（含转写原文佐证），供撰写脚本时取用。
产出 work/candidates.json 与 work/outline_map.md。
"""

from __future__ import annotations

import argparse
import math
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from common import (  # noqa: E402
    StageError,
    StageLogger,
    load_json,
    save_json,
)

CJK = r"一-鿿"
STOPWORDS = set(
    "的 了 和 是 在 有 与 及 对 从 到 为 以 也 都 就 而 被 把 让 给 使 等 中 上 下 "
    "个 这 那 我们 你们 他们 它 你 他 她 其 之 于 或 并 但 如 若 则 因 所 由 会 能 要 "
    "可以 应该 什么 怎么 如何 为什么 一个 一些 这些 那些 不 没 很 更 最 已经 还是 "
    "the a an and or of to in is are for on with as at by be this that it you we they"
    .split()
)
KEYWORD_RE = re.compile(r"\{([^}]+)\}|（?关键词[:：]\s*([^）]+)）?")


def parse_outline(text: str) -> tuple[str, list[dict]]:
    """解析大纲文本，返回 (总标题, 条目列表)。"""
    title = ""
    items: list[dict] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            level = len(heading.group(1))
            body = heading.group(2).strip()
            if level == 1 and not title:
                title = body
                continue
            if level <= 2 and not items:
                title = title or body
                continue
            items.append({"level": level, "text": body})
            continue
        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        if bullet:
            items.append({"level": 3, "text": bullet.group(1).strip()})
            continue
        numbered = re.match(r"^(\d+)[.)、]\s*(.*)$", stripped)
        if numbered:
            items.append({"level": 3, "text": numbered.group(2).strip()})
            continue
        items.append({"level": 3, "text": stripped})
    if not items:
        raise StageError("大纲里没有解析出任何条目", hint="确认大纲文件内容不为空，且每条要点独占一行")
    return title, items


def extract_keywords(text: str) -> list[str]:
    """抽取关键词：显式标注优先，其次拉丁词，最后中文二元组。"""
    explicit: list[str] = []
    for m in KEYWORD_RE.finditer(text):
        raw = m.group(1) or m.group(2) or ""
        for kw in re.split(r"[,，、;；\s]+", raw):
            kw = kw.strip()
            if kw:
                explicit.append(kw)
    body = KEYWORD_RE.sub(" ", text)
    body = re.sub(r"[*_`#>\[\]()（）《》“”\"'‘’,，。！？!?:：、；;~～\-—…·|/\\]", " ", body)

    keywords: list[str] = []
    for token in body.split():
        token = token.strip()
        if not token:
            continue
        low = token.lower()
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.+-]{1,}", token):
            if low not in STOPWORDS:
                keywords.append(low)
            continue
        if re.fullmatch(rf"[{CJK}]{{2,}}", token):
            if len(token) <= 4:
                if token not in STOPWORDS:
                    keywords.append(token)
            for i in range(len(token) - 1):
                bg = token[i:i + 2]
                if bg not in STOPWORDS:
                    keywords.append(bg)
            continue
        if re.fullmatch(rf"[{CJK}]", token) and token not in STOPWORDS:
            keywords.append(token)

    seen = set()
    ordered: list[str] = []
    for kw in explicit + keywords:
        if kw in seen:
            continue
        seen.add(kw)
        ordered.append(kw)
    return ordered


def normalize(text: str) -> str:
    return re.sub(r"[\s，。！？!?:：、；;~～\-—…·「」『』（）()\[\]【】\"'‘’,.]+", "", text).lower()


def build_windows(segments: list[dict], target: float = 12.0, max_gap: float = 2.5) -> list[dict]:
    """把转写片段聚合成约 target 秒的检索窗口。"""
    windows: list[dict] = []
    cur: list[dict] = []
    for seg in segments:
        if cur and (seg["end"] - cur[0]["start"] > target or seg["start"] - cur[-1]["end"] > max_gap):
            windows.append({"segments": cur})
            cur = []
        cur.append(seg)
    if cur:
        windows.append({"segments": cur})
    for w in windows:
        w["start"] = w["segments"][0]["start"]
        w["end"] = w["segments"][-1]["end"]
        w["text"] = " ".join(s["text"] for s in w["segments"])
        w["norm"] = normalize(w["text"])
    return windows


def merge_windows(hits: list[dict], gap: float = 4.0) -> list[dict]:
    """把相邻命中的窗口合并成候选片段。"""
    if not hits:
        return []
    hits = sorted(hits, key=lambda h: (h["video"], h["start"]))
    merged = [dict(hits[0])]
    for hit in hits[1:]:
        last = merged[-1]
        if hit["video"] == last["video"] and hit["start"] - last["end"] <= gap:
            last["end"] = max(last["end"], hit["end"])
            last["hits"] = sorted(set(last["hits"] + hit["hits"]))
            last["score"] = round(last["score"] + hit["score"] * 0.35, 4)
            last["windows"] = last.get("windows", 1) + hit.get("windows", 1)
        else:
            merged.append(dict(hit))
    return merged


def score_windows(keywords: list[str], windows: list[dict], idf: dict[str, float]) -> list[dict]:
    scored = []
    for w in windows:
        hits = []
        raw = 0.0
        for kw in keywords:
            if kw in w["norm"]:
                weight = idf.get(kw, 1.0) * (1.0 if len(kw) <= 2 else 1.6)
                raw += weight
                hits.append(kw)
        if raw <= 0:
            continue
        coverage = len(set(hits)) / max(1, len(keywords))
        scored.append({
            "video": w["video"],
            "start": w["start"],
            "end": w["end"],
            "score": round(raw * (0.55 + 0.45 * coverage), 4),
            "coverage": round(coverage, 3),
            "hits": sorted(set(hits)),
            "excerpt": w["text"][:220],
        })
    scored.sort(key=lambda s: s["score"], reverse=True)
    return scored


def main() -> int:
    ap = argparse.ArgumentParser(description="把大纲条目匹配到素材视频片段")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--outline", required=True, help="大纲文件路径（utf-8 文本/markdown）")
    ap.add_argument("--top-k", type=int, default=6)
    ap.add_argument("--window", type=float, default=12.0, help="检索窗口时长（秒）")
    ap.add_argument("--merge-gap", type=float, default=4.0)
    args = ap.parse_args()

    work_dir = pathlib.Path(args.work_dir)
    logger = StageLogger(work_dir, "match")
    outline_path = pathlib.Path(args.outline).expanduser()
    if not outline_path.exists():
        raise StageError(f"大纲文件不存在：{outline_path}", hint="确认用户给出的大纲路径")
    text = outline_path.read_text(encoding="utf-8", errors="replace")
    title, items = parse_outline(text)
    logger.log(f"大纲解析：{len(items)} 个条目，标题「{title or '未命名'}」")

    inventory = load_json(work_dir / "inventory.json")
    transcripts: dict[str, list[dict]] = {}
    for item in inventory["videos"]:
        tpath = work_dir / "transcripts" / f"{item['id']}.json"
        if tpath.exists():
            transcripts[item["id"]] = load_json(tpath)["segments"]
        else:
            transcripts[item["id"]] = []

    all_windows = []
    for vid, segments in transcripts.items():
        for w in build_windows(segments, args.window):
            w["video"] = vid
            all_windows.append(w)
    if not all_windows:
        raise StageError("没有可用的转写内容", hint="先运行 transcribe.py，或为视频准备外挂字幕")

    df: dict[str, int] = {}
    for w in all_windows:
        for kw in set(re.findall(rf"[{CJK}]{{2}}|[a-z0-9][a-z0-9_.+-]+", w["norm"])):
            df[kw] = df.get(kw, 0) + 1
    n_windows = len(all_windows)
    idf = {kw: math.log(n_windows / (1 + cnt)) + 0.5 for kw, cnt in df.items()}

    result = {"title": title, "outline_source": str(outline_path.resolve()), "items": []}
    md = [f"# 大纲 ↔ 素材匹配报告", "", f"大纲文件：`{outline_path.name}`", f"条目数：{len(items)}", ""]

    for idx, item in enumerate(items, start=1):
        keywords = extract_keywords(item["text"])
        scored = score_windows(keywords, all_windows, idf)
        grouped: dict[tuple[str, float], list[dict]] = {}
        for s in scored:
            grouped.setdefault((s["video"], round(s["start"] // 10)), []).append(s)
        merged = merge_windows(
            [{**g[0], "windows": len(g), "hits": sorted({h for x in g for h in x["hits"]}),
              "score": round(sum(x["score"] for x in g), 4)} for g in grouped.values()],
            args.merge_gap,
        )
        merged.sort(key=lambda m: m["score"], reverse=True)
        top = merged[: args.top_k]
        best_coverage = max((s["coverage"] for s in scored), default=0.0)
        status = "ok" if best_coverage >= 0.34 else ("sparse" if best_coverage > 0 else "miss")
        entry = {
            "id": f"o{idx}",
            "order": idx,
            "level": item["level"],
            "text": item["text"],
            "keywords": keywords,
            "status": status,
            "candidates": top,
        }
        result["items"].append(entry)
        logger.log(f"[{idx}] {item['text'][:34]} | 关键词 {len(keywords)} 个 | 状态 {status} | 最佳命中 {best_coverage:.2f}")
        for c in top[:3]:
            logger.log(f"      → {c['video']} {c['start']:.1f}-{c['end']:.1f}s score={c['score']} hits={c['hits'][:6]}")

        md.append(f"## {idx}. {item['text']}")
        md.append("")
        md.append(f"- 状态：{status}｜关键词：{'、'.join(keywords[:12]) or '（无）'}")
        if top:
            md.append("| 素材 | 区间 | 匹配度 | 命中词 | 转写佐证 |")
            md.append("| --- | --- | --- | --- | --- |")
            for c in top:
                excerpt = c.get("excerpt", "").replace("|", "/")[:110]
                md.append(f"| {c['video']} | {c['start']:.1f}s → {c['end']:.1f}s | {c['score']} | "
                          f"{'、'.join(c['hits'][:6])} | {excerpt} |")
        else:
            md.append("- 无匹配候选：需要人工在素材里定位或补录素材。")
        md.append("")

    save_json(work_dir / "candidates.json", result)
    (work_dir / "outline_map.md").write_text("\n".join(md), encoding="utf-8")
    miss = [i for i in result["items"] if i["status"] != "ok"]
    logger.log(f"完成：{len(result['items'])} 条，其中 {len(miss)} 条匹配稀疏需人工确认 → {work_dir / 'outline_map.md'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StageError as err:
        print(f"[ERROR] {err}", file=sys.stderr)
        if err.hint:
            print(f"[HINT]  {err.hint}", file=sys.stderr)
        raise SystemExit(2)
