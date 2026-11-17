# 文件契约

工作目录（`--work-dir`）里各文件的结构。
所有 JSON 都是 UTF-8、缩进 2。

## work/inventory.json（阶段 1 产出）

```json
{
  "generated_at": "2026-11-10T09:20:00",
  "media_dir": "D:/素材",
  "video_count": 3,
  "total_duration": 612.4,
  "videos": [
    {
      "id": "v001_intro",
      "name": "intro.mp4",
      "stem": "intro",
      "path": "D:/素材/intro.mp4",
      "rel_path": "../../素材/intro.mp4",
      "duration": 62.5,
      "width": 1920,
      "height": 1080,
      "fps": 30.0,
      "rotation": 0,
      "has_audio": true,
      "audio_codec": "aac",
      "size_mb": 48.2,
      "subtitle_sidecar": "D:/素材/intro.srt"
    }
  ]
}
```

`id` 是脚本里引用素材的唯一标识，格式 `v{序号}_{文件名}`。

## work/transcripts/<id>.json（阶段 2 产出）

```json
{
  "id": "v001_intro",
  "source": "whisper:small",
  "language": "zh",
  "duration": 62.5,
  "segments": [{"start": 0.0, "end": 3.2, "text": "..."}],
  "full_text": "..."
}
```

`source` 取值：`sidecar:srt`（用了外挂字幕）、`whisper:<模型名>`、`no_audio`（无音轨）。

## work/candidates.json（阶段 4 产出）

```json
{
  "title": "大纲标题",
  "items": [
    {
      "id": "o1",
      "order": 1,
      "level": 2,
      "text": "大纲条目原文",
      "keywords": ["步骤", "结论"],
      "status": "ok",
      "candidates": [
        {"video": "v001_intro", "start": 12.0, "end": 27.5, "score": 8.9,
         "coverage": 0.75, "hits": ["结论"], "excerpt": "转写原文片段"}
      ]
    }
  ]
}
```

## work/script.json（阶段 5 由 Claude 撰写）

这是核心契约，渲染全靠它。

```json
{
  "title": "成片标题",
  "target_duration": 300,
  "meta": {"width": 1920, "height": 1080, "fps": 30},
  "sections": [
    {
      "id": "s1",
      "outline": "对应的大纲条目原文",
      "role": "hook",
      "vo": "这一节的完整口播文本。",
      "note": "给用户/剪辑看的说明，不参与配音。",
      "clips": [
        {"video": "v001_intro", "start": 12.0, "end": 18.5,
         "note": "画面在做什么、为什么选它", "muted": true}
      ]
    }
  ]
}
```

字段规则：

| 字段 | 必填 | 约束 |
| --- | --- | --- |
| `title` | 是 | 成片标题 |
| `target_duration` | 否 | 目标时长（秒），用于校验偏差 |
| `meta.width/height/fps` | 否 | 默认 1920/1080/30 |
| `sections[].id` | 是 | 全局唯一 |
| `sections[].outline` | 建议 | 必须能对应到大纲里的某一条 |
| `sections[].role` | 是 | `hook`/`point`/`example`/`payoff`/`cta`/`transition` |
| `sections[].vo` | 是 | 只写说出口的话，不写画面描述和提示词 |
| `sections[].clips[].video` | 是 | 必须是 inventory 里的 id |
| `sections[].clips[].start` | 是 | 秒，**锚点**：该内容出现在素材里的那一刻，必须落在该素材时长内 |
| `sections[].clips[].end` | 是 | 秒，名义标记（通常是 `start + 1`）；镜头实际长度由这句口播的时长决定 |
| `sections[].clips[].note` | 建议 | 说明这个画面在干嘛 |
| `sections[].clips[].muted` | 否 | 默认 `true`（盖掉素材原声）；设 `false` 保留原声 |

`clips` 按顺序播放，**一个 clip 对应一句口播**（见下）。

**`vo` 的行数必须等于 `clips` 的个数**——第 i 句口播配第 i 个画面，
时间线上镜头时长严格等于该句配音时长。
数量对不上时画面与口播会整体错位（各句长短不一时最坏能偏 6 秒以上）。

`clips[i].start` 是**锚点**：该画面要讲的内容出现在源素材里的那一刻。
镜头从锚点开始取，按这句口播的时长往后延。

## work/tts/index.json（阶段 5b 预览 / 阶段 6 正式产出）

```json
{
  "engine": "doubao",
  "speaker": "my_voice_01",
  "resource_id": "seed-icl-2.0",
  "model": "seed-tts-2.0-standard",
  "total_duration": 298.4,
  "total_chars": 1372,
  "sections": {
    "s1": {
      "duration": 12.8,
      "sentences": [
        {"index": 1, "text": "句子文本", "audio": "D:/work/tts/s1_001.mp3",
         "duration": 4.2, "start": 0.0, "end": 4.2,
         "words": [{"word": "你", "start": 0.19, "end": 0.33}],
         "api_words": [{"word": "你", "start": 0.19, "end": 0.33}],
         "regions": [[0.15, 4.05]]}
      ]
    }
  }
}
```

同一句里有两份字级时间戳，坐标系不同，混用必错：

| 字段 | 坐标系 | 取值范围 |
| --- | --- | --- |
| `words` | **节内绝对秒**（累加到整节） | `0 ~ 该节时长` |
| `api_words` | **句内相对秒** | `0 ~ 该句时长` |

`api_words` 是 API 原始值，重复校准时必须以它为输入，否则不幂等。
`regions` 是该句的发声区间（由音频本身测得，永远可靠），字幕定时用它兜底。

`engine` 标明这批音频是谁合成的：`edge-tts` 是阶段 5b 的免费试听稿，
`doubao` 是正式版。正式配音会整篇覆盖索引，`tts_patch.py` 也会拒绝复用
`edge-tts` 的音频（音色会串）。

**两套音频必须分目录**：正式的在 `tts/`，预览的在 `tts/preview/`。
文件名相同（`s5_004.mp3`）内容却不同，放一起会互相覆盖。
`build_timeline.py` 会核对每个音频文件的真实时长与索引是否一致，
不一致就打 warning——这是发现「音频被换掉」的最后一道防线。

`audio` 是**绝对路径**。索引一旦生成就绑定了具体文件，
移动或重命名音频文件后必须重新生成索引。

## work/timeline.json（阶段 7 产出）

```json
{
  "meta": {"title": "成片标题", "width": 1920, "height": 1080, "fps": 30,
           "duration": 298.4, "audio_duration": 298.4},
  "shots": [
    {"id": "s1_1", "section": "s1", "video": "v001_intro", "src": "D:/素材/intro.mp4",
     "in": 12.0, "out": 18.5, "duration": 6.5, "freeze": 0.0,
     "note": "", "muted": true, "start": 0.0}
  ],
  "audio": [
    {"section": "s1", "files": ["D:/work/tts/s1_001.mp3"],
     "sentences": [{"audio": "D:/work/tts/s1_001.mp3", "start": 0.0, "duration": 4.2}],
     "start": 0.0, "duration": 12.8}
  ],
  "subtitles": [{"start": 0.19, "end": 2.4, "text": "字幕行"}],
  "warnings": []
}
```

`freeze` 是该镜头末尾需要定格补时长的秒数（画面不足口播时长时的兜底）。
`warnings` 是编排阶段产生的问题，交人处理。

## work/subtitles.ass（阶段 7 产出）

libass 字幕文件，渲染时用 `subtitles` 滤镜烧录。
默认样式：微软雅黑、白字黑描边、底部居中、每行最多 18 字。
