# 野码AI的视频创作工具箱

一套面向科技教程、AI 工具实测和大模型测评的 Agent Skills。
给 Claude Code、Codex 等 Agent 一个选题、一份稿件或一批录屏，就可以从对应的创作环节开始。
工具箱覆盖选题大纲、口播分镜、稿件验收、网页演示、录屏剪辑、字幕校对和封面设计。

[野码AI的 B 站个人空间](https://space.bilibili.com/402098977)

## Skills 列表

| Skill | 作用与价值 | 可以这样对 Agent 说 |
| --- | --- | --- |
| [`viral-video-creator`](viral-video-creator/SKILL.md) | 把科技选题逐步整理成大纲、口播稿、分镜、制作清单和标题封面方案，每阶段确认后再继续 | “帮我策划一条面向新手的 B 站 AI 编程工具教程，先做选题和大纲” |
| [`model-benchmark-video`](model-benchmark-video/SKILL.md) | 查阅官方模型卡，按能力分层设计同题实测与评分标准，让横评结果可复现 | “我要对比模型 A 和模型 B，先用官方资料搭测评框架，再设计可复跑的同题题库” |
| [`viral-video-reviewer`](viral-video-reviewer/SKILL.md) | 检查现有大纲、脚本或口播稿的开场钩子、结构、节奏和画面安排，给出有原文依据的验收报告和可直接替换的文案 | “按制作规范评审 `口播稿.md`，标出问题原句，给我可直接替换的版本” |
| [`auto-tutorial-studio`](auto-tutorial-studio/SKILL.md) | Agent 演示真实操作，你用 OBS 等工具录屏，录完交给 Agent 按口播剪辑、加动效与转场并检查画面遮挡 | “按这份大纲演示软件的导入和导出，我用 OBS 录，录完把文件交给你剪成 MP4” |
| [`outline-to-video`](outline-to-video/SKILL.md) | 根据大纲匹配已有录屏片段、撰写口播，先用 Edge TTS 做试听，确认后用豆包配音并渲染 MP4 | “用 `大纲.md` 和 `录屏/` 做一条 5 分钟讲解视频，先给我口播稿和试听” |
| [`web-video-presentation`](web-video-presentation/SKILL.md) | 把文章或口播稿做成点击推进的 16:9 网页演示，适合边讲边操作或录屏，可选合成分段配音 | “把 `文章.md` 做成可以点击推进的 16:9 网页演示，先给我口播稿和章节计划” |
| [`jianying-subtitle`](jianying-subtitle/SKILL.md) | 从剪映草稿导出带时间码的 SRT，识别常见错字、重复词和断词，交付修订版字幕及审查清单，也可写回草稿 | “从这个含 `draft_content.json` 的剪映草稿导出并校对字幕，先给我修订版 SRT” |
| [`youtube-viral-thumbnail-designer`](youtube-viral-thumbnail-skill/SKILL.md) | 从视频结论中提炼点击理由，用主视觉和真实证据支持封面文案，兼顾手机缩略图的可读性 | “根据这段实测结论和产品截图，给我一个 YouTube 封面方案和出图提示词” |

测评视频可以先用 `model-benchmark-video` 定框架和题库，再用 `viral-video-creator` 写口播和分镜，最后用 `viral-video-reviewer` 审稿。
审稿时可以让 Agent 使用本项目随附的[视频制作规范](viral-video-creator/references/production-spec.md)。

## 快速开始

### 一键安装

为你使用的客户端任选一种方式，即可全局安装全部 8 个技能。

#### Claude Code

**Skills CLI**（需要 Git 和 Node.js 22.20 或更高版本）

```sh
npx --yes skills add https://github.com/wildmai/video-skills --global --agent claude-code --skill "*" --yes --copy
```

**Git Clone**（Windows 使用 Git Bash）

```bash
git clone --depth 1 https://github.com/wildmai/video-skills.git ~/.claude/video-skills && mkdir -p ~/.claude/skills && cp -R ~/.claude/video-skills/*/ ~/.claude/skills/
```

#### Codex

**Skills CLI**（需要 Git 和 Node.js 22.20 或更高版本）

```sh
npx --yes skills add https://github.com/wildmai/video-skills --global --agent codex --skill "*" --yes --copy
```

**Git Clone**（Windows 使用 Git Bash）

```bash
git clone --depth 1 https://github.com/wildmai/video-skills.git ~/.agents/video-skills && mkdir -p ~/.agents/skills && cp -R ~/.agents/video-skills/*/ ~/.agents/skills/
```

若只想安装到当前项目，在该项目目录运行 Skills CLI 命令，并去掉 `--global`。[Skills CLI 文档](https://github.com/vercel-labs/skills/blob/main/docs/commands.md#skills-add)

### 调用技能

安装后打开 Claude Code 或 Codex，在对话输入框里调用技能。

| Agent | 全局技能目录 | 调用示例 |
| --- | --- | --- |
| Claude Code | `~/.claude/skills/` | `/viral-video-creator 帮我为 AI 编程入门视频写大纲` |
| Codex | `~/.agents/skills/` | `$viral-video-creator 帮我为 AI 编程入门视频写大纲` |

客户端文档见 [Claude Code Skills](https://code.claude.com/docs/en/skills) 和 [Codex Skills](https://learn.chatgpt.com/docs/build-skills)。

### 从本地目录直接使用

克隆项目或下载 ZIP 后解压，在 Claude Code、Codex 或其他能读取本地文件的 Agent 中打开包含本 README 的目录。
把下方示例发给 Agent，并按需要替换技能路径和任务内容。

```text
请读取 ./viral-video-creator/SKILL.md 和它引用的制作规范。
我想做一条给新手看的 B 站视频，主题是用 AI 工具重构一个小项目，时长 6 分钟。
先把选题和视频大纲保存到当前项目，等我确认后再写口播稿。
```

## 按需准备运行环境

大纲、口播和审稿流程可以直接从对话开始。
有素材处理、网页开发或成图需求时，按所选技能准备依赖。

- **剪映字幕（`jianying-subtitle`）**建议使用剪映专业版 5.9（5.9 Pro），并准备 Python 和包含 `draft_content.json` 的剪映草稿目录。
  其他版本的草稿格式可能不同，使用前可先尝试导出 SRT，确认字幕能正常读取后再写回草稿。
- **实操教程制作**的窗口控制脚本依赖 Windows，还需准备外部录屏工具、Python、FFmpeg 和 FFprobe，以及配音与 OCR 所用的 Python 依赖。
- **已有录屏剪辑**需要 Python、FFmpeg 和 FFprobe，试听使用 Edge TTS，正式配音需要豆包 API Key 和音色 ID。
- **网页演示**的脚手架需要 Bash、Node.js 和 npm，配音按所选服务配置。
- **封面成图**需要 Agent 环境提供图像生成工具，文字方案和出图提示词可直接在对话中完成。

## 许可证

本项目自有内容采用 [MIT 许可证](LICENSE)。
`web-video-presentation` 来自 [ConardLi 的 garden-skills](https://github.com/ConardLi/garden-skills/tree/main/skills/web-video-presentation)，随附[上游 MIT 许可证](web-video-presentation/LICENSE)。
