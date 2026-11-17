# 豆包 TTS 接口说明与排错

本文档对应 `scripts/doubao_tts.py` 的实现。
协议取自火山引擎官方《单向流式语音合成(HTTP)》文档（2026-11 核对）。
音色克隆见 [voice-clone.md](voice-clone.md)。

## 一、怎么拿到凭证

**API Key**：火山引擎控制台 → 语音技术 → 豆包语音 → 接入管理 → API Key。
新控制台是单个 `X-Api-Key`；旧控制台是 `X-Api-App-Key` + `X-Api-Access-Key`。
本 skill 用新控制台的 `X-Api-Key`。

**音色 ID**：控制台 → 豆包语音 → 音色库 → 选中音色 → 复制音色 ID。

- 预置音色：形如 `zh_female_vv_uranus_bigtts`，配 `--resource-id seed-tts-2.0`。
- **复刻音色**（本 skill 默认）：声音复刻接口注册出来的 speaker ID，配 `--resource-id seed-icl-2.0`（默认值）。
  没有复刻音色时，先用 `voice_clone.py` 注册一个。

**resource-id 与 model 的对应**：

| resource-id | model | 音色类型 |
| --- | --- | --- |
| `seed-icl-2.0`（默认） | `seed-tts-2.0-standard` / `seed-tts-2.0-expressive` | 声音复刻 2.0 注册的音色 |
| `seed-tts-2.0` | 不传 | 预置音色 |

`model` **只对声音复刻 2.0 生效**，枚举值就上面两个。
传了 `model` 的请求**不支持 `context_texts` 语音指令**（脚本会直接拦下并报错）。

### 配置放 .env

凭证不用每次带在命令行上，放 `.env` 里，改完直接生效。

```bash
python scripts/doubao_tts.py --config --work-dir <work>
```

这条命令会在 `<work>/.env` 生成模板并打印当前生效的配置（密钥打码）：

```
当前生效的豆包配置（优先级：命令行 > 环境变量 > .env 就近优先）：
  --api-key      = sk-a****7890   ← D:\work\.env
  --speaker      = my_cloned_voice   ← D:\work\.env
  --resource-id  = seed-icl-2.0   ← D:\work\.env
  --model        = auto   ← 默认值
```

`.env` 模板内容：

```ini
DOUBAO_API_KEY=sk-xxxxxxxx
DOUBAO_SPEAKER=my_cloned_voice
DOUBAO_RESOURCE_ID=seed-icl-2.0
DOUBAO_MODEL=auto
```

**搜索顺序（就近优先）**：

1. `<work>/.env`
2. 从工作目录逐层向上（项目根、其上级）
3. `~/.outline-to-video/.env`（跨项目全局配置）
4. skill 目录下的 `.env`（本机复用兜底）

**优先级**：命令行参数 > 进程环境变量 > `.env` 文件。

解析规则：支持 `#` 注释、空行、`export ` 前缀、单双引号包裹的值。

**安全提醒**：`.env` 里是明文密钥，别提交到 git。
skill 目录下的 `.env` 已被 skill 目录的 `.gitignore` 忽略；
打包请用 `scripts/pack.py`，它会排除 `.env`（skill-creator 自带的打包脚本不会排除，会把密钥打进分发件）。

## 二、协议

**端点**：`POST https://openspeech.bytedance.com/api/v3/tts/unidirectional`

**请求头**：

| 头 | 必填 | 说明 |
| --- | --- | --- |
| `X-Api-Key` | 是 | 控制台拿到的 API Key |
| `X-Api-Resource-Id` | 是 | `seed-icl-2.0` 或 `seed-tts-2.0` |
| `Content-Type` | 是 | `application/json` |
| `Connection` | 是 | `keep-alive` |
| `X-Control-Require-Usage-Tokens-Return` | 否 | 设为 `*` 时返回计费字符数 |

**请求体**：

```json
{
  "req_params": {
    "text": "要合成的文本",
    "speaker": "复刻音色的 speaker ID",
    "model": "seed-tts-2.0-standard",
    "additions": "{\"disable_markdown_filter\": true}",
    "audio_params": {
      "format": "mp3",
      "sample_rate": 24000,
      "speech_rate": 0,
      "pitch": 0,
      "enable_subtitle": true
    }
  }
}
```

**响应**：HTTP Chunked，每行一个 JSON。

```json
{
  "code": 0,
  "message": "OK",
  "data": "<base64 音频分片>",
  "sentence": {
    "text": "你好，语音测试",
    "words": [
      {"word": "你", "startTime": 0.195, "endTime": 0.335, "confidence": 0.89}
    ]
  },
  "usage": {"text_words": 7}
}
```

- `code == 0`：这一行带音频分片，把所有 `data` 按顺序 base64 解码后拼接就是完整音频。
- `code == 20000000`：结束。
- `code > 0`：错误，`message` 是原因。

`sentence.words` 是**字级时间戳**（秒），字幕就靠它生成。

### 流式响应的实际结构（实测）

一次请求返回两种记录混在同一个流里：

| 记录 | 特征 | 内容 |
| --- | --- | --- |
| 音频分片 | `data` 非空，`sentence` 为空 | 每片约 3KB mp3，按顺序拼接即完整音频 |
| 时间戳记录 | `data` 为空（0 字节），`sentence.text` 非空 | 该句的字级时间戳 |

关键点：

- **时间戳是「相对整段音频」的绝对时间**，不是分片相对时间。
  逐句累加偏移会把时间放大（本项目实测放大 2.25 倍，字幕会严重跑偏）。
- 时间戳记录和音频分片的**数量不对应**：一句音频可能横跨十几个分片，几十个分片之后才吐一条时间戳。
- 句末会有一条 `sentence.text` 为整段全文、`words` 为空的记录，它是全文汇总。
- 有些请求会返回 `sentence.text` 完整但 `words` 为空的中间记录，别当成错误。

脚本的处理方式：时间戳原样收集；只有当某条记录的时间**出现回退**（小于已累计的最大 end）时，才判定它是分片相对时间并补偏移。
另外落盘后会用 ffprobe 量真实音频时长做兜底——若末字时间超出音频，按比例缩放，保证字幕永不越过音频结尾。

### 已知缺陷：中英混排的字级时间戳不可靠（实测）

**现象**：文本里含拉丁字母/数字时，那些词元的字级时间戳是错的。

实测「高级功能四块：Agent、MCP、3D 游戏开发、Blender 编辑。」返回：

```
'高'      0.415 ~ 0.575     ← 汉字：准确
'块：'    1.125 ~ 1.155
'Agent、' 2.365 ~ 2.395     ← 拉丁：时长只有 30ms
'MCP、'   4.215 ~ 4.245     ← 真正发声的时间被丢进了词元间的空洞
```

规律：

- 含拉丁字母/数字的词元，`startTime~endTime` 只有 **30~40ms**；
- 它真正的发声时长变成了前后词元之间的**空洞**；
- 汉字词元的时间戳是准的；
- 比例串最严重：「1:1、16:9、9:16、4:3、3:4」会被拆成 `1:` `1、` `16:` `9、` `9:` `16、` …
  交替错位，**顺序和区间都对不上**，词元级算法救不回来。

**后果**：直接拿这些时间戳切字幕会出现两种可见缺陷：

1. 说话过程中字幕消失（实测成片 203 处空档，累计 148s）；
2. 30~90ms 的闪烁字幕条（实测成片 20 处）。

**处理**（`scripts/word_align.py`，`doubao_tts.py` 已自动调用）：

1. 用 `silencedetect` 从音频本身切出发声区间（永远可靠）；
2. 汉字词元按 API 时间锚定到区间，再用保序回归保证区间序号单调；
3. 拉丁词元按朗读顺序平均分配到左右锚点之间的空区间；
4. 区间内按字符权重摊分时长，词元首尾相接。

阶段 7 直接用**校正后的词元时间戳**给字幕行定时：
相邻词元首尾相接，正好铺满每一段发声，所以字幕在人说话时不会消失，
行与行的先后也一定等于口播顺序。
`regions` 作为兜底——若有哪一段发声没被覆盖到，就把前一条字幕行延伸过去。

> `tts/index.json` 里额外保存了 `api_words`（API 原始时间戳）与 `regions`（发声区间）。
> `api_words` 让重复校正保持幂等；`regions` 供阶段 7 兜底使用。

### 时间戳的两种坐标系（混用必错）

`tts/index.json` 里同一句有两份时间戳，坐标系不同：

| 字段 | 坐标系 | 取值范围 |
| --- | --- | --- |
| `api_words` | **句内相对秒** | `0 ~ 该句时长` |
| `words` | **节内绝对秒** | 累加到整节结束 |

增量重配音（只重合成改动的句子）时若把 `api_words` 又减了一次句首偏移，
时间会变成负数（实测 97 句里有 84 句越界），
词元会被指到错误的发声区间上，字幕顺序整个错乱。

复用旧配音的两个硬规矩：

1. `api_words` 原样搬运，不要加减；
2. 校验 `min(start) >= 0` 且 `max(end) <= 句子时长`，越界说明这份数据已经坏了，
   必须重新合成，不能拿来对齐。

同理，`align_words.py` 校正时始终以 `api_words` 为输入——
拿对齐后的 `words` 再对齐一次，「可信/不可信」的判定就会漂移，结果不再幂等。

## 三、和 Edge TTS 预览稿的关系

豆包按字计费，口播稿在用户确认之前通常要改好几轮。
**确认前用免费的 Edge TTS 出试听稿，确认后才用豆包合成正式版**，
细节见 `edge-tts.md`。

`tts/index.json` 里的 `engine` 字段标记这批音频的来源
（`edge-tts` / `doubao`）。正式配音会整篇覆盖预览索引，
`scripts/tts_patch.py` 也会拒绝复用预览音频——音色会串，这条是硬编码的守卫。

## 四、TTS 2.0 与 ICL 2.0 的能力差异

| 功能 | `seed-tts-2.0` | `seed-icl-2.0` |
| --- | --- | --- |
| 适用音色 | 预置音色 | 声音复刻注册的音色 |
| 字级时间戳（`enable_subtitle`） | 支持 | **支持** |
| SSML | 仅支持标签 | 仅支持标签 |
| 语音指令（`context_texts`） | 支持 | **不支持**（与 `model` 互斥） |
| AIGC 生成标识 | 支持 | 支持 |
| Latex 公式朗读 | 支持 | 支持 |
| 发音字典 `tone` | 支持 | 支持（限中英文音色） |

结论：换到复刻音色后，**唯一损失的是语气指令**。
字幕（字级时间戳）照常工作，所以本 skill 的字幕管线不受影响。

## 五、参数速查

**audio_params**

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `format` | `mp3` | `mp3` / `wav` / `pcm` / `ogg_opus` |
| `sample_rate` | 24000 | 采样率 |
| `speech_rate` | 0 | 语速，范围 [-50, 100]，100 = 2 倍速，-50 = 0.5 倍速 |
| `pitch` | 0 | 音调，范围 [-12, 12] |
| `loudness_rate` | 0 | 音量增减 |
| `bit_rate` | 64000 | mp3 比特率 |
| `enable_subtitle` | - | 开启后返回字幕信息 |

**additions**（注意是 JSON **字符串**，不是对象）

| 字段 | 说明 |
| --- | --- |
| `disable_markdown_filter` | 设为 true 时服务端会解析并去掉 Markdown 语法 |
| `disable_emoji_filter` | 去掉 emoji |
| `enable_latex_tn` | 公式朗读 |
| `context_texts` | 语气指令，如 `["请说慢一些"]`；**复刻音色不支持** |
| `tone` | 发音字典，`原词/修正内容` 形式，最多 5000 条 |
| `explicit_language` | 指定语种，如 `zh` / `en` |
| `explicit_dialect` | 方言：`beijing` / `dongbei` / `henan` / `shaanxi` / `shanghai` / `sichuan` / `tianjin` / `yue` |
| `enable_auto_language_recognition` | 自动识别语种，默认 false |

## 六、本 skill 的用法

```bash
# 自检（先跑这个，确认 key 和音色可用）
python scripts/doubao_tts.py --doctor --work-dir work

# 批量配音（读 script.json 的每节 vo，按句合成，产出 work/tts/）
python scripts/doubao_tts.py --script work/script.json --out-dir work/tts --work-dir work

# 换成表现力增强版
... --model expressive

# 语速 / 音调
... --speech-rate -5 --pitch -1
```

凭证默认读 `.env`（见上文「配置放 .env」），命令行参数只是临时覆盖。

脚本会：

- 按中文标点切句（单句 ≤ 60 字），逐句合成，避免长文本被截断。
- 合成前剥离 Markdown 记号和 emoji，防止被念出来。
- 记录每句的时长与字级时间戳到 `work/tts/index.json`。
- 失败自动重试 3 次（指数退避），仍然失败则报错退出，不静默降级。

## 七、排错

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| HTTP 401 / 403 | API Key 无效、过期，或 `resource-id` 与账号开通的资源不一致 | 重新复制 Key；确认音色类型与 `resource-id` 匹配 |
| HTTP 400，message 提到 speaker | 音色 ID 写错、属于别的账号、或未授权 | 从音色库重新复制；复刻音色要确认训练已完成（status 2/4） |
| HTTP 404 | 端点或 resource-id 不对 | 核对端点与 resource-id |
| `InvalidModel` | `model` 取值不在枚举里 | 只能用 `seed-tts-2.0-standard` / `seed-tts-2.0-expressive` |
| `speaker not found` | 音色没训练好或 ID 填错 | 用 `voice_clone.py query` 查状态；要 status 2/4 |
| 报 context_texts 相关错误 | 复刻音色不支持语音指令 | 去掉 `--tone` |
| code > 0 且 message 提到超长 | 单次文本过长 | 脚本已按 60 字切句；仍超长就缩短该节口播 |
| 音频里念出了"星号" | 文本里有 Markdown | 脚本会自动剥离，同时源头别写 Markdown |
| 字幕整体跑偏、越来越晚 | 时间戳被当成相对时间累加偏移 | 已修复；确认用的是当前版本脚本 |
| 语速太快/太慢 | 默认语速 | `--speech-rate`，-10 约慢 10% |
| 音色念错多音字 | 发音字典缺失 | 用 `additions.tone` 发音字典，或在 `vo` 里换同义词规避 |

排查时先看 `work/logs/tts.log`，里面记录了每次请求与错误。
音色本身的问题（训练失败、试听效果差）看 [voice-clone.md](voice-clone.md)。
