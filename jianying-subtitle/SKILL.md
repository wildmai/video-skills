---
name: jianying-subtitle
description: 从剪映（JianyingPro）草稿提取字幕生成标准 SRT，并自动审查改正错别字、重复字词、他/它指代错误、断句碎片、词语分割和语句不通顺。Use when user provides a JianyingPro draft directory path (containing draft_content.json) and wants to (1) extract subtitles to SRT, (2) review subtitles and list errors (typos, repeated words, 他/它 errors, sentence fragments, split words, ill-formed sentences), or (3) apply fixes back into the draft (write_back). Especially useful when a video was exported from Jianying and the user wants to find and correct ASR subtitle issues.
---

# Jianying Subtitle

从剪映草稿一键提取字幕 → 自动审查改正 → 输出 SRT + 审查清单 → （可选）写回草稿。

## 适用场景

用户给出剪映草稿路径（典型路径如 `D:/JianyingPro Drafts/<日期或项目名>/`），希望：
1. 把草稿里的字幕导成标准 SRT 文件
2. 检查字幕里的错别字、重复字词、他/它指代错误、词语分割、语句不通顺
3. 一键应用审查改正，并产出审查清单
4. （可选）把改正后字幕直接灌回 `draft_content.json`

视频通常**没有内嵌字幕**或字幕是 ASR 生成的有错字，从草稿提取能拿到原始的、已对齐时间码的字幕文本。

## 快速使用

### 第一步：提取 + 审查

```bash
python scripts/process.py "<剪映草稿目录>"
```

输出（默认写到当前工作目录）：
- `<草稿名>.srt` — 原始 SRT 字幕
- `<草稿名>.txt` — 原始纯文本（带行号，便于人审）
- `<草稿名>.fixed.srt` — 审查改正后的 SRT
- `<草稿名>.fixed.txt` — 审查改正后的纯文本
- `<草稿名>.review.md` — 审查清单（Markdown）

可选参数：

```bash
python scripts/process.py "D:/JianyingPro Drafts/9月7日" --output-dir "E:/output"
```

### 第二步：（可选）写回草稿

```bash
python scripts/write_back.py "<剪映草稿目录>" --output-dir "<同 process.py 的输出目录>"
```

**写回前必须关闭剪映**，否则剪映下次保存会覆盖你的修改。

写回脚本会自动：
1. 备份原文件为 `draft_content.json.review-backup`
2. 合并所有文本轨道的 segment 后按 start 排序，与 fixed.srt 一一对应
3. 修改 `materials.texts[].content.text` 字段
4. 校验全部匹配 + 检查常见 ASR 错字残留

## 检测能力

### 精确改正（依赖预定义清单，行号是参考值）

| 类型 | 数量 | 典型例子 |
| --- | --- | --- |
| 错别字 | 4 | 「最重要的一个月」「月」字、「的/得/地」误用 |
| 多余字 / 缺字 | 7 | 「学会到怎么样」多「到」、「最值的」少「得」 |
| 重复字词 | 2 | 「给你给它聊天」、「接着接着生成」 |
| 语病 | 2 | 主语切换突兀、句子结构断裂 |
| 断句碎片 | 1 | 跨行条目 6-7、174-175 合并 |

### 通用检测（不依赖预定义清单，对其他草稿同样适用）

| 检测 | 算法 | 信号强度 |
| --- | --- | --- |
| 词语分割 | 下行以「的」开头 | 极强（几乎不会单独成句） |
| 词语分割 | 下行以「了」+ 数字开头 | 强（V了N 型补语被切断） |
| 词语分割 | 上行末尾孤立强虚词（做/把/给/和/与/但/或…）+ 短句 | 强（几乎不成词尾） |
| 词语分割 | 上行末尾孤立弱虚词（在/地/到/为/而/来/以）+ 短句 | 中（需排除合法词尾后） |
| 语句不通顺 | 单字连续重复 ≥ 3 次（排除数字） | 强 |
| 语句不通顺 | 非叠词白名单的 2-4 字重叠 | 中 |
| 语句不通顺 | 长度异常（>30 或 ≤ 2 字符） | 中 |

调参经验（MFlash 草稿，257 条）：

- 句尾「了」**不**参与断词检测 —— 句尾带「了」是极常见句式，误报率高达 6/8；
  真正需要抓的「V了N」被切断，由「下行以『了』+数字开头」这条规则覆盖。
- 数字剔除时用 `\x00` 占位，**不要**替换成空格，否则「Deepseek 4.1 Flash 1017」
  会因数字被抹成空格而误报「单字空格连续重复 3 次」。
- 弱虚词行尾需要词尾白名单（`可以/接下来/得到/成为/…`）才能压住误报。

## 工作流程

### 第一步：定位草稿

草稿目录必须包含 `draft_content.json`。如果用户给的路径是草稿父目录（如 `D:/JianyingPro Drafts/`），需要先列出所有子目录让用户确认是哪一个。

```bash
ls "D:/JianyingPro Drafts/"
```

### 第二步：运行 process.py

脚本自动：
1. 解析 `draft_content.json` 中**所有** `tracks[type=text]` 和 `materials.texts[]`（多文本轨道会被合并）
2. 按 `target_timerange.start` 排序，提取 `(start_us, duration_us, text)` 三元组
3. 写 SRT 和 TXT（时间单位微秒 → `HH:MM:SS,mmm`）
4. 全局「他」→「它」（保留「其他」）
5. 应用审查清单精确替换（默认 18 条通用规则；专用规则写在 `scripts/process.py` 顶部）
6. **选手标签归一化**：`a/b/c/d` → `A/B/C/D`（仅在紧贴「选手」或「和」的语境下替换，不误伤 `bug`/`Harness` 等英文词）
7. **通用检测**：扫描词语分割、语句不通顺
8. 生成 Markdown 审查清单

### 第三步：汇报结果

告知用户：
- 字幕条目数
- 全局「他」→「它」数量
- 精确清单命中数
- 通用检测疑似数量
- 残留「他」检查结果（应为 0）
- 输出文件路径

**通用检测会有少量误报**（特别是孤立虚词规则），需向用户说明「疑似」性质，并引导人工复核。

### 第四步：（可选）写回草稿

如果用户希望直接灌回 `draft_content.json`：
1. **先关闭剪映**
2. 让用户确认（写回是不可逆的，脚本会先备份）
3. 运行 `scripts/write_back.py`
4. 启动剪映核对

## 扩展审查清单

如果用户指出新的错误类型，按以下格式追加到 `scripts/process.py` 的 `REPLACEMENTS`：

```python
{'lines': [<行号>], 'old': '<原文>', 'new': '<改正>', 'kind': '<类型>', 'note': '<备注>'},
```

跨行条目用 `\n` 分隔并**包含下一行的行号前缀**（`.txt` 格式：`<行号>  <文本>`）：

```python
{'lines': [174, 175], 'old': '旧文本\n175  续文本', 'new': '新文本\n175  续新文本', ...}
```

`apply_replacements` 会自动：
- 对 `.txt` 用原 `old`/`new` 匹配
- 对 `.srt` 去掉每行行号前缀、改为单行匹配（因为 SRT 相邻条目间是序号+时间码，无法直接拼接跨行）

修改后重跑 `scripts/process.py`。

### 规则分类建议

把规则按"通用 vs 专用"分组放在 `REPLACEMENTS` 顶部，便于维护：

- **通用规则**：对其他草稿也适用（词语、句式、错别字形态等）
- **专用规则**：与具体草稿主题/工具名绑定（仅当前草稿命中）

orca 草稿（18 条命中）的拆分示例（已写入 `process.py` 顶部注释）：

**专用（11 条，工具名/上下文绑定）**：
- 工具名 ASR 错字：Vscode/VOCA/尔卡 → Orca（5 条）
- 工具名 ASR 错字：Helix → Herdr（1 条）
- 工具名 ASR 错字：MACDOWN → Markdown（1 条）
- 跨行条目 043-044、085-086：Liner → Linear（3 条）
- 单行条目 016、042、078：Git Hub / Github → GitHub（3 条，跨行 085-086 已含 Git Hub→GitHub）

**通用（7 条，其他草稿也适用）**：
- 错别字：制定 → 定制（058）
- 错别字：work tree → worktree（090、094）
- 错别字：work（多余）删除（089）
- 错别字：功能 → agent（074，上下文相关但模式可复用）
- 断句碎片：跨行 047-048 主语切换
- 跨行条目 47-48 合并

通用规则应该沉淀到 skill 默认清单，专用规则按需追加。

### 专用名词规范化（MFlash 草稿沉淀）

ASR 出来的模型名/产品名几乎总是大小写与空格错乱，应当**按官方写法统一**，
而不是逐条「看起来没问题就放过」。MFlash 草稿的规范写法：

| ASR 出现的写法 | 规范写法 |
| --- | --- |
| `Deepseek` / `Deepseek4.1Flash` / `Deepseek V4.1的Flash1017` | `DeepSeek V4.1 Flash` / `DeepSeek V4.1 Flash 1017` |
| `Minimax` / `Mini Max` / `迷你Max` / `M3 Flash` / `M3Flash` | `MiniMax M3.1 Flash` |
| `GRM` / `GM5.3Flash` / `GLM5.3Flash` | `GLM 5.3 Flash` |
| `三bench` | `Fun Bench` |
| `three点js` | `three.js` |

规则写法：品牌级的大小写/空格统一用 `{'lines': [], ...}` 全局替换；
需要补全版本号（如 `M3 Flash` → `M3.1 Flash`）或删除多余词（如「人物的和场景」）
的，用带行号的精确规则，并在 `note` 里写明推断依据。

**注意**：全局替换规则之间有先后顺序 —— 精确规则里的 `old` 必须写成
「前序全局规则已经执行完之后」的文本，否则匹配不上。

### 参赛选手标签大小写

评测类视频的字幕里，参赛选手标签统一用大写 `A/B/C/D`。

`process.py` 的 `fix_contestant_labels()` 用带上下文的正则处理，**只改文字**：

| 语境 | 规则 | 示例 |
| --- | --- | --- |
| 字母紧跟「选手」 | `(?<![A-Za-z])([abcd])(?=选手)` | `a选手` → `A选手` |
| 「选手」紧跟字母 | `(?<=选手)([abcd])(?![A-Za-z])` | `选手a` → `选手A` |
| 字母紧跟「和」 | `(?<![A-Za-z])([abcd])(?=和)` | `c和d` → `C和d` |
| 「和」紧跟字母 | `和([abcd])(?![A-Za-z])` | `b和c` → `b和C` |

该步必须放在**精确替换之后**执行：精确规则的 `old` 里写的是小写标签
（如 `c选手画面和人物比a`），先做大写化会让它匹配不上。

## 已知陷阱

1. **CRLF 换行**：剪映 json 在 Windows 上是 `\r\n`，脚本内已做归一化处理。
2. **「其他」一词**：含「他」字，必须保留。脚本用占位符隔离。
3. **跨行条目**：
   - `.txt` 格式：相邻条目用 `\n<行号>  <文本>` 分隔，匹配模式要包含行号
   - `.srt` 格式：相邻条目用 `\n\n<序号>\n<时间码>\n<文本>` 分隔，**不能用 `\n\n` 拼接跨行**——脚本会自动改为单行匹配
4. **空字幕**：少数片段 `content.text` 为空，已自动跳过。
5. **草稿已被剪映同步**：如果用户在剪映里手动改过字幕，原始错误可能已不存在。
6. **多文本轨道**：一个草稿可以含多条文本轨道（正文 + 角标/标签），`extract_subtitles` 已改为遍历全部轨道并合并去重。早期版本只取第一个轨道，会漏掉绝大多数字幕。
7. **剪映帧对齐**：`target_timerange.start` 和 `duration` 不是精确毫秒，会有 ±666us（30fps 下约一帧）的偏差。`write_back.py` 用位置一一对应而非精确时间码匹配，避开此问题。
8. **跨行条目匹配跨格式**：见 §"扩展审查清单"，`apply_replacements` 已处理。
9. **固定错误**：`.fixed.srt` 与 `.fixed.txt` 必须**完全等价**（除行号格式）。`write_back.py` 会校验这一致性。
10. **⭐ 改字会打乱字幕样式（不是换行符的问题）**：
    剪映每条字幕的 `content.styles[0].range` 是**字符区间**（正常情况下恒等于 `[0, len(text)]`）。
    只改 `content.text` 而不同步区间，剪映就只对区间内的字符套用样式、其余字符回落成模板默认样式，
    表现为「从某条字幕开始样式和前后不一致」（第一条被改动的字幕 = 症状起点）。
    `write_back.py` 的 `sync_style_ranges()` 会把整条覆盖区间同步成 `[0, len(新文本)]`。
11. **`materials.texts[].words` 会与文本脱节**：
    这是 ASR 的逐词时间轴（`text` / `start_time` / `end_time`），改字后不再对应新文本。
    剪映的逐字校对面板会读到它。`write_back.py` 的 `remap_words()` 用 `difflib` 对齐
    旧→新字符区间，重新切分词并同步时间轴；整段被删的词连同其时间一起丢弃，
    插入的字符并入前一个词。三个数组长度始终一致，校验不过则原样保留。

## 反向灌回剪映（细节）

**完整步骤**：

1. **关闭剪映**（必须；剪映运行时会覆盖你的修改）
2. 运行 `scripts/write_back.py "<草稿目录>"`
3. 脚本会自动：
   - 备份原 `draft_content.json` → `draft_content.json.review-backup`
   - 修改 `materials.texts[].content.text` 字段（保留其它字段和键顺序）
   - **同步 `content.styles[].range` 与新文本长度**（否则字幕样式只覆盖前 N 个字，样式会被打乱）
   - **同步 `materials.texts[].words` 逐词时间轴**（否则逐字校对面板仍是旧文本）
   - 校验全部匹配 + 检查常见 ASR 错字残留
4. 重启剪映核对

**回滚**：把 `.review-backup` 改回 `draft_content.json`。

详细字段说明见 [references/jianying-schema.md](references/jianying-schema.md)。

## 资源

### scripts/
- `process.py` — 端到端处理：提取 + 全局他→它 + 精确清单 + 通用检测 + 生成审查清单
- `write_back.py` — 把 `<草稿名>.fixed.srt` 灌回 `draft_content.json`

### references/
- `jianying-schema.md` — 剪映 `draft_content.json` 字段说明、时间单位、常见陷阱

## 验证清单

每次运行后必须确认：
- [ ] SRT 文件条目数 = 字幕片段数
- [ ] SRT 结构合法（每 3 行 + 空行：序号 / 时间码 / 文本）
- [ ] 残留「他」= 0（排除「其他」）
- [ ] 审查清单的「合计」= 实际命中数
- [ ] 通用检测项需人工复核（标注「疑似」）
- [ ] 如执行 write_back：写回后残留 ASR 错字（Vscode/VOCA/MACDOWN/Liner/Git Hub/Github/尔卡 等）= 0
