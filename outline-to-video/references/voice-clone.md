# 音色克隆（声音复刻）

对应 `scripts/voice_clone.py`。
官方接口：[音色注册](https://docs.volcengine.com/docs/6561/2534906)、[音色查询](https://docs.volcengine.com/docs/6561/2535742)。

## 一、前置：账号要开通服务

1. 火山引擎控制台 → 语音技术 → 豆包语音 → **开通管理**，开通 **豆包声音复刻模型 2.0**。
2. 用**后付费音色**的话，还要单独开通后付费音色服务。
3. 新版控制台（`console.volcengine.com/speech/new`）才有单 `X-Api-Key` 鉴权；旧版控制台即将下线，别用。

## 二、完整流程

```bash
# 1) 注册音色（用一段参考音频）
python scripts/voice_clone.py register \
    --audio <参考音频.wav/mp3/m4a> \
    --text "参考音频里念的原文" \
    --speaker-id my_voice_01 \
    --demo-text "这是试听文本" \
    --work-dir work

# 2) 等训练完成（Training=1 → Success=2 / Active=4）
python scripts/voice_clone.py wait --speaker-id my_voice_01 --timeout 300 --work-dir work

# 3) 查状态，顺带把试听音频存下来
python scripts/voice_clone.py query --speaker-id my_voice_01 --save-demo --work-dir work

# 4) 用这个音色合成
python scripts/doubao_tts.py --script work/script.json --out-dir work/tts --work-dir work
```

API Key 从 `.env` 读（见 [doubao-tts.md](doubao-tts.md) 的「配置放 .env」），
注册成功后把 `speaker_id` 填进 `.env` 的 `DOUBAO_SPEAKER`，后面就不用再带参数了。

合成阶段默认就用 `seed-icl-2.0`，不用额外指定。

## 三、参考音频要求

| 项 | 要求 |
| --- | --- |
| 格式 | wav / mp3 / ogg / m4a / aac / pcm（pcm 仅 24k 单通道） |
| 大小 | ≤ 10MB |
| 时长 | 建议 10-30 秒，本 skill 默认截断到 30 秒 |
| 内容 | 单一说话人、清晰人声、无背景音乐、尽量无混响 |
| 文本 | 强烈建议同时提供参考文本（`--text`），服务端做 WER 比对，差异过大会返回 `45001109` |

脚本会自动把音频转成 24k 单声道 wav 再上传。

**降噪**：音频噪声大时加 `--denoise`（会损失声音细节）；音频干净时不要开。

## 四、后付费音色的命名

后付费音色走 `custom_speaker_id`：

```json
{
  "speaker_id": "custom_speaker_id",
  "custom_speaker_id": "custom_zh_xxx"
}
```

命令行上就是 `--custom-speaker-id custom_zh_xxx`（脚本会自动把 `speaker_id` 设成固定值）。

**命名规范**：

- 8~256 个字符，只允许数字、大小写字母、`-`、`_`。
- 必须以英文字母开头，首尾不能是 `-` 或 `_`。
- 同账号下不能重名。
- 不能撞官方前缀/后缀（`S_`、`ICL_`、`MIX_`、`DiT_`、`BV` 开头，或 `_bigtts`、`_tob`、`_streaming` 结尾等），会被系统拦截。

预付费音色直接用控制台给的 `speaker_id`，不需要 `custom_speaker_id`。

## 五、状态机

`query` / `wait` 返回的 `status`：

| 值 | 名称 | 能否用于合成 |
| --- | --- | --- |
| 0 | NotFound | 否 |
| 1 | Training | 否 |
| 2 | Success | **可以** |
| 3 | Failed | 否，换音频重来 |
| 4 | Active | **可以** |

`model_type = 5` 表示声音复刻 2.0。

## 六、计费陷阱（重要）

> **首次调用合成接口即视为"转正"并收取音色槽位费。**

也就是说：注册完先别急着正式合成。
先用 `query --save-demo` 听一遍试听音频，满意了再合成正式内容。
试听音频只在 Success 状态下返回，且 **1 小时内有效**。

## 七、错误码对照

| code | 含义 | 处理 |
| --- | --- | --- |
| 45000010 | Invalid X-Api-Key | Key 无效或过期，重新复制 |
| 45001001 | 请求参数有误 | 检查字段是否齐全、格式是否符合约束 |
| 45001101 | 音频上传失败 | 网络/超时，重试；确认音频大小与格式 |
| 45001102 | ASR 转写失败 | 音频质量差，换更清晰的人声 |
| 45001104 | 声纹检测未通过 | 触发敏感声纹或相似度过高，换音频或换说话人 |
| 45001105 | 获取音频数据失败 | base64 或数据为空，检查上传内容 |
| 45001107 | SpeakerID 未找到 | speaker_id 写错或已删除 |
| 45001108 | 音频转码失败 | 格式不支持或文件损坏，换格式 |
| 45001109 | WER 检测错误 | 参考音频与 `--text` 对不上，改文本或换音频 |
| 45001112 | SNR 检测错误 | 换信噪比更高的音频 |
| 45001113 | 降噪失败 | 去掉 `--denoise` 或换音频 |
| 45001114 | 音频质量较差 | 换音频 |
| 45001122 | ASR 未检测到人声 | 音频里没人声或人声太弱 |
| 45001123 | 达到上传次数上限 | 该 speaker_id 的复刻次数用完了，换一个 |
| 45001124 | ASR 文本审核拒绝 | 音频内容触发审核，换素材 |
| 45001125 | demo 文本审核拒绝 | 改 `--demo-text` |
| 45001126 | demo 文本长度错误 | `--demo-text` 需 4-300 字 |
| 45001127 | prompt 音频审核拒绝 | 参考音频触发审核，换素材 |
| 45001128 | prompt 音频文本审核拒绝 | 音频对应文本触发审核，换素材 |

排查时看 `work/logs/voice_clone.log`。
