"""把 process.py 输出的改正后字幕灌回剪映草稿 draft_content.json。

注意：
  - 必须在**关闭剪映**后运行，否则剪映下次保存会覆盖你的修改
  - 脚本会先自动备份为 draft_content.json.review-backup
  - 合并所有 type=text 轨道的 segment，按 start_us 升序后与 fixed.srt 一一对应

用法：
  python scripts/write_back.py <draft_dir> [--output-dir OUT] [--fixed-srt PATH]

  默认：
    <draft_dir>           必填，剪映草稿目录（含 draft_content.json）
    --output-dir          process.py 的输出目录（含 <draft名>.fixed.srt），默认当前工作目录
    --fixed-srt           显式指定 fixed.srt 路径，覆盖自动推断
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def parse_srt(path: Path) -> list[tuple[int, int, str]]:
    srt_text = path.read_text(encoding='utf-8').replace('\r\n', '\n')
    blocks = re.split(r'\n\n+', srt_text.strip())
    rows = []
    for blk in blocks:
        lines = blk.split('\n')
        if len(lines) < 3:
            continue
        time_line = lines[1]
        m = re.match(
            r'(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})',
            time_line,
        )
        if not m:
            continue
        h, mn, s, ms, h2, mn2, s2, ms2 = (int(x) for x in m.groups())
        start_us = ((h * 3600 + mn * 60 + s) * 1000 + ms) * 1000
        end_us = ((h2 * 3600 + mn2 * 60 + s2) * 1000 + ms2) * 1000
        text = '\n'.join(lines[2:]).strip()
        rows.append((start_us, end_us - start_us, text))
    rows.sort(key=lambda r: r[0])
    return rows


def replace_text_in_content(content_str: str, new_text: str) -> tuple[str, bool]:
    """修改 materials.texts[].content 字符串里的 text 字段，保持其它字段与键顺序原样。

    同时同步 `styles[].range`：剪映的富文本样式用**字符区间**描述作用范围，
    文字长度变了必须同步区间，否则样式只覆盖前 N 个字，字幕样式会被打乱
    （表现为「部分字幕样式和前后不一致」）。
    """
    try:
        obj = json.loads(content_str)
    except Exception:
        return content_str, False
    if not isinstance(obj, dict):
        return content_str, False
    old_text = obj.get('text', '')
    if old_text == new_text:
        return content_str, False
    obj['text'] = new_text
    sync_style_ranges(obj.get('styles'), old_text, new_text)
    return json.dumps(obj, ensure_ascii=False), True


def sync_style_ranges(styles, old_text: str, new_text: str) -> int:
    """把富文本样式的字符区间同步到新文本长度。

    - 原本覆盖整条文本的区间（[0, len(old)]）→ 改成 [0, len(new)]
    - 局部区间：起点保持不变，终点超出新长度时截断
    """
    if not isinstance(styles, list) or not styles:
        return 0
    old_len, new_len = len(old_text), len(new_text)
    changed = 0
    for st in styles:
        if not isinstance(st, dict):
            continue
        rng = st.get('range')
        if not (isinstance(rng, list) and len(rng) == 2):
            continue
        start, end = rng
        if start == 0 and end == old_len:
            new_rng = [0, new_len]
        else:
            new_rng = [start, min(end, new_len)]
        if new_rng != rng:
            st['range'] = new_rng
            changed += 1
    return changed


def build_index_map(old: str, new: str) -> list[int]:
    """旧文本每个字符下标 → 新文本下标 的映射（difflib 对齐）。"""
    import difflib

    mapping = [0] * (len(old) + 1)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new).get_opcodes():
        if tag == 'equal':
            for k in range(i1, i2):
                mapping[k] = j1 + (k - i1)
            mapping[i2] = j2
        elif tag == 'replace':
            span_old = i2 - i1
            span_new = j2 - j1
            for k in range(i1, i2):
                mapping[k] = j1 + (round((k - i1) * span_new / span_old) if span_old else 0)
            mapping[i2] = j2
        elif tag == 'delete':
            for k in range(i1, i2):
                mapping[k] = j1
            mapping[i2] = j1
        else:  # insert
            mapping[i1] = j1
    return mapping


def remap_words(material: dict, old_text: str, new_text: str) -> tuple[bool, str]:
    """把 ASR 逐词时间轴 `words` 重新映射到改后的文本上。

    - 词被整段删掉 → 该词连同它的起止时间一起丢弃（保证三个数组等长）
    - 词之间新插入的字符 → 并入前一个词
    - 任何校验不过就放弃修改（原样保留），绝不写出半成品
    """
    words = material.get('words')
    if not isinstance(words, dict):
        return False, 'words 结构异常'
    pieces = words.get('text')
    starts = words.get('start_time')
    ends = words.get('end_time')
    if not (isinstance(pieces, list) and isinstance(starts, list) and isinstance(ends, list)):
        return False, 'words 字段类型异常'
    if not (len(pieces) == len(starts) == len(ends)) or not pieces:
        return False, 'words 三个数组长度不一致'
    if ''.join(pieces) != old_text:
        return False, 'words 与原文本本来就对不上'

    mapping = build_index_map(old_text, new_text)

    # 旧文本里每个词的字符区间 → 映射到新文本
    spans: list[list[int]] = []
    pos = 0
    for p in pieces:
        spans.append([mapping[min(pos, len(mapping) - 1)],
                      mapping[min(pos + len(p), len(mapping) - 1)]])
        pos += len(p)
    for i in range(1, len(spans)):
        if spans[i][0] < spans[i - 1][1]:
            spans[i][0] = spans[i - 1][1]
        if spans[i][1] < spans[i][0]:
            spans[i][1] = spans[i][0]

    # 丢弃映射后变空的词（整段被删）
    keep = [i for i, (s, e) in enumerate(spans) if e > s]
    if not keep:
        # 全文都被删 → 退化为单词
        keep = [0]
        spans[0] = [0, len(new_text)]
    spans = [spans[i] for i in keep]
    starts = [starts[i] for i in keep]
    ends = [ends[i] for i in keep]

    # 词之间的空隙（插入的字符）并入前一个词
    for i in range(len(spans) - 1):
        if spans[i + 1][0] > spans[i][1]:
            spans[i][1] = spans[i + 1][0]
    # 首尾补齐，保证拼接后正好是全文
    spans[0][0] = 0
    spans[-1][1] = len(new_text)

    new_pieces = [new_text[s:e] for s, e in spans]
    if any(not p for p in new_pieces):
        return False, '映射后仍有空词'
    if ''.join(new_pieces) != new_text:
        return False, '映射结果与新文本不一致'
    if any(starts[i] > starts[i + 1] for i in range(len(starts) - 1)):
        return False, '起始时间非递增'
    if any(ends[i] > ends[i + 1] for i in range(len(ends) - 1)):
        return False, '结束时间非递增'

    words['text'] = new_pieces
    words['start_time'] = starts
    words['end_time'] = ends
    return True, ''


# 残留 ASR 错字检查（已知常见错误，写回后必须清零）
KNOWN_BAD_SUBSTRINGS = [
    # orca 草稿
    'Vscode', 'VOCA', 'MACDOWN', 'Liner', 'Git Hub', 'Github', '尔卡',
    # MFlash 草稿：模型名 / 专用名词 / ASR 同音错字
    'GRM', 'GM5', 'Minimax', 'Mini Max', 'Deepseek', '三bench',
    '徐统', 't壶', 'M3Flash', 'M3.1Flash', 'V4.1Flash', 'Flash1017',
    'three点js', 'M3点一', '增加过时', '质检数',
]


def check_residue(data: dict) -> list[str]:
    bad_hits = []
    for t in data.get('materials', {}).get('texts', []):
        try:
            txt = json.loads(t['content']).get('text', '')
        except Exception:
            continue
        for kw in KNOWN_BAD_SUBSTRINGS:
            if kw in txt:
                bad_hits.append(f'  {kw!r} in {txt!r}')
    return bad_hits


def check_lowercase_labels(data: dict) -> list[str]:
    """提醒：选手标签 a/b/c/d 未统一为大写（只提醒，不判失败）。"""
    hits = []
    for t in data.get('materials', {}).get('texts', []):
        try:
            txt = json.loads(t['content']).get('text', '')
        except Exception:
            continue
        if re.search(r'(?<![A-Za-z])[abcd](?![A-Za-z])', txt):
            hits.append(f'  {txt!r}')
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(
        description='把改正后字幕灌回剪映草稿 draft_content.json',
    )
    parser.add_argument('draft_dir', type=Path, help='剪映草稿目录')
    parser.add_argument('--output-dir', '-o', type=Path, default=None,
                        help='process.py 的输出目录（默认：当前工作目录）')
    parser.add_argument('--fixed-srt', type=Path, default=None,
                        help='显式指定 fixed.srt 路径')
    args = parser.parse_args()

    draft_dir: Path = args.draft_dir
    draft_json = draft_dir / 'draft_content.json'
    if not draft_json.exists():
        print(f'❌ 找不到 {draft_json}', file=sys.stderr)
        return 1

    if args.fixed_srt:
        srt_path = args.fixed_srt
    else:
        out_dir = args.output_dir or Path.cwd()
        stem = draft_dir.name
        srt_path = out_dir / f'{stem}.fixed.srt'

    if not srt_path.exists():
        print(f'❌ 找不到 {srt_path}', file=sys.stderr)
        return 1

    print(f'📂 草稿：{draft_dir}')
    print(f'📥 改正 SRT：{srt_path}')

    # 备份原文件（写回前），避免不可逆
    backup_path = draft_json.with_name('draft_content.json.review-backup')
    if not backup_path.exists():
        import shutil
        shutil.copy2(draft_json, backup_path)
        print(f'💾 已备份：{backup_path}')
    else:
        print(f'💾 已存在备份：{backup_path}（跳过）')

    rows = parse_srt(srt_path)
    print(f'   SRT 解析：{len(rows)} 条')

    data = json.loads(draft_json.read_text(encoding='utf-8'))

    # 合并所有文本轨道 segment，按 start 排序
    text_by_id = {t['id']: t for t in data.get('materials', {}).get('texts', [])}
    all_segs: list[dict] = []
    for tr in data.get('tracks', []):
        if tr.get('type') == 'text':
            for seg in tr.get('segments', []):
                all_segs.append(seg)
    all_segs.sort(key=lambda s: s.get('target_timerange', {}).get('start', 0))

    print(f'   文本 segment 总数：{len(all_segs)}')

    if len(all_segs) != len(rows):
        print(f'⚠ segment 数 {len(all_segs)} ≠ SRT 行数 {len(rows)}，中止', file=sys.stderr)
        return 1

    hits = 0
    words_fixed = 0
    words_skipped: list[tuple[str, str]] = []
    skipped_unchanged = 0
    skipped_no_mat = 0
    for seg, (srt_start, srt_dur, new_text) in zip(all_segs, rows):
        seg_start = seg.get('target_timerange', {}).get('start', 0)
        seg_dur = seg.get('target_timerange', {}).get('duration', 0)
        if abs(seg_start - srt_start) > 1000 or abs(seg_dur - srt_dur) > 1000:
            print(f'⚠ 时间码偏差过大：seg({seg_start}, {seg_dur}) vs srt({srt_start}, {srt_dur})')

        mat_id = seg.get('material_id')
        mat = text_by_id.get(mat_id)
        if not mat:
            skipped_no_mat += 1
            continue
        try:
            old_text = json.loads(mat['content']).get('text', '')
        except Exception:
            old_text = ''
        new_content, changed = replace_text_in_content(mat['content'], new_text)
        if changed:
            mat['content'] = new_content
            hits += 1
            # 同步 ASR 逐词时间轴，使其与改后的文本一致
            ok, reason = remap_words(mat, old_text, new_text)
            if ok:
                words_fixed += 1
            else:
                words_skipped.append((new_text, reason))
        else:
            skipped_unchanged += 1

    print(f'   实际改写：{hits}（未变 {skipped_unchanged}，缺 material {skipped_no_mat}）')
    print(f'   逐词时间轴同步：{words_fixed}（跳过 {len(words_skipped)}）')
    for txt, reason in words_skipped[:5]:
        print(f'     跳过 {txt!r}：{reason}')

    # 写回
    draft_json.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    print(f'✓ 已写回：{draft_json}')

    # 校验
    print()
    print('=== 校验 ===')
    data2 = json.loads(draft_json.read_text(encoding='utf-8'))
    text_by_id2 = {t['id']: t for t in data2['materials']['texts']}
    all_segs2: list[dict] = []
    for tr in data2['tracks']:
        if tr.get('type') == 'text':
            all_segs2.extend(tr.get('segments', []))
    all_segs2.sort(key=lambda s: s.get('target_timerange', {}).get('start', 0))

    mismatched = []
    for seg, (srt_start, srt_dur, want_text) in zip(all_segs2, rows):
        mat = text_by_id2.get(seg.get('material_id'))
        if not mat:
            continue
        try:
            txt_now = json.loads(mat['content']).get('text', '')
        except Exception:
            txt_now = ''
        if txt_now != want_text:
            mismatched.append((seg.get('target_timerange', {}).get('start', 0),
                               txt_now, want_text))
    if mismatched:
        print(f'⚠ 校验失败 {len(mismatched)} 条：')
        for s, now, want in mismatched[:5]:
            print(f'  start={s}')
            print(f'    now = {now!r}')
            print(f'    want= {want!r}')
        return 2
    print(f'✅ 全部 {len(all_segs2)} 条匹配')

    # 样式区间与文本长度必须一致（否则字幕样式会只覆盖前 N 个字）
    style_bad = []
    words_bad = []
    for mat in text_by_id2.values():
        try:
            c = json.loads(mat['content'])
        except Exception:
            continue
        txt = c.get('text', '')
        for st in c.get('styles') or []:
            rng = st.get('range')
            if (isinstance(rng, list) and len(rng) == 2
                    and rng[0] == 0 and rng[1] != len(txt)):
                style_bad.append((txt, rng))
        w = mat.get('words')
        if isinstance(w, dict) and w.get('text'):
            if ''.join(w['text']) != txt:
                words_bad.append((txt, ''.join(w['text'])))
    if style_bad:
        print(f'⚠ 样式区间与文本长度不符 {len(style_bad)} 条：')
        for t, r in style_bad[:5]:
            print(f'  text={t!r} len={len(t)} range={r}')
    else:
        print(f'✅ 全部字幕的样式区间与文本长度一致')
    if words_bad:
        print(f'⚠ 逐词时间轴与文本不符 {len(words_bad)} 条：')
        for t, w in words_bad[:5]:
            print(f'  text={t!r} words={w!r}')
    else:
        print(f'✅ 全部逐词时间轴与文本一致')

    # 残留 ASR 错字检查
    print()
    print('=== 残留 ASR 错字 ===')
    bads = check_residue(data2)
    if bads:
        print(f'⚠ 仍有 {len(bads)} 处残留：')
        for b in bads[:10]:
            print(b)
        return 3
    print(f'✓ 全部已知 ASR 错字残留为 0')

    # 选手标签大小写提醒（不判失败）
    lower = check_lowercase_labels(data2)
    if lower:
        print()
        print(f'⚠ 仍有小写 a/b/c/d 标签 {len(lower)} 处（确认是否应为大写）：')
        for l in lower[:5]:
            print(l)

    return 0


if __name__ == '__main__':
    sys.exit(main())
