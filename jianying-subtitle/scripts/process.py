"""剪映草稿字幕处理工具：提取 + 自动审查改正。

输入：剪映草稿目录（含 draft_content.json）
输出：
  <draft>.srt          — 原始字幕 SRT
  <draft>.txt          — 原始字幕纯文本
  <draft>.fixed.srt    — 审查改正后的 SRT
  <draft>.fixed.txt    — 审查改正后的纯文本
  <draft>.review.md    — 审查清单（Markdown）

用法：
  python process.py <draft_dir> [--output-dir OUT]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


# ────────────────────────────────────────────────────────────
# 数据结构：剪映工程内部时间单位为微秒（1/1_000_000 秒）
# ────────────────────────────────────────────────────────────

def us_to_srt(us: int) -> str:
    """微秒 -> SRT 时间码 HH:MM:SS,mmm。"""
    total_ms = us // 1000
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def us_to_hms(us: int) -> str:
    """微秒 -> HH:MM:SS（秒级），用于审查清单的时间码列。"""
    return us_to_srt(us).rsplit(',', 1)[0]


# ────────────────────────────────────────────────────────────
# 第一步：从 draft_content.json 提取字幕
# ────────────────────────────────────────────────────────────

def extract_subtitles(draft_json: Path) -> list[tuple[int, int, str]]:
    """返回 [(start_us, duration_us, text), ...]，按起始时间排序。

    遍历所有 type=text 的轨道并合并（剪映草稿可能含多条文本轨道：
    正文字幕 + 角标/标签等）。同 start_us + 同 text 的相邻条目会被去重。
    """
    data = json.loads(draft_json.read_text(encoding='utf-8'))

    text_by_id = {t['id']: t for t in data.get('materials', {}).get('texts', [])}

    text_tracks = [t for t in data.get('tracks', []) if t.get('type') == 'text']
    if not text_tracks:
        raise RuntimeError('draft_content.json 中未找到 text 轨道')

    rows: list[tuple[int, int, str]] = []
    seen: set[tuple[int, int, str]] = set()
    for text_track in text_tracks:
        for seg in sorted(
            text_track.get('segments', []),
            key=lambda s: s.get('target_timerange', {}).get('start', 0),
        ):
            text_mat = text_by_id.get(seg.get('material_id'))
            if not text_mat:
                continue
            try:
                content = json.loads(text_mat['content'])
                text = content.get('text', '').strip()
            except Exception:
                text = (text_mat.get('base_content') or '').strip()
            if not text:
                continue

            tr = seg.get('target_timerange') or {}
            start_us = tr.get('start', 0)
            dur_us = tr.get('duration', 0)
            key = (start_us, dur_us, text)
            if key in seen:
                continue
            seen.add(key)
            rows.append((start_us, dur_us, text))

    rows.sort(key=lambda r: r[0])
    return rows


def write_srt(rows: list[tuple[int, int, str]], dst: Path) -> None:
    with dst.open('w', encoding='utf-8') as f:
        for i, (start, dur, text) in enumerate(rows, 1):
            f.write(f'{i}\n')
            f.write(f'{us_to_srt(start)} --> {us_to_srt(start + dur)}\n')
            f.write(f'{text}\n\n')


def write_txt(rows: list[tuple[int, int, str]], dst: Path) -> None:
    with dst.open('w', encoding='utf-8') as f:
        for i, (_, _, text) in enumerate(rows, 1):
            f.write(f'{i:03d}  {text}\n')


# ────────────────────────────────────────────────────────────
# 第二步：自动审查与改正
# ────────────────────────────────────────────────────────────

# 「他」→「它」时排除「其他」一词
HE_PLACEHOLDER = '\x00HE\x00'


def fix_he_global(text: str) -> tuple[str, int]:
    """把指代 AI/工具的单字「他」改为「它」，保留「其他」。"""
    if '其他' not in text:
        return text.replace('他', '它'), text.count('他')
    protected = text.replace('其他', HE_PLACEHOLDER)
    count = protected.count('他')
    fixed = protected.replace('他', '它').replace(HE_PLACEHOLDER, '其他')
    return fixed, count


# 审查清单精确替换（行号是参考值，匹配靠字符串）
REPLACEMENTS: list[dict] = [
    # ── orca 草稿专用规则（基于 112 条字幕人工分析） ──────────────
    {'lines': [12], 'old': '而且非常好用的一个开源工具Vscode',
     'new': '而且非常好用的一个开源工具Orca',
     'kind': '错别字', 'note': '「Vscode」应为「Orca」（与草稿主题一致）'},
    {'lines': [18], 'old': 'Helix已经是大名鼎鼎',
     'new': 'Herdr已经是大名鼎鼎',
     'kind': '错别字', 'note': '按用户指示「Helix → Herdr」'},
    {'lines': [13], 'old': 'Vscode是一个专门为agent',
     'new': 'Orca是一个专门为agent',
     'kind': '错别字', 'note': '同上'},
    {'lines': [22], 'old': 'Windows上我用的是Vscode',
     'new': 'Windows上我用的是Orca',
     'kind': '错别字', 'note': '按用户指示统一为 Orca'},
    {'lines': [32], 'old': 'VOCA完全解决了这些问题',
     'new': 'Orca完全解决了这些问题',
     'kind': '错别字', 'note': '「VOCA」应为「Orca」（ASR 错字）'},
    {'lines': [47, 48], 'old': '让它同时来自动驱动我们\n048  本地的agent来进行开发',
     'new': '让它同时来自动驱动\n048  我们本地的agent来进行开发',
     'kind': '断句碎片', 'note': '「我们」与下行合并，避免主语突兀'},
    {'lines': [58], 'old': '它支持非常多的制定选项',
     'new': '它支持非常多的定制选项',
     'kind': '错别字', 'note': '「制定」应为「定制」'},
    {'lines': [74], 'old': '可以使用本地的任何功能',
     'new': '可以使用本地的任何agent',
     'kind': '错别字', 'note': '「功能」应为「agent」（上下文是 agent）'},
    {'lines': [85, 86], 'old': '它还集成了Liner和Git Hub这样\n086  的项目生命周期管理工具',
     'new': '它还集成了Linear和GitHub这样\n086  的项目生命周期管理工具',
     'kind': '错别字', 'note': '「Liner→Linear」、「Git Hub→GitHub」'},
    {'lines': [89], 'old': '而且work它的核心是基于Git',
     'new': '而且它的核心是基于Git',
     'kind': '错别字', 'note': '多余「work」'},
    {'lines': [90], 'old': 'work tree来进行多分支管理',
     'new': 'worktree来进行多分支管理',
     'kind': '错别字', 'note': 'git worktree 是复合名词，连写'},
    {'lines': [93, 94], 'old': '而且它们每一个项目自带\n094  的Git work tree来进行隔离',
     'new': '而且它们每一个项目自带\n094  的Git worktree来进行隔离',
     'kind': '错别字', 'note': 'worktree 连写；与下行构成「…自带的 Git worktree」'},
    {'lines': [104], 'old': '尔卡白皮书',
     'new': 'Orca白皮书',
     'kind': '错别字', 'note': '「尔卡」应为「Orca」（严重 ASR 错误）'},
    {'lines': [16], 'old': '在Git Hub上已经拿到7万多的star',
     'new': '在GitHub上已经拿到7万多的star',
     'kind': '错别字', 'note': '「Git Hub」应为「GitHub」（连写）'},
    {'lines': [42], 'old': '同时它还支持了Git Hub',
     'new': '同时它还支持了GitHub',
     'kind': '错别字', 'note': '「Git Hub」应为「GitHub」（连写）'},
    {'lines': [43, 44], 'old': '甚至是Liner等项目管理平台\n044  比如你直接在Liner集成以后',
     'new': '甚至是Linear等项目管理平台\n044  比如你直接在Linear集成以后',
     'kind': '错别字', 'note': '「Liner」应为「Linear」（项目管理工具真名）'},
    {'lines': [68], 'old': 'MACDOWN的预览还可以支持所见即所得',
     'new': 'Markdown的预览还可以支持所见即所得',
     'kind': '错别字', 'note': '「MACDOWN」应为「Markdown」（与 039 行自相矛盾）'},
    {'lines': [78], 'old': '你用Github或者是Github上这些issue',
     'new': '你用GitHub或者是GitHub上这些issue',
     'kind': '错别字', 'note': '「Github」应为「GitHub」（官方拼写大小写）'},

    # ── 通用规则（其他草稿也适用） ──────────────────────────────
    {'lines': [179], 'old': '最重要的一个月',
     'new': '最重要的一个点', 'kind': '错别字', 'note': '「月」应为「点/理由」'},
    {'lines': [88], 'old': '比零零碎碎改十几次要高效的多',
     'new': '比零零碎碎改十几次要高效得多', 'kind': '错别字', 'note': '「的/得」误用'},
    {'lines': [113], 'old': '玩家会玩的怎么样',
     'new': '玩家会玩得怎么样', 'kind': '错别字', 'note': '「的/得」误用'},
    {'lines': [131], 'old': '都能够清晰的看得到',
     'new': '都能够清晰地看得到', 'kind': '错别字', 'note': '「的/地」误用'},
    {'lines': [21], 'old': '还能够学会到怎么样用豆包工作',
     'new': '还能够学会怎么样用豆包工作', 'kind': '多余字', 'note': '「到」多余'},
    {'lines': [74], 'old': '分数和目标都只能自动过关',
     'new': '分数和目标一到就自动过关', 'kind': '多余字', 'note': '「只」语义不当'},
    {'lines': [76], 'old': '实际缺少一些背景音乐和音效',
     'new': '实际上缺少一些背景音乐和音效', 'kind': '多余字', 'note': '「实际」应为「实际上」'},
    {'lines': [104], 'old': '还能够自己兜底和质量',
     'new': '还能自己做好兜底和质量保证', 'kind': '缺字', 'note': '缺动词承接'},
    {'lines': [144], 'old': '最值的',
     'new': '最值得的', 'kind': '缺字', 'note': '缺「得」'},
    {'lines': [177], 'old': '我要想跟大家说一下',
     'new': '我要跟大家说一下', 'kind': '多余字', 'note': '「想」多余'},
    {'lines': [186], 'old': '等一会新版本',
     'new': '等一会儿新版本', 'kind': '缺字', 'note': '「一会儿」缺「儿」'},
    {'lines': [181], 'old': '给你给它聊天',
     'new': '跟你或它聊天', 'kind': '重复字词', 'note': '「给」重复'},
    {'lines': [185], 'old': '接着接着生成',
     'new': '接着生成', 'kind': '重复字词', 'note': '「接着」重复'},
    {'lines': [171], 'old': '问题的关卡怎么判断标准',
     'new': '关卡问题的判断标准', 'kind': '语病', 'note': '断句结构不顺'},
    {'lines': [174, 175], 'old': '这一步的效果就是你花\n175  了30分钟摸索的经验',
     'new': '这一步的效果就是让你花\n175  30分钟摸索出来的经验',
     'kind': '语病', 'note': '跨行；主语切换突兀'},
    {'lines': [184], 'old': '发反馈电脑上的豆包接着改代码',
     'new': '发个反馈，电脑上的豆包接着改代码',
     'kind': '语病', 'note': '「发反馈」后缺标点/动词'},
    {'lines': [6, 7], 'old': '说是拆任务写代码做\n007  素材做可视化都能干',
     'new': '说是拆任务写代码\n007  做素材做可视化都能干',
     'kind': '断句碎片', 'note': '条目 6 末尾孤立「做」字'},

    # ── step 草稿专用规则 ──────────────────────────────────────
    {'lines': [41], 'old': '现在已经放到了Git Hub上',
     'new': '现在已经放到了GitHub上',
     'kind': '错别字', 'note': '「Git Hub」应为「GitHub」（连写）'},

    # ── step 草稿 —— 工具名标准化（全局替换） ──────────────────
    # 所有 stepcode / Stepcode / step code 都统一为 Step-Code
    # 单个 "step" 单词保留不动（用户指示）
    {'lines': [], 'old': 'Stepcode', 'new': 'Step-Code',
     'kind': '错别字', 'note': '工具名 → Step-Code'},
    {'lines': [], 'old': 'stepcode', 'new': 'Step-Code',
     'kind': '错别字', 'note': '工具名 → Step-Code'},
    {'lines': [], 'old': 'step code', 'new': 'Step-Code',
     'kind': '错别字', 'note': '工具名 → Step-Code'},

    # ── Claude Code 大小写 ──────────────────────────────────────
    {'lines': [], 'old': 'Claude code', 'new': 'Claude Code',
     'kind': '错别字', 'note': '官方写法「Claude Code」（C 大写）'},

    # ── Codec / Codex ASR 错字 ─────────────────────────────────
    {'lines': [], 'old': 'Codec', 'new': 'Codex',
     'kind': '错别字', 'note': 'Codec → Codex（ASR 错字）'},

    # ── MiniMax / StepFun 大小写 ───────────────────────────────
    {'lines': [], 'old': 'Minimax', 'new': 'MiniMax',
     'kind': '错别字', 'note': 'MiniMax（官方大小写）'},
    {'lines': [], 'old': 'stepfun', 'new': 'StepFun',
     'kind': '错别字', 'note': 'StepFun（公司名官方大小写）'},

    # ── 模型名 step-5-preview ──────────────────────────────────
    {'lines': [], 'old': 'STEP5 Preview', 'new': 'step-5-preview',
     'kind': '错别字', 'note': '模型名统一为 step-5-preview'},
    {'lines': [], 'old': 'step 5 Preview', 'new': 'step-5-preview',
     'kind': '错别字', 'note': '同上'},
    {'lines': [], 'old': 'step5 Preview', 'new': 'step-5-preview',
     'kind': '错别字', 'note': '同上'},
    {'lines': [], 'old': 'step 5的Preview', 'new': 'step-5-preview',
     'kind': '错别字', 'note': '同上'},
    {'lines': [], 'old': 'step5的Preview', 'new': 'step-5-preview',
     'kind': '错别字', 'note': '同上'},

    # ── Codex 的 X 多余「的」字 ────────────────────────────────
    {'lines': [92], 'old': 'Codex的给出', 'new': 'Codex给出',
     'kind': '重复字词', 'note': '「Codex的给出」→「Codex给出」（多余「的」）'},
    {'lines': [193], 'old': 'Codex的做', 'new': 'Codex做',
     'kind': '重复字词', 'note': '「Codex的做」→「Codex做」（多余「的」）'},

    # ── funbench 草稿专用规则 ──────────────────────────────────
    {'lines': [29], 'old': '首先你需要从Git Hub上把项目克隆下来',
     'new': '首先你需要从GitHub上把项目克隆下来',
     'kind': '错别字', 'note': '「Git Hub」应为「GitHub」（连写）'},
    {'lines': [76], 'old': '代码已经提交到get Hub',
     'new': '代码已经提交到GitHub',
     'kind': '错别字', 'note': '「get Hub」应为「GitHub」（ASR 严重错误）'},
    {'lines': [81], 'old': '也可以欢迎提示issue',
     'new': '也可以欢迎提issue',
     'kind': '错别字', 'note': '「提示」应为「提」（ASR 误加「示」）'},

    # ── 模型名 / 品牌名官方写法（全局替换） ────────────────────
    {'lines': [], 'old': 'Deepseek', 'new': 'DeepSeek',
     'kind': '错别字', 'note': 'DeepSeek（官方大小写）'},
    {'lines': [], 'old': 'Mini Max', 'new': 'MiniMax',
     'kind': '错别字', 'note': 'MiniMax（官方大小写）'},
    {'lines': [], 'old': 'M3.1Flash', 'new': 'M3.1 Flash',
     'kind': '错别字', 'note': '模型名后补空格'},
    {'lines': [], 'old': 'V4.1Flash', 'new': 'V4.1 Flash',
     'kind': '错别字', 'note': '模型名后补空格'},
    {'lines': [], 'old': 'Flash1017', 'new': 'Flash 1017',
     'kind': '错别字', 'note': '版本号前补空格'},

    # ── MFlash 草稿专用规则（基于 257 条字幕人工分析） ──────────
    # 模型名规范
    # 专用名词：MiniMax M3.1 Flash / DeepSeek V4.1 Flash /
    #           DeepSeek V4.1 Flash 1017 / GLM 5.3 Flash / Fun Bench
    {'lines': [10], 'old': 'DeepSeek4.1Flash77分',
     'new': 'DeepSeek V4.1 Flash 77分',
     'kind': '错别字', 'note': '补全专用名词「DeepSeek V4.1 Flash」'},
    {'lines': [11], 'old': '而M3.1 Flash73.7分',
     'new': '而M3.1 Flash 73.7分',
     'kind': '错别字', 'note': '模型名与分数之间补空格'},
    {'lines': [12], 'old': '安全方面GRM584.5',
     'new': '安全方面GLM是84.5',
     'kind': '错别字', 'note': '「GRM」为「GLM」；「5」是「是」的同音误写'},
    {'lines': [13], 'old': 'M3点一82.2',
     'new': 'M3.1是82.2',
     'kind': '错别字', 'note': '「M3点一」即「M3.1」'},
    {'lines': [19], 'old': '三bench测试的方法非常简单',
     'new': 'Fun Bench测试的方法非常简单',
     'kind': '错别字', 'note': '「三bench」为工具真名「Fun Bench」'},
    {'lines': [32], 'old': 'DeepSeek V4.1的Flash 1017版本和',
     'new': 'DeepSeek V4.1 Flash 1017版本和',
     'kind': '错别字', 'note': '专用名词「DeepSeek V4.1 Flash 1017」'},
    {'lines': [33], 'old': 'DeepSeek V4.1 Flash以及GRM的5.3Flash',
     'new': 'DeepSeek V4.1 Flash以及GLM 5.3 Flash',
     'kind': '错别字', 'note': '「GRM」为「GLM」，补全「GLM 5.3 Flash」'},
    {'lines': [106], 'old': '最终的获胜者是b选手MiniMax M3 Flash模型',
     'new': '最终的获胜者是b选手MiniMax M3.1 Flash模型',
     'kind': '错别字', 'note': '全片唯一型号为 M3.1 Flash，「M3 Flash」漏「.1」'},
    {'lines': [140], 'old': '是第二名MiniMax M3Flash的7倍',
     'new': '是第二名MiniMax M3.1 Flash的7倍',
     'kind': '错别字', 'note': '同上，补全「.1」'},
    {'lines': [138], 'old': '没想到是GLM5.3Flash模型',
     'new': '没想到是GLM 5.3 Flash模型',
     'kind': '错别字', 'note': '补全专用名词「GLM 5.3 Flash」'},
    {'lines': [199], 'old': '迷你Max M3.1 Flash和GRM5.3 Flash相对更好',
     'new': 'MiniMax M3.1 Flash和GLM 5.3 Flash相对更好',
     'kind': '错别字', 'note': '「迷你Max」为「MiniMax」；「GRM」为「GLM」'},
    {'lines': [228], 'old': '居然是GM5.3Flash',
     'new': '居然是GLM 5.3 Flash',
     'kind': '错别字', 'note': '「GM」漏「L」，应为「GLM 5.3 Flash」'},
    {'lines': [230], 'old': 'MiniMax M3.1 Flash与DeepSeek的1017模型相当',
     'new': 'MiniMax M3.1 Flash与DeepSeek V4.1 Flash 1017相当',
     'kind': '错别字', 'note': '专用名词「DeepSeek V4.1 Flash 1017」'},
    {'lines': [237], 'old': '最低的是DeepSeek V4.1的Flash',
     'new': '最低的是DeepSeek V4.1 Flash',
     'kind': '错别字', 'note': '专用名词「DeepSeek V4.1 Flash」'},
    {'lines': [242], 'old': '最高的是GM5.3 Flash',
     'new': '最高的是GLM 5.3 Flash',
     'kind': '错别字', 'note': '「GM」漏「L」，应为「GLM 5.3 Flash」'},
    {'lines': [244], 'old': '排名在后两位的分别是DeepSeek 4.1 Flash 1017',
     'new': '排名在后两位的分别是DeepSeek V4.1 Flash 1017',
     'kind': '错别字', 'note': '补全「V」'},
    # ASR 错字
    {'lines': [50], 'old': '经典的t壶骑自行车',
     'new': '经典的茶壶骑自行车',
     'kind': '错别字', 'note': '「t壶」为「茶壶」（经典考题：茶壶骑自行车）'},
    {'lines': [107], 'old': '感觉UI方面徐统还是一如既往的优秀',
     'new': '感觉UI方面输出还是一如既往的优秀',
     'kind': '错别字', 'note': '「徐统」为「输出」（ASR 同音错字）'},
    {'lines': [171], 'old': '基于three点js的日本樱花谷',
     'new': '基于three.js的日本樱花谷',
     'kind': '错别字', 'note': '「three点js」即 three.js'},
    # 语病 / 多余字
    {'lines': [88], 'old': '但人物的和场景丰富度不够',
     'new': '但人物和场景丰富度不够',
     'kind': '多余字', 'note': '「人物的和场景」多「的」'},
    {'lines': [126], 'old': '但增加过时有错误',
     'new': '但增加功能时有错误',
     'kind': '错别字', 'note': '「过时」为「功能时」（与 c/d 选手表述一致）'},
    {'lines': [129], 'old': '增加时候有提示',
     'new': '增加的时候有提示',
     'kind': '语病', 'note': '缺助词「的」'},
    {'lines': [131], 'old': 'c选手质检数13个',
     'new': 'c选手质检bug数13个',
     'kind': '缺字', 'note': '与 a/b 选手「质检bug数N个」表述对齐'},
    {'lines': [134], 'old': 'd选手发现bug数11个',
     'new': 'd选手质检bug数11个',
     'kind': '错别字', 'note': '与 a/b/c 选手「质检bug数N个」表述对齐'},
    {'lines': [148], 'old': '逐条进行比',
     'new': '逐条进行比对',
     'kind': '缺字', 'note': '句子被截断，缺「对」'},
    {'lines': [207], 'old': '用最少的步骤让所有的人过河',
     'new': '用最少的步骤让所有人过河',
     'kind': '多余字', 'note': '「所有的」多「的」'},
    # 的/得 误用
    {'lines': [92], 'old': 'b选手的画面和UI做的比较丰富',
     'new': 'b选手的画面和UI做得比较丰富',
     'kind': '错别字', 'note': '「的/得」误用'},
    {'lines': [133], 'old': '表现的比较好',
     'new': '表现得比较好',
     'kind': '错别字', 'note': '「的/得」误用'},
    {'lines': [150], 'old': '能正确的计算出总通过的数量为15条',
     'new': '能正确地计算出总通过的数量为15条',
     'kind': '错别字', 'note': '「的/得」误用'},
    # 词语分割（跨行）
    {'lines': [69, 70], 'old': '接下来我们看一下这\n070  四个选手分别是谁',
     'new': '接下来我们看一下\n070  这四个选手分别是谁',
     'kind': '词语分割', 'note': '「这四个」被拆到两行'},
    {'lines': [89, 90], 'old': 'c选手画面和人物比a\n090  选手要提升了不少',
     'new': 'c选手画面和人物比a选手\n090  提升了不少',
     'kind': '词语分割', 'note': '「a选手」被拆到两行'},
    {'lines': [96, 97], 'old': '基本上提示词中所有\n097  的要求都已经实现',
     'new': '基本上提示词中所有的\n097  要求都已经实现',
     'kind': '词语分割', 'note': '「所有的要求」被拆到两行'},
    {'lines': [123, 124], 'old': '质检的bug数量和添加\n124  功能这里的交付效果',
     'new': '质检的bug数量和添加功能\n124  这里的交付效果',
     'kind': '词语分割', 'note': '「添加功能」被拆到两行'},
    {'lines': [167, 168], 'old': '分别是DeepSeek V4.1 Flash的1017\n168  版本和MiniMax M3.1的Flash',
     'new': '分别是DeepSeek V4.1 Flash 1017版本\n168  和MiniMax M3.1 Flash',
     'kind': '词语分割', 'note': '「1017版本」被拆到两行'},
    {'lines': [214, 215], 'old': '但上面的求解步骤和\n215  下方的步骤表不一致',
     'new': '但上面的求解步骤\n215  和下方的步骤表不一致',
     'kind': '词语分割', 'note': '「和下方的」被拆到两行'},
    {'lines': [248, 249], 'old': '比如MiniMax M3.1 Flash在MiniMax\n249  code中效果会更好',
     'new': '比如MiniMax M3.1 Flash在\n249  MiniMax Code中效果会更好',
     'kind': '词语分割', 'note': '产品名「MiniMax Code」被拆到两行'},
]


def _strip_line_num(s: str) -> str:
    """去掉 .txt 格式里行首的 <数字>  前缀。"""
    import re as _re
    return _re.sub(r'^\d+\s\s', '', s)


def fix_contestant_labels(text: str) -> tuple[str, int]:
    """把参赛选手标签的小写 a/b/c/d 统一为大写 A/B/C/D。

    只在明确贴着「选手」或「和」的语境下替换，避免误伤英文单词
    （bug / Harness / DeepSeek 等）。仅改文字，不涉及样式。
    """
    patterns = [
        re.compile(r'(?<![A-Za-z])([abcd])(?=选手)'),      # a选手 → A选手
        re.compile(r'(?<=选手)([abcd])(?![A-Za-z])'),      # 选手a → 选手A
        re.compile(r'(?<![A-Za-z])([abcd])(?=和)'),        # c和d → C和d
        re.compile(r'和([abcd])(?![A-Za-z])'),             # b和c → b和C
    ]
    count = 0

    def _upper(match: re.Match) -> str:
        nonlocal count
        count += 1
        return match.group(0).replace(match.group(1), match.group(1).upper())

    for pat in patterns:
        text = pat.sub(_upper, text)
    return text, count


def apply_replacements(text: str, is_srt: bool = False) -> tuple[str, list[dict]]:
    """对文本应用精确替换，跨行条目自动支持 .txt 与 .srt 两种格式。

    .txt 格式：相邻条目用 ``\\n<行号>  <文本>`` 分隔
    .srt 格式：相邻条目用 ``\\n\\n<序号>\\n<时间码>\\n<文本>`` 分隔

    对跨行条目在 .srt 上的处理：
      不能直接拼成 ``\\n\\n``（中间隔着序号+时间码），改为：
      对每一行分别做单行替换（同一条规则同时记一次 hit）。
    """
    hits: list[dict] = []
    for r in REPLACEMENTS:
        if '\n' not in r['old']:
            # 单行条目：直接匹配
            cnt = text.count(r['old'])
            if cnt:
                text = text.replace(r['old'], r['new'])
                hits.append({**r, 'count': cnt})
            continue

        # 跨行条目
        old_lines = r['old'].split('\n')
        new_lines = r['new'].split('\n')
        if len(old_lines) != len(new_lines):
            continue
        old_srt_lines = [_strip_line_num(l) for l in old_lines]
        new_srt_lines = [_strip_line_num(l) for l in new_lines]

        per_rule_total = 0
        rule_hit = False
        for i, (o_txt, n_txt, o_srt, n_srt) in enumerate(
            zip(old_lines, new_lines, old_srt_lines, new_srt_lines)
        ):
            target_old = o_srt if is_srt else o_txt
            target_new = n_srt if is_srt else n_txt
            if target_old and target_old in text:
                cnt = text.count(target_old)
                text = text.replace(target_old, target_new)
                per_rule_total += cnt
                rule_hit = True
        if rule_hit:
            hits.append({**r, 'count': per_rule_total})
    return text, hits


# ────────────────────────────────────────────────────────────
# 第三步：通用检测 —— 不依赖预定义清单
# ────────────────────────────────────────────────────────────

# 句尾疑似断词的孤立字（短句时尤其可疑）
# 注：「了」不列入 —— 句尾「了」是极常见的正常句式；真正需要抓的
#     「V了N」被切断的情形由规则 2（下行以「了」+数字开头）覆盖
SUSPECT_TAIL_CHARS = set('做在把给和与以来地到为而但或')
# 已知的合法短句收尾，避免误报（"这真的"、"真的"、"好了"、"行了" 等）
KNOWN_OK_TAILS = ('别的', '真的', '好的', '行了', '好了', '没了', '走了', '完了', '到了')
# 强信号：这些字几乎不会作为词尾出现，行尾出现基本就是「动词/连词被切到下一行」
STRONG_TAIL_CHARS = set('做把给和与但或让被从向跟同用拿比讲谈聊问写改装放送追帮替对沿朝依据按加减乘除')
# 弱信号：可以成词，但下列常见词尾组合属于正常用语，需排除
WEAK_TAIL_CHARS = set('在地到为而了来以')
# 弱信号下的合法词尾组合（行尾出现这些说明不是断词）
OK_WORD_ENDS = (
    '可以', '类似', '以为', '以来', '未来', '后来', '起来', '出来', '过来', '回来',
    '下来', '上来', '本来', '完了', '好了', '算了', '来了', '说了', '到了', '得到',
    '收到', '受到', '想到', '说到', '提到', '来到', '回到', '成为', '作为', '认为',
    '属于', '位于', '存在', '现在', '正在', '于是', '但是', '因为', '所以', '或者',
    '或是', '土地', '各地', '各地', '到底', '到处', '原因', '目的', '结果', '最了',
    '看了', '听了', '改了', '写了', '出了', '进了', '过了', '拿了', '走了', '跑了',
    '用了', '带了', '做了', '给了', '要了', '行了', '好了', '见了', '好了', '行了',
    '确实', '其实', '突然', '忽然', '仍然', '依然', '居然', '竟然', '当然', '终于',
    '分别', '分别', '一下', '一些', '一点', '一样', '一般', '一切', '一起', '一直',
    '接下来', '而且', '并且', '以及', '而后', '然而', '随后', '其次', '最后',
)


def _is_short(text: str, limit: int = 10) -> bool:
    """判断是否短句。"""
    return len(text) <= limit


def detect_split_words(rows: list[tuple[int, int, str]]) -> list[dict]:
    """检测相邻字幕之间的词语/词组被切分。

    规则：
    1. 下行以「的」开头（极强信号：几乎不会单独成句）—— 「完整的小游戏」型
    2. 下行以「了」+ 数字开头 —— 「花了30分钟」型补语
    3. 上行以孤立虚词结尾（短句），如「做」「在」「把」—— 「做素材」型
    """
    issues: list[dict] = []
    for i in range(len(rows) - 1):
        text1 = rows[i][2]
        text2 = rows[i + 1][2]
        line1 = i + 1
        line2 = i + 2

        # 规则 1：下行以「的」开头
        if text2.startswith('的') and len(text2) > 1:
            issues.append({
                'kind': '词语分割',
                'lines': [line1, line2],
                'text': f'{text1} / {text2}',
                'reason': '下行以「的」开头，疑为「形容词+的+名词」型断词（如"完整的小游戏"）',
            })
            continue

        # 规则 2：下行以「了」+ 数字开头（数字出现在前 4 字内）
        if (len(text2) >= 2
                and text2[0] == '了'
                and any(c.isdigit() for c in text2[1:5])):
            issues.append({
                'kind': '词语分割',
                'lines': [line1, line2],
                'text': f'{text1} / {text2}',
                'reason': '下行以「了+数字」开头，疑为「V了N」型补语被切断',
            })
            continue

        # 规则 3：上行以孤立虚词结尾且短句
        if _is_short(text1) and text1[-1] in SUSPECT_TAIL_CHARS:
            tail = text1[-1]
            # 排除已知合法短语
            if text1.endswith(KNOWN_OK_TAILS):
                continue
            # 弱信号字（了/来/以/在/地/到/为/而）必须排除合法词尾，否则大量误报
            if tail in WEAK_TAIL_CHARS and text1.endswith(OK_WORD_ENDS):
                continue
            # 强信号字（做/把/给/和/与/但/或…）几乎不成词尾，可信度高
            confidence = '强' if tail in STRONG_TAIL_CHARS else '中'
            issues.append({
                'kind': '词语分割',
                'lines': [line1, line2],
                'text': f'{text1} / {text2}',
                'reason': f'上行末尾孤立「{tail}」字（短句），疑与下行构成断词（信号强度：{confidence}）',
            })
    return issues


# 中文中常见的叠词（不算病句）
NORMAL_REPEATS = ('一条一条', '一项一项', '一个一个', '一次一次',
                  '一步一步', '一个一个', '一天一天', '一年一年')


def detect_ill_formed(rows: list[tuple[int, int, str]]) -> list[dict]:
    """检测病句/语句不通顺。

    规则：
    1. 连续重复单字 ≥ 3 次（如「的的的」「了了了」）
    2. 不在叠词白名单中的 2-4 字重叠词（如「接着接着」「给你给」）
    3. 单条字幕过长（>30 字符，可能显示溢出）
    4. 单条字幕过短（≤ 2 字符，几乎都是 ASR 错误）
    """
    issues: list[dict] = []
    for i, (_, _, text) in enumerate(rows):
        line_no = i + 1

        # 连续重复单字（排除数字/千分位中的「000」）
        # 用 \x00 占位，避免数字被替换成空格后产生假的「连续空格」
        text_no_digits = re.sub(r'\d[\d,.]*\d|\d', '\x00', text)
        m = re.search(r'(.)\1{2,}', text_no_digits)
        if m:
            issues.append({
                'kind': '语句不通顺',
                'lines': [line_no],
                'text': text,
                'reason': f'单字「{m.group(0)[0]}」连续重复 {len(m.group(0))} 次',
            })

        # 重叠词（排除常见叠词）
        for m in re.finditer(r'(.{2,4}?)\1', text):
            if m.group(0) in NORMAL_REPEATS:
                continue
            issues.append({
                'kind': '语句不通顺',
                'lines': [line_no],
                'text': text,
                'reason': f'重叠词「{m.group(0)}」',
            })

        # 长度异常
        if len(text) > 30:
            issues.append({
                'kind': '语句不通顺',
                'lines': [line_no],
                'text': text,
                'reason': f'长度 {len(text)} 字符，建议拆分',
            })
        # 常见的合法短句（如「谢谢」「嗯」「好」）
        KNOWN_SHORT_OK = {'谢谢', '嗯', '啊', '好', '是的', '不是', '对', '好的',
                          '再见', '拜拜', '开始', '结束'}
        if 0 < len(text) <= 2 and text not in KNOWN_SHORT_OK:
            issues.append({
                'kind': '语句不通顺',
                'lines': [line_no],
                'text': text,
                'reason': f'长度仅 {len(text)} 字符，可能 ASR 错误',
            })
    return issues


# ────────────────────────────────────────────────────────────
# 审查清单输出
# ────────────────────────────────────────────────────────────

def write_review_md(
    rows: list[tuple[int, int, str]],
    he_count: int,
    label_count: int,
    replacement_hits: list[dict],
    split_issues: list[dict],
    ill_formed_issues: list[dict],
    dst: Path,
    video_name: str,
) -> None:
    """生成审查清单 Markdown。"""
    md: list[str] = []
    md.append(f'# {video_name} 字幕审查清单')
    md.append('')
    md.append('## 处理摘要')
    md.append('')
    md.append(f'- 字幕条目数：**{len(rows)}**')
    md.append(f'- 全局「他」→「它」：**{he_count}** 处（保留「其他」不变）')
    md.append(f'- 选手标签 a/b/c/d → A/B/C/D：**{label_count}** 处')
    md.append(f'- 精确改正命中：**{len(replacement_hits)}** 条')
    md.append(f'- 通用检测 —— 词语分割：**{len(split_issues)}** 处')
    md.append(f'- 通用检测 —— 语句不通顺：**{len(ill_formed_issues)}** 处')
    md.append('')

    # 按类型汇总
    by_kind: dict[str, list[dict]] = {}
    for h in replacement_hits:
        by_kind.setdefault(h['kind'], []).append(h)
    for h in split_issues:
        by_kind.setdefault(h['kind'], []).append(h)
    for h in ill_formed_issues:
        by_kind.setdefault(h['kind'], []).append(h)

    md.append('## 错误统计')
    md.append('')
    md.append('| 错误类型 | 数量 |')
    md.append('| --- | --- |')
    total = 0
    for kind in ('错别字', '多余字', '缺字', '重复字词', '语病',
                 '断句碎片', '词语分割', '语句不通顺'):
        if kind in by_kind:
            md.append(f'| {kind} | {len(by_kind[kind])} |')
            total += len(by_kind[kind])
    md.append(f'| **合计** | **{total}** |')
    md.append('')

    def render_section(kind: str, items: list[dict]) -> None:
        if not items:
            return
        md.append(f'### {kind}')
        md.append('')
        md.append('| 行号 | 时间码 | 内容 | 备注 |')
        md.append('| --- | --- | --- | --- |')
        for it in items:
            lines = it.get('lines', [])
            line_str = ', '.join(str(n) for n in lines)
            first_line = lines[0] if lines else 0
            ts = us_to_hms(rows[first_line - 1][0]) if 0 < first_line <= len(rows) else '全局'
            text = it.get('text', '')
            if not text:
                # 精确替换规则没有自带 text，按行号从原文还原
                parts = [rows[n - 1][2] for n in lines if 0 < n <= len(rows)]
                text = ' / '.join(parts) if parts else '（全局替换）'
            note = it.get('reason') or it.get('note', '')
            md.append(f'| {line_str} | {ts} | {text} | {note} |')
        md.append('')

    md.append('## 详细清单')
    md.append('')
    for kind in ('错别字', '多余字', '缺字', '重复字词', '语病',
                 '断句碎片', '词语分割', '语句不通顺'):
        render_section(kind, by_kind.get(kind, []))

    md.append('## 全局「他」→「它」处理')
    md.append('')
    md.append(f'- 共 **{he_count}** 处指代 AI/工具的单字「他」已替换为「它」')
    md.append('- 「其他」（条目 048）作为完整词语保留不动')
    md.append('')

    md.append('## 通用检测说明')
    md.append('')
    md.append('### 词语分割（不依赖预定义清单）')
    md.append('')
    md.append('- **下行以「的」开头**：极强信号，提示「形容词+的+名词」型断词')
    md.append('- **下行以「了」+ 数字开头**：提示「V了N」型补语被切断')
    md.append('- **上行末尾孤立虚词**（做/把/给/和/与/但/或…）+ 短句：**强**信号，基本可判定为断词')
    md.append('- **上行末尾弱信号虚词**（在/地/到/为/而/来/以）+ 短句：需排除合法词尾后才是**中**信号')
    md.append('')
    md.append('### 语句不通顺')
    md.append('')
    md.append('- 单字连续重复 ≥ 3 次（如「的的的」）')
    md.append('- 非叠词白名单的 2-4 字重叠词（叠词白名单：一条一条/一项一项 等）')
    md.append('- 长度异常（>30 字符或 ≤ 2 字符）')
    md.append('')
    md.append('## 备注')
    md.append('')
    md.append('- 通用检测结果可能含少量误报，需结合上下文判断')
    md.append('- 跨行精确替换通过字符串匹配实现，`.txt` 需保留行号前缀，`.srt` 自动改为单行匹配')
    md.append('- 本次改动只涉及字幕文字内容，不涉及字幕样式、位置、字号、特效等渲染属性')
    md.append('- 通用检测算法对其他剪映草稿同样适用，不依赖条目编号')

    dst.write_text('\n'.join(md), encoding='utf-8')


# ────────────────────────────────────────────────────────────
# 主入口
# ────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description='从剪映草稿提取字幕并自动审查改正错别字/重复字词/语病/词语分割。'
    )
    parser.add_argument('draft_dir', type=Path,
                        help='剪映草稿目录（含 draft_content.json）')
    parser.add_argument('--output-dir', '-o', type=Path, default=None,
                        help='输出目录（默认：当前工作目录）')
    args = parser.parse_args()

    draft_dir: Path = args.draft_dir
    draft_json = draft_dir / 'draft_content.json'
    if not draft_json.exists():
        print(f'❌ 找不到 {draft_json}', file=sys.stderr)
        return 1

    out_dir: Path = args.output_dir or Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = draft_dir.name

    print(f'📂 草稿：{draft_dir}')
    print(f'📁 输出目录：{out_dir}')

    # Step 1: 提取
    print('\n=== 提取字幕 ===')
    rows = extract_subtitles(draft_json)
    print(f'共 {len(rows)} 条字幕')

    srt_path = out_dir / f'{stem}.srt'
    txt_path = out_dir / f'{stem}.txt'
    write_srt(rows, srt_path)
    write_txt(rows, txt_path)
    print(f'✓ {srt_path.name}')
    print(f'✓ {txt_path.name}')

    # Step 2: 改正
    print('\n=== 自动审查改正 ===')
    txt_text = txt_path.read_text(encoding='utf-8').replace('\r\n', '\n')
    srt_text = srt_path.read_text(encoding='utf-8').replace('\r\n', '\n')

    txt_fixed, he_count = fix_he_global(txt_text)
    srt_fixed, _ = fix_he_global(srt_text)
    print(f'全局「他」→「它」共 {he_count} 处')

    # 精确替换先跑：规则里的 old 按「原始小写标签」书写
    txt_fixed, hits = apply_replacements(txt_fixed, is_srt=False)
    srt_fixed, _ = apply_replacements(srt_fixed, is_srt=True)
    print(f'精确替换命中 {len(hits)} 条')

    # 选手标签统一大写放在最后，避免打乱精确规则的匹配
    txt_fixed, label_count = fix_contestant_labels(txt_fixed)
    srt_fixed, _ = fix_contestant_labels(srt_fixed)
    print(f'选手标签 a/b/c/d → A/B/C/D 共 {label_count} 处')

    # Step 3: 通用检测
    print('\n=== 通用检测 ===')
    split_issues = detect_split_words(rows)
    ill_formed = detect_ill_formed(rows)
    print(f'词语分割疑似 {len(split_issues)} 处')
    print(f'语句不通顺疑似 {len(ill_formed)} 处')

    # Step 4: 输出
    fixed_txt = out_dir / f'{stem}.fixed.txt'
    fixed_srt = out_dir / f'{stem}.fixed.srt'
    fixed_txt.write_text(txt_fixed, encoding='utf-8')
    fixed_srt.write_text(srt_fixed, encoding='utf-8')

    review_md = out_dir / f'{stem}.review.md'
    write_review_md(rows, he_count, label_count, hits, split_issues, ill_formed,
                    review_md, stem)

    print()
    print(f'✓ {fixed_txt.name}')
    print(f'✓ {fixed_srt.name}')
    print(f'✓ {review_md.name}')

    # 残留检查
    remaining_he = [l for l in txt_fixed.splitlines()
                    if '他' in l and '其他' not in l]
    print()
    if remaining_he:
        print(f'⚠ 残留「他」{len(remaining_he)} 处：')
        for l in remaining_he:
            print(f'  {l}')
    else:
        print('✅ 无残留「他」')

    return 0


if __name__ == '__main__':
    sys.exit(main())