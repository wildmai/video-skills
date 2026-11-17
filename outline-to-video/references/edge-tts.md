# Edge TTS 预览配音

正式配音用豆包声音复刻，但口播稿在用户确认之前往往要改好几轮。
豆包按字计费，每轮都全量合成就是重复付钱。

**先用免费的 Edge TTS 出试听稿，确认后再用豆包合成正式版**——
试听阶段花钱为 0，正式阶段只付一次。

## 一、怎么用

```bash
# 出试听稿
python scripts/tts_preview.py --work-dir <work>

# 换音色 / 调语速
python scripts/tts_preview.py --work-dir <work> \
    --voice zh-CN-YunxiNeural --rate +8% --pitch +0Hz

# 只看会合成哪些句子，不真的合成
python scripts/tts_preview.py --work-dir <work> --dry-run
```

需要 `pip install edge-tts`（开源免费，不需要 API Key、不限次数）。

产物：

| 文件 | 内容 |
| --- | --- |
| `tts/index_preview.json` | 预览索引，和豆包**完全同一套结构**（见 `schemas.md`），下游脚本一行不用改 |
| `tts/preview/s*.mp3` | 逐句音频 |
| `tts/preview/preview_vo.mp3` | 整篇合并音频，方便从头听一遍 |
| `preview.mp4` | 剪辑好的预览片（带字幕、已质检） |
| `timeline_preview.json` / `subtitles_preview.ass` | 预览片的时间线与字幕 |

### 预览配音必须和正式配音分目录

**这条踩过，别改。**

预览音频和正式音频的**文件名完全相同**（`s5_004.mp3`），
内容却不同（音色不一样）。放同一个目录会互相覆盖。

实测后果：预览跑完，正式配音的 97 个音频文件**全被换成了预览音色**，
而 `tts/index.json` 还指着原来的时长。
索引记 12.79s、文件只有 8.76s，成片上就出现 4.78 秒的空洞——
配音全错位，而且不报错。

现在的防线有三层：

1. `tts_preview.py` 默认写到 `tts/preview/` 子目录；
2. 显式把 `--out-dir` 指到 `tts/` 时**直接报错**；
3. `build_timeline.py` 会核对每个音频文件的真实时长与索引是否一致，
   对不上就打 warning 并警告「时间线不可信」。

第 3 条是关键——它能在渲染前就发现音频被换过。

## 二、中文音色

| 音色 | 特点 |
| --- | --- |
| `zh-CN-XiaoxiaoNeural` | 女声，温暖亲和，通用首选（默认） |
| `zh-CN-YunxiNeural` | 男声，青年，节奏偏快 |
| `zh-CN-XiaoyiNeural` | 女声，活泼 |
| `zh-CN-YunyangNeural` | 男声，播音腔，稳 |
| `zh-CN-YunjianNeural` | 男声，浑厚 |
| `zh-CN-YunxiaNeural` | 女声，童声感 |
| `zh-CN-liaoning-XiaobeiNeural` | 东北口音 |
| `zh-CN-shaanxi-XiaoniNeural` | 陕西口音 |

语速 `--rate`：`-20%` ~ `+20%`（`+10%` 更快）。
音调 `--pitch`：`-10Hz` ~ `+10Hz`。

## 三、预览片

默认会连着把预览片渲出来（音频 → 时间线 → 渲染 → 质检，一条命令走完）：

```bash
python scripts/tts_preview.py --work-dir <work>
```

| 参数 | 作用 |
| --- | --- |
| `--no-render` | 只出音频，不渲片（想先听一遍再用） |
| `--preview-out` | 预览片路径，默认 `preview.mp4` |
| `--crf` / `--preset` | 默认 22 / veryfast，出片快一倍，文字照样看得清 |

预览片和正式片的产物**完全分开**，不会覆盖你已经做好的正式成片。
用户看预览片就能同时判断：文案对不对、节奏顺不顺、画面跟口播对不对得上。

## 四、和豆包的关系

`tts/index.json` 里有一个 `engine` 字段标明这批音频是谁合成的：

| engine | 来源 |
| --- | --- |
| `edge-tts` | 预览稿，免费 |
| `doubao` | 正式版，豆包声音复刻 |

**这条守卫是硬编码的**，不用担心把预览稿当成正式配音：

- `doubao_tts.py` 整篇覆盖索引，`engine` 变成 `doubao`；
- `tts_patch.py` 发现旧索引是 `edge-tts`，会**逐句拒绝复用**并用豆包重合成，
  日志里会打「旧音频来自 edge-tts 预览稿，改用豆包重合成」。

## 五、字级时间戳：Edge 的坑

Edge TTS 的 `Communicate` 默认只吐**句级**边界（`boundary="SentenceBoundary"`），
一个句子一条。要拿词级边界必须显式传 `boundary="WordBoundary"`：

```python
comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch,
                             boundary="WordBoundary")
async for chunk in comm.stream():
    if chunk["type"] == "WordBoundary":
        # chunk["text"] / chunk["offset"] / chunk["duration"]（100 纳秒为单位）
```

中文也能拿到词边界，但有两个坑：

1. **边界文本不含标点**——"遍" 而不是 "遍。"；
2. **粒度与豆包不同**——"Hi3D" 被拆成 "Hi" / "3" / "D"，而 "接下来" 反而是一个词元。

直接拿边界文本当字幕，会丢标点、也和原文对不上。

处理办法（`word_align.align_tokens_to_boundaries`）：
先在原文里定位每个边界覆盖的字符区间，
再把 `word_align.tokenize_for_timing` 切出来的词元（逐字/整词，文本与原文严格一致）
映射上去——起点取第一个相交边界的起点，终点取最后一个相交边界的终点。

拿到时间戳后仍要走一遍 `word_align.align_words`，
用音频里的真实发声区间校正，和豆包那条路共用同一套逻辑。

实测：97 句中文口播，发声区间未覆盖 0 处，字幕文本与原文 100% 一致。

## 六、限制

- **音色不是用户的复刻音色**，只是试听，不能当最终交付。
- 依赖能连到微软语音服务的网络；不通时会报
  「Edge TTS 合成失败」，此时可以换音色重试，或直接退回豆包。
- 语速与音色跟豆包复刻音色不一样，
  **试听稿的总时长不等于成片时长**（同稿实测 Edge 581.8s vs 豆包 531.3s）。
  所以时间线要按各自的配音重新算，别拿试听稿的时长去承诺成片时长。
- 脚本名故意叫 `tts_preview.py` 而不是 `edge_tts.py`：
  后者会和 `edge-tts` 库同名，运行时 `import edge_tts` 会把脚本自己导进来，
  报「module 'edge_tts' has no attribute 'Communicate'」。

## 七、想给用户听两版音色

分别跑两次、把第二次的音频另存即可：

```bash
python scripts/tts_preview.py --work-dir <work> --voice zh-CN-XiaoxiaoNeural
cp tts/preview_vo.mp3 tts/preview_vo_female.mp3
python scripts/tts_preview.py --work-dir <work> --voice zh-CN-YunxiNeural
cp tts/preview_vo.mp3 tts/preview_vo_male.mp3
```
