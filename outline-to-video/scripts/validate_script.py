"""阶段 5a：脚本校验。

在把脚本交给用户确认之前，先做机器可查的体检：
字段完整性、画面引用是否越界、时长是否对得上、钩子是否到位、有没有重复画面。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from common import (  # noqa: E402
    StageError,
    StageLogger,
    load_json,
)

ROLES = {"hook", "point", "example", "payoff", "cta", "transition"}
MIN_CLIP = 0.8
CHARS_PER_SEC = 5.2  # 中文默认语速实测值（逐句合成，含每句首尾静音）
# 一句口播内部不该再有句读标点：TTS 会按这些标点再断句，
# 字幕也会按它们切行，结果是「一句」变成「两句」，画面和口播就对不上了。
INNER_PUNCT = "；！？…；!?"
MAX_VO_LINE = 58        # 单句超过这个长度，TTS 会拆成多句



def load_black_regions(work_dir: pathlib.Path) -> dict:
    """读工作目录下的 black_regions.json（没有就返回空 dict）。"""
    path = work_dir / "black_regions.json"
    if not path.exists():
        return {}
    try:
        raw = load_json(path).get("videos", {})
        return {k: v.get("regions", []) for k, v in raw.items()}
    except Exception:  # noqa: BLE001
        return {}


def validate(script: dict, inventory: dict,
             black_regions: dict | None = None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if not script.get("title"):
        errors.append("缺少 title")
    sections = script.get("sections")
    if not isinstance(sections, list) or not sections:
        errors.append("缺少 sections 数组")
        return errors, warnings

    videos = {v["id"]: v for v in inventory["videos"]}
    used_clips: dict[tuple, str] = {}
    seen_ids = set()
    total_chars = 0
    est_seconds = 0.0

    for idx, sec in enumerate(sections, start=1):
        sid = sec.get("id") or f"s{idx}"
        if sid in seen_ids:
            errors.append(f"{sid}: id 重复")
        seen_ids.add(sid)
        role = sec.get("role", "point")
        if role not in ROLES:
            errors.append(f"{sid}: role 取值非法（{role}），可用：{sorted(ROLES)}")
        vo = (sec.get("vo") or "").strip()
        if not vo:
            errors.append(f"{sid}: vo 为空")
        else:
            total_chars += len(vo)
            est_seconds += len(vo) / CHARS_PER_SEC
        if not sec.get("outline"):
            warnings.append(f"{sid}: 没有写明对应的大纲条目（outline 字段）")

        # 一句口播配一个画面：这是「画面跟着口播走」的前提。
        # 数量对不上时，时间线只能按比例摊，画面边界会和句子边界越走越偏
        # （实测最大偏 6.3s）。
        vo_lines = [ln for ln in vo.splitlines() if ln.strip()] if vo else []
        clips = sec.get("clips") or []
        if not clips:
            errors.append(f"{sid}: clips 为空，该节没有画面")
            continue
        if vo_lines and len(vo_lines) != len(clips):
            errors.append(f"{sid}: 口播 {len(vo_lines)} 句 vs 画面 {len(clips)} 个，"
                          f"必须一一对应（第 i 句配第 i 个画面），否则画面与口播会整体错位")
        for lno, line in enumerate(vo_lines, 1):
            if any(ch in line for ch in INNER_PUNCT):
                errors.append(f"{sid} 口播第 {lno} 句里有「；！？」这类标点："
                              f"它会被当成断句点，一句话变成两句，和画面对不上")
            if len(line) > MAX_VO_LINE:
                warnings.append(f"{sid} 口播第 {lno} 句 {len(line)} 字，超过 {MAX_VO_LINE} 字会被拆句")
        for cidx, clip in enumerate(clips, start=1):
            vid = clip.get("video")
            if vid not in videos:
                errors.append(f"{sid} clip#{cidx}: video={vid} 不在 inventory 中")
                continue
            start = clip.get("start")
            end = clip.get("end")
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                errors.append(f"{sid} clip#{cidx}: start/end 必须是数字")
                continue
            if end <= start:
                errors.append(f"{sid} clip#{cidx}: end 必须大于 start")
                continue
            dur = end - start
            vdur = videos[vid]["duration"]
            if start < 0 or end > vdur + 0.5:
                errors.append(f"{sid} clip#{cidx}: [{start}, {end}] 超出 {vid} 时长 {vdur}s")
            if dur < MIN_CLIP:
                warnings.append(f"{sid} clip#{cidx}: 片段只有 {dur:.2f}s，太短会闪")
            key = (vid, round(start, 2), round(end, 2))
            if key in used_clips:
                warnings.append(f"{sid} clip#{cidx}: 与 {used_clips[key]} 使用了同一段画面")
            else:
                used_clips[key] = f"{sid} clip#{cidx}"
        # 素材够不够铺满这一句的口播。
        # 镜头长度由这句口播的时长决定（见 build_timeline 的 1:1 对齐），
        # 所以 clip 的 start/end 只是「锚点标记」，真正要查的是：
        # 从锚点往后，源素材还剩多少秒——不够就只能定格，画面会变成重复帧。
        for cidx, clip in enumerate(clips, start=1):
            vid = clip.get("video")
            start = clip.get("start")
            if vid not in videos or not isinstance(start, (int, float)):
                continue
            if cidx > len(vo_lines):
                break
            need = len(vo_lines[cidx - 1]) / CHARS_PER_SEC
            avail = float(videos[vid]["duration"]) - float(start)
            if avail < need - 0.5:
                warnings.append(f"{sid} clip#{cidx}: 锚点 {start:.1f}s 往后只剩 {avail:.1f}s，"
                                f"但这句口播约 {need:.1f}s，不够的部分只能定格（画面会停住）")

    # 画面锚点落在素材的黑帧里 → 成片上会闪黑
    black = black_regions or {}
    if black:
        for idx, sec in enumerate(sections, start=1):
            sid = sec.get("id") or f"s{idx}"
            for cidx, clip in enumerate(sec.get("clips") or [], start=1):
                vid = clip.get("video")
                start = clip.get("start")
                if not isinstance(start, (int, float)):
                    continue
                for a, b in black.get(vid, []):
                    if a <= start < b:
                        errors.append(f"{sid} clip#{cidx}: 锚点 {start:.1f}s 落在 {vid} 的黑帧区 "
                                      f"{a:.1f}-{b:.1f}s（录屏掉帧），成片上会闪黑，换个锚点")

    if sections and sections[0].get("role") != "hook":
        warnings.append("第一节的 role 不是 hook，开头缺少钩子")
    if sections and sections[-1].get("role") not in ("cta", "payoff"):
        warnings.append("最后一节的 role 不是 cta/payoff，收尾不够完整")

    target = script.get("target_duration")
    if isinstance(target, (int, float)) and target > 0:
        drift = abs(est_seconds - target) / target
        if drift > 0.25:
            warnings.append(f"预估口播 {est_seconds:.0f}s 与目标时长 {target:.0f}s 偏差 {drift:.0%}")
        else:
            pass

    return errors, warnings


def main() -> int:
    ap = argparse.ArgumentParser(description="校验视频脚本 JSON")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--script", required=True)
    args = ap.parse_args()

    work_dir = pathlib.Path(args.work_dir)
    logger = StageLogger(work_dir, "validate")
    script = load_json(args.script)
    inventory = load_json(work_dir / "inventory.json")
    errors, warnings = validate(script, inventory, load_black_regions(work_dir))

    total_chars = sum(len((s.get("vo") or "")) for s in script.get("sections", []))
    est = total_chars / CHARS_PER_SEC
    logger.log(f"脚本：{len(script.get('sections', []))} 节 / {total_chars} 字 / 预估口播 {est:.0f}s")

    for w in warnings:
        logger.warn(w)
    for e in errors:
        logger.error(e)
    if errors:
        logger.error(f"校验未通过：{len(errors)} 个错误 / {len(warnings)} 个警告")
        return 1
    logger.log(f"校验通过（{len(warnings)} 个警告）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StageError as err:
        print(f"[ERROR] {err}", file=sys.stderr)
        if err.hint:
            print(f"[HINT]  {err.hint}", file=sys.stderr)
        raise SystemExit(2)
