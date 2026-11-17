---
name: outline-to-video
description: 把「一份视频大纲 + 一个素材视频目录」做成完整的知识/干货讲解成片：清点并转写素材视频、按大纲匹配相关片段、编写带钩子与节奏设计的口播脚本、用免费的 Edge TTS 出试听稿等用户确认，确认后再用豆包声音复刻配音并渲染输出 MP4。当用户说"按大纲做视频""把这些录屏剪成讲解视频""根据大纲配音出片""大纲出片""outline to video"时使用。
---

# outline-to-video

把散在文件夹里的录屏/演示素材，按用户给的大纲剪成一条**有钩子、有节奏**的知识讲解视频，配豆包 TTS 旁白，输出 MP4。

**产出的成片是干货/教学片**：旁白驱动、画面作证据、前 8 秒留住人、每节都有结论。
不是素材混剪，也不是把大纲念一遍。

## 何时用

- 用户给了大纲（或目录）+ 素材视频路径，要做成一条讲解视频。
- 典型场景：课程录屏整理、技术分享、工具教程、把长录屏剪成知识短片。

不适用：卡点混剪、需要真人出镜、需要复杂动效包装的片子。

## 硬规则

1. **脚本必须经用户确认后才能做正式配音和渲染。**
   把 `script.md` 完整给用户看，明确问"可以按这版做吗"，然后停下来等回复。
   用户改了就改，改完重新确认，不要自己往下推进。

   **确认前一律用免费的 Edge TTS 试听，确认后才用豆包声音复刻正式配音。**
   豆包按字计费，而口播稿在确认前往往要改好几轮——
   每轮都拿豆包合成就是重复付钱。试听稿用 Edge TTS，不花钱、不限次、几秒出，
   足够验证「文案对不对、节奏顺不顺、画面配不配得上」。
   详见 `references/edge-tts.md`。
2. **不臆造素材。**
   所有 `clips` 必须来自 `inventory.json` 里的素材 ID 和真实时长，且要在联系表里亲眼确认过画面在讲什么。
   素材里没有的内容，如实告诉用户没有，并给出两条路：补录素材，或把这节改成纯口播卡点。
3. **不手搓 ffmpeg。**
   该跑的脚本就跑脚本；要加能力就改脚本，不要在会话里拼 ffmpeg 命令。
4. **出错先查日志。**
   每个阶段在 `<work>/logs/<阶段>.log` 留了完整日志，包括 ffmpeg 的 stderr 尾部。
5. **质检不通过要修，不要只报告。**
   时长对不上、黑帧、长静音、定格画面，都要回到脚本或素材层解决，然后重跑。
6. **不要替用户决定内容，只替用户干活的活。**
   口播稿、画面选段、节奏设计都是你的活；素材范围、主题、结论由用户拍板。

## 阶段 0：输入与环境

从用户输入里能拿到的都不要问。
只问拿不到的，一次问完：

- **豆包凭证**（API Key + 音色 ID）。
  走 `.env` 配置，让用户填一次就行，不用每次带参数：

  ```bash
  python scripts/doubao_tts.py --config --work-dir <work>
  ```

  这条命令会在 `<work>/.env` 生成模板，并打印当前生效的配置（密钥打码）。
  让用户把 `DOUBAO_API_KEY` 和 `DOUBAO_SPEAKER` 填进去即可，改完直接生效。
  默认用**复刻音色**，模型 `seed-icl-2.0`。
  用户还没有复刻音色时，向他要一段参考音频（10-30 秒干净人声 + 对应原文），用 `voice_clone.py` 注册一个；
  训练要几分钟，可以和写脚本并行推进，细节见 `references/voice-clone.md`。
- **目标时长**（没给就按大纲条数估算，并在脚本里说明这个估算）。
- **画幅**（默认横屏 1920x1080@30；用户要竖屏就 1080x1920）。

配置优先级：**命令行参数 > 进程环境变量 > `.env`（离工作目录越近的优先）**。
`.env` 搜索顺序：`<work>/.env` → 逐层向上（项目根等）→ `~/.outline-to-video/.env`（全局）→ skill 目录下的 `.env`。

环境自检：

```bash
ffmpeg -version                       # 需要 libx264 / aac / subtitles(libass)
python -c "import requests, PIL"      # 渲染与联系表
python -c "import faster_whisper"     # 没有也能跑，但转写必须外挂字幕
```

工作目录（后面所有 `--work-dir` 都指它）：

```
<work>/
├── .env                豆包配置（API Key / 音色 ID，别提交）
├── inventory.json      素材清单          ├── script.json    脚本（机器读）
├── transcripts/        每个素材的转写     ├── script.md      脚本（人读，确认用）
├── frames/             联系表 + 分镜表    ├── tts/           正式配音 + 字级时间戳
├── candidates.json     大纲 ↔ 素材候选    ├── tts/preview/   预览配音（Edge，免费）
├── outline_map.md      候选可读报告       ├── timeline.json  渲染时间线
├── black_regions.json  素材里的黑帧区间   ├── subtitles.ass  字幕
├── logs/               阶段日志           ├── out.mp4        正式成片
└──                                    ├── preview.mp4     预览片（阶段 5b）
```

素材目录同级建 `<项目名>_work`；用户指定了就用用户的。

## 阶段 1：清点素材

```bash
python scripts/probe_media.py --media-dir <素材目录> --work-dir <work>
```

产出 `inventory.json`。
**先读它**：素材总数、总时长、有没有横竖屏混杂、有没有外挂字幕，决定后面怎么做。

## 阶段 2：转写内容

```bash
python scripts/transcribe.py --work-dir <work> [--model small|medium|large-v3] [--hf-mirror]
```

有同名外挂字幕就用字幕，没有就本地 faster-whisper 转写（CPU 上 `small` 约 3 倍实时，`medium` 更准但更慢）。
下载模型慢或失败时加 `--hf-mirror` 走国内镜像。

产出 `transcripts/<id>.json` 与 `.txt`。
**必须读转写全文**，这是判断"素材里到底讲了什么"的唯一可靠依据。

## 阶段 3：看画面

```bash
python scripts/extract_frames.py --work-dir <work> [--interval 6]
```

产出 `frames/<id>/sheet_XXX.jpg`（联系表，每格带时间戳）和 `frames/<id>/shots.json`（镜头切点）。

**用 Read 工具看联系表**，这是唯一能看到画面的方式。
挑片段时先看联系表确认画面在干嘛，再去转写里对齐语义。

## 阶段 4：大纲 ↔ 素材匹配

```bash
python scripts/match_outline.py --work-dir <work> --outline <大纲文件>
```

产出 `candidates.json` 与 `outline_map.md`。

`status` 为 `sparse` / `miss` 的条目不要硬套。
先读转写全文确认素材里有没有讲这件事，没有就如实报告缺口。

匹配不准时，可以在用户的大纲里给条目加 `{#关键词1, 关键词2}` 标注再重跑。

## 阶段 5：写脚本并确认

**这是唯一需要你创造内容的阶段。**

先读 `references/script-craft.md`（结构、钩子、节奏、语言规则）和 `references/shot-selection.md`（怎么取片）。

然后写两个文件：

- `work/script.json`：机器读，格式见 `references/schemas.md`。
- `work/script.md`：人读，用 `assets/script.template.md` 的结构，给用户确认用。

写完必须校验：

```bash
python scripts/validate_script.py --work-dir <work> --script <work>/script.json
```

有 ERROR 就改到通过。校验会挡住这些毛病：

| 报错 | 为什么是错 |
| --- | --- |
| 口播 N 句 vs 画面 M 个 | 必须一一对应，否则画面与口播整体错位（实测最大偏 6.3s） |
| 某句里有「；！？」 | 它是断句点，一句话会变成两句，和画面对不上 |
| 锚点落在素材黑帧区 | 成片上会闪黑，换个锚点 |
| 锚点往后素材不够 | 不够的部分只能定格，画面会停住 |

**然后停下来。**
把 `script.md` 的内容完整展示给用户，说明总时长、用了哪些素材、有没有缺口，然后明确问一句"确认按这版制作吗？"
等用户回复确认（或提出修改并改完再确认）之后，才进入阶段 6。

**不过在问用户之前，先出试听稿**——见阶段 5b。
让用户能一边看文字稿、一边听声音，比干看文字稿更容易提出有效意见，
而且这一步不花钱。

## 阶段 5b：预览配音 + 预览片（Edge TTS，免费）

一条命令出全套试听稿：**音频 + 剪辑好的 MP4 + 质检**。

```bash
python scripts/tts_preview.py --work-dir <work> \
    [--voice zh-CN-XiaoxiaoNeural] [--rate +0%] [--pitch +0Hz] [--no-render]
```

产物：

| 文件 | 内容 |
| --- | --- |
| `tts/index_preview.json` | 预览配音索引（**和豆包同一套结构**） |
| `tts/preview/preview_vo.mp3` | 整篇合并音频 |
| `preview.mp4` | 剪辑好的预览片（带字幕、已质检） |
| `timeline_preview.json` / `subtitles_preview.ass` | 预览片的时间线与字幕 |

**预览片和正式片的产物完全分开**，不会覆盖你已经做好的正式成片。

> ⚠ **预览配音必须写进 `tts/preview/` 子目录，不能和正式配音共用 `tts/`。**
> 两边文件名相同（`s5_004.mp3`）内容却不同，共用目录会互相覆盖——
> 实测踩过：正式配音的 97 个音频被全换成预览音色，
> 索引还指着旧时长，成片配音错位 4.78 秒且不报错。
> 脚本已经做了三层防线（独立目录 + 显式指定 `tts/` 直接报错 +
> 编排时间线时核对音频时长），但**别去改这些防线**。

预览片用 `veryfast` 预设、CRF 22：出片快一倍，文字照样看得清，
够判断「画面配不配得上口播」。

要点：

- **不要 API Key、不计费、不限次数**，中文有 8 个音色（`zh-CN-XiaoxiaoNeural` 默认）。
- 索引里记 `"engine": "edge-tts"`。阶段 6 的豆包配音写进 `tts/`（正式目录），
  和预览的 `tts/preview/` 各归各的；
  `tts_patch.py` 也会拒绝复用预览音频（音色会串）——这条守卫是硬编码的，放心跑。
- Edge 对中文的词边界粒度与豆包不同（"Hi3D" 拆成 "Hi"/"3"/"D"）、且不含标点，
  脚本会把它套回自己的词元切分上，保证字幕文本与原文一字不差。
- 试听稿的音色不是用户的复刻音色，**只是试听**，不要拿它当最终交付。
- 用户看预览片就能同时判断三件事：文案对不对、节奏顺不顺、
  **画面跟口播对不对得上**——对不上就回去改 `clips` 的锚点，改完重跑本阶段。
- 改一句文案想再听一遍？重跑本阶段即可，仍然免费。

细节见 `references/edge-tts.md`。

## 阶段 6：豆包 TTS 配音（正式版）

默认用 `seed-icl-2.0`（声音复刻 2.0）合成复刻音色。

```bash
# 先自检（凭证默认读 .env，不用带 --api-key / --speaker）
python scripts/doubao_tts.py --doctor --work-dir <work>

# 批量配音
python scripts/doubao_tts.py --script <work>/script.json --out-dir <work>/tts \
    --work-dir <work> [--model standard|expressive]
```

复刻音色**不支持语音指令**（`--tone` 会被脚本直接拦下报错），语气只能靠文案本身控制。
要语气指令就改用预置音色：`--resource-id seed-tts-2.0 --tone "..."`。

还没有复刻音色时，先注册（训练需要几分钟，状态到 `Success`/`Active` 才能合成）：

```bash
python scripts/voice_clone.py register --audio <参考音频> --text "参考音频里的原文" \
    --speaker-id <音色代号> --work-dir <work>
python scripts/voice_clone.py wait --speaker-id <音色代号> --work-dir <work>
```

产出 `tts/index.json`，含每句的时长与**字级时间戳**（字幕就靠它）。
**这一步会整篇覆盖阶段 5b 的预览配音**（索引里的 `engine` 从 `edge-tts` 变成 `doubao`），
正式成片用的永远是豆包复刻音色。

配音是按句合成的，所以某句不对可以单独重配，不用整篇重来：

```bash
# 只重合成「文变了」的句子，其余复用已有音频与时间戳
python scripts/tts_patch.py --work-dir <work> [--dry-run]
```

`script.json` 的 `vo` 改完后跑它；它按句子文本对齐新旧版本，
只有真正改动的句子才调 API，其余原样复用。

> **字级时间戳有坑，脚本已自动处理。**
> 豆包对中英混排文本的 word 时间戳不可靠：凡含拉丁字母/数字的词元
> （`Agent`、`3D`、`MCP`、`1:1`…）时长会被压成 30~40ms，
> 真正的发声时间变成词元之间的空洞，直接拿去切字幕会出现
> 「说话时字幕消失」和「字幕一闪而过」。
> `doubao_tts.py` 已用音频本身把这些时间戳校正回来
> （实现见 `scripts/word_align.py`），并额外保存：
> - `api_words`：API 原始时间戳，**句内相对秒**，供重复校正时复用（保证幂等）
> - `regions`：该句的发声区间，阶段 7 用它给字幕行定时
>
> **时间戳有两种坐标系，混用必错**（实测踩过）：
> `api_words` 是句内相对秒（0 ~ 句长），`words` 是节内绝对秒（累加到整节）。
> 增量重配音时若把 `api_words` 再减一次句首偏移，词元时间会变成负数，
> 词元会被指到错误的发声区间上，字幕顺序整个错乱。
> 复用旧配音务必原样搬运 `api_words`，并校验
> `min(start) >= 0` 且 `max(end) <= 句子时长`，越界就说明这份数据坏了，要重新合成。
>
> 已生成过配音、想重新校正时间戳时：
> ```bash
> python scripts/align_words.py --work-dir <work>
> ```

## 阶段 7：编排时间线

```bash
# 先扫素材里的黑帧（录屏掉帧造成的纯黑），扫一次约几分钟，结果会缓存
python scripts/scan_black.py --work-dir <work>

python scripts/build_timeline.py --work-dir <work> \
    --script <work>/script.json --tts-index <work>/tts/index.json
```

`scan_black.py` 把每个素材里的黑帧区间写进 `black_regions.json`。
编排时间线时会自动避开这些区间——镜头取到黑帧就截断，剩下的时长定格补齐，
成片上不会闪过一屏黑。日志里会报告哪些镜头被截断，
**那些镜头最好换一个锚点**（同内容在别处往往也有）。

产出 `timeline.json` 与 `subtitles.ass`。

**第 i 个画面严格配第 i 句口播**：镜头时长 = 该句配音时长，
画面从锚点（`clip.start`，即该内容出现在屏幕上的那一刻）开始取，
源素材往后不够就往前挪，都不够才定格。

这是「画面跟着口播走」的前提。
早先的做法是把整节的口播时长按各画面自然长度比例摊下去，
但各句配音长短差别很大（1.5s ~ 13s），画面边界就会和句子边界越走越偏——
实测最大偏 6.3s，观众听到「选好目录，一路下一步」时屏幕上已经在放下一段了。

所以写脚本时**一句口播配一个画面**（`vo` 的行数 = `clips` 的个数），
并让 `clips[i].start` 指向该句要讲的那个内容的出现时刻。
数量对不上时会打 warning，此时画面与口播无法一一对应。

画面不够就顺延取材源后续，源用尽才定格（会打警告，说明这段该补画面了）。
**日志里的 warning 要转达给用户**。

字幕行的切分规则（`build_timeline.py`）：

- **按显示宽度切，不按字数切**：一个汉字算一个单位，拉丁字母算 0.55、
  数字算 0.55、空格 0.3。一行最多 22 个单位。
  按字符个数切会把「Nano-Banana 有 Pro 和 2 两版」这种其实很短的行
  误判成超长，于是在词中间硬断行。
- **断点要落在词与词之间**：标点是天然断点；「的」「地」这类后缀不能起头，
  「把」「和」这类介词连词不能留在行尾。带惩罚项的均衡切分
  会自动避开这两种别扭断法。
- **时间直接取词元时间戳**：词元时间戳已经按发声区间校正过，
  相邻词元首尾相接，正好铺满每一段发声，所以字幕在人说话时不会消失，
  行与行的先后也一定等于口播顺序。
- **字幕连续显示**：每条字幕一直显示到下一条出现。
  TTS 在每个标点后会停 0.6~0.9s，若让字幕跟着语音断开，
  字幕会以约 1Hz 的频率闪烁，看着像故障而不是字幕。
- `regions` 里有任何一段发声没被覆盖到，兜底把前一条字幕行延伸过去。

## 阶段 8：渲染

```bash
python scripts/render_video.py --work-dir <work> --timeline <work>/timeline.json \
    --subtitles <work>/subtitles.ass --out <work>/out.mp4 \
    [--bgm <音乐路径>] [--bgm-volume 0.08] [--threads 4] [--ken-burns 0]
```

产出 MP4（H.264 + AAC、烧录字幕、响度归一到 -16 LUFS）。

- `--threads`：x264 编码线程数，默认 4。多核机器上线程太多会把内存吃光，
  报 `malloc of size ... failed`。脚本失败时会自动降级重试（4→2→1）。
- `--ken-burns`：默认 0。ffmpeg 的 `crop` 对浮点坐标取整，
  开运镜会变成阶梯式跳变，观感上是画面抖动；文字界面保持 0。

## 阶段 9：质检

```bash
python scripts/qc_video.py --work-dir <work> --video <work>/out.mp4 --timeline <work>/timeline.json
```

检查时长、分辨率、音轨、响度、黑帧、长静音、长定格。
有 FAIL 必须修（回脚本或补素材），修完重跑渲染和质检。

最后把成片路径、时长、命中警告一起报给用户。

## 参考资料

| 文件 | 什么时候读 |
| --- | --- |
| `references/script-craft.md` | 写脚本前必读 |
| `references/shot-selection.md` | 挑画面片段时读 |
| `references/edge-tts.md` | 出试听稿、调音色/语速、或预览稿出问题时读 |
| `references/doubao-tts.md` | 配音报错或要调语气/语速时读 |
| `references/voice-clone.md` | 注册复刻音色、查训练状态、克隆报错时读 |
| `references/schemas.md` | 写 `script.json` 时对照字段 |
| `references/ffmpeg-recipes.md` | 渲染报错时排障 |
| `assets/script.template.md` | 写 `script.md` 的骨架 |
| `assets/outline.template.md` | 用户大纲写得含糊时给他参考 |
| `assets/env.example` | `.env` 配置模板（`--config` 会自动生成可直接填的 `.env`） |

## 自检与打包

```bash
python scripts/smoke_test.py     # 用合成素材跑通全链路（配音用本地合成音替代）
python scripts/pack.py           # 打包成 .skill（自动排除 .env / logs / __pycache__）
```

打包**不要**用 skill-creator 自带的 `package_skill.py`：它会把目录里所有文件塞进包，
包括 `.env`，等于把用户的 API Key 打进分发件。
`scripts/pack.py` 会排除 `.env`、`logs/`、`__pycache__/`。
