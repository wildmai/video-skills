# 剪映草稿文件结构参考

剪映桌面版（JianyingPro）的草稿是一个**目录**，核心文件是 `draft_content.json`。
本 skill 通过解析该文件来提取字幕轨道。

## 目录结构

```
<草稿名>/
├── draft_content.json     ← 主数据（字幕时间线、素材引用）
├── draft_meta_info.json   ← 元信息（草稿名、原始视频、创建时间）
├── draft_extra            ← 时间戳文件
├── draft_settings         ← 用户偏好
├── common_attachment/     ← 草稿内的通用素材
├── Resources/             ← 字体、模板、特效资源
└── .backup/               ← 备份
```

## draft_content.json 顶层字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 草稿唯一 ID |
| `duration` | int | 总时长（微秒） |
| `fps` | float | 帧率，默认 30 |
| `canvas_config` | object | 画布 `{width, height, ratio}` |
| `tracks` | array | 轨道列表 |
| `materials` | object | 素材字典 |
| `materials.texts` | array | 文字素材数组（字幕正文） |

## 轨道结构（`tracks`）

每条轨道有 `type` 字段：
- `video`：视频轨道
- `audio`：音频轨道
- `text`：文字轨道 ← **字幕所在轨道**

文字轨道结构：

```json
{
  "type": "text",
  "segments": [
    {
      "id": "9B7C0EEF-...",
      "material_id": "5DD658E1-...",  ← 指向 materials.texts 中的文字素材
      "target_timerange": {
        "start": 2400000,              ← 起始时间（微秒）
        "duration": 1300000            ← 持续时间（微秒）
      }
    }
  ]
}
```

## 文字素材（`materials.texts[]`）

每个文字素材包含：

```json
{
  "id": "5DD658E1-...",
  "type": "subtitle",
  "content": "{\"styles\":[...], \"text\":\"我平时开发用过很多\"}",
  "words": {                          ← 词级时间戳（剪映 ASR 字幕会带）
    "text": ["我", "平时", "开发", ...],
    "start_time": [0, 280, 480, ...],  ← 毫秒
    "end_time": [280, 480, 760, ...]
  },
  "font_size": 5.0,
  "font_path": "D:/Program File/JianyingPro/.../zh-hans.ttf"
}
```

**关键点**：`content` 字段是 **JSON 字符串**，需要二次解析才能拿到真正的字幕文本：

```python
content_obj = json.loads(text_mat['content'])
text = content_obj['text']
```

## 时间单位

剪映内部使用**微秒（1/1_000_000 秒）**：
- `1 秒 = 1_000_000 微秒`
- SRT 时间码（`HH:MM:SS,mmm`）= 毫秒精度

转换示例：

```python
def us_to_srt(us: int) -> str:
    total_ms = us // 1000
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
```

## SRT 输出格式

```
1
00:00:02,400 --> 00:00:03,700
我平时开发用过很多

2
00:00:03,700 --> 00:00:05,266
综合能力比较强的AI工具

…
```

每个字幕条目占 3 行 + 1 空行：序号、时间码（`-->`分隔）、文本、空行。

## 常见陷阱

1. **换行符**：剪映在 Windows 上保存的 json 是 **CRLF (`\r\n`)**，脚本必须做归一化处理，否则跨行字符串匹配会失败。
2. **空字幕**：少数片段的 `content.text` 为空，需要跳过。
4. **重复「他」字**：「其他」一词中嵌入了「他」字，全局替换时需用占位符隔离：
   ```python
   HE_PLACEHOLDER = '\x00HE\x00'
   protected = text.replace('其他', HE_PLACEHOLDER)
   fixed = protected.replace('他', '它').replace(HE_PLACEHOLDER, '其他')
   ```
5. **跨行修改**：相邻条目之间用换行 + 行号（`006  `）分隔，跨行精确替换需包含下一行的行号：
   ```python
   old = '说是拆任务写代码做\n007  素材做可视化都能干'
   ```

## 验证方式

提取后应做以下验证：
- 字幕条目数与 `len(text_track['segments'])` 一致
- 每条字幕都有非空文本
- SRT 文件按 3 行 + 空行结构组织
- 末尾时间码不超过视频总时长

## 反向工程：把改正后字幕灌回剪映

使用 `scripts/write_back.py`：

```bash
python scripts/write_back.py "<草稿目录>" --output-dir "<process.py 输出目录>"
```

写回脚本自动：
1. 备份原文件为 `draft_content.json.review-backup`（剪映自带的 `.bak` 之外的另一份独立备份）
2. 合并所有 type=text 轨道的 segment，按 `target_timerange.start` 升序排序
3. 与 `fixed.srt` 按位置一一对应
4. 修改 `materials.texts[].content.text`（保留其它字段与键顺序）
5. 校验全部匹配 + 检查常见 ASR 错字残留

**关键点**：
- 必须在**关闭剪映**后运行
- 写回脚本的"匹配"按位置而非精确时间码，因为剪映有帧对齐（±666us @ 30fps）

## 进阶话题

### 多文本轨道并存

一个草稿可能含多条 type=text 轨道：
- 轨道 0：正文字幕（111 条 ASR 文本）
- 轨道 1：角标/广告位（1 条短文本）

`extract_subtitles` 已实现：**遍历全部文本轨道并合并去重**（按 start_us 升序）。

早期版本用 `next()` 取第一个文本轨道，会漏掉绝大多数字幕。

### 帧对齐（Frame Snapping）

剪映把 `target_timerange.start` 和 `duration` 对齐到帧边界。在 30fps 下偏差最大为：
- `1 / fps = 1/30 s = 33333 us ≈ 33ms`
- 实际观察值通常为 ±666us（约 1/1500s）

**写回时不能用精确时间码匹配**，要用：
- 各文本轨道 segment 合并后按 start 排序
- 与 `fixed.srt` 行按位置一一对应（相同长度序列）

### 跨行条目格式差异

跨行精确替换条目（如 174-175 行合并）在不同格式下的形态：

**`.txt` 格式**（process.py 用）：
```
174  旧文本
175  续文本
```
→ 匹配模式：`旧文本\n175  续文本`

**`.srt` 格式**（同样 process.py 输出，但用 `apply_replacements(is_srt=True)`）：
```
174
00:02:54,000 --> 00:02:57,000
旧文本

175
00:02:57,000 --> 00:03:00,000
续文本
```
→ **不能**用 `\n\n` 拼接两段文本作为匹配模式（中间隔着序号+时间码）

`apply_replacements` 自动处理：对 `.srt` 把跨行拆为单行逐条匹配。

### 文本素材的 content 字段

`materials.texts[].content` 是 **JSON 字符串**（不是对象），需要二次解析：

```python
content_obj = json.loads(text_mat['content'])
text = content_obj['text']
```

修改时也要保持 JSON 字符串形态：用 `json.dumps(obj, ensure_ascii=False)` 重建，键顺序会丢失（Python 3.7+ dict 是有序的，但 json.dumps 默认按当前顺序）。

### 固定错误速查（写回后必须清零）

以下子串在改正后**必须全部为 0**：

| 子串 | 正确写法 | 触发场景 |
| --- | --- | --- |
| `Vscode` / `VOCA` / `尔卡` | `Orca` | 工具名 ASR 错字 |
| `MACDOWN` | `Markdown` | 与同上下文自相矛盾 |
| `Liner` | `Linear` | 项目管理工具真名 Linear |
| `Git Hub` / `Github` | `GitHub` | 官方拼写大小写 |
| `work tree` | `worktree` | git worktree 是复合名词 |
| `Helix` | `Herdr` | 工具名 ASR 错字 |

`write_back.py` 内置 `KNOWN_BAD_SUBSTRINGS` 检查，并在末尾报告残留数。