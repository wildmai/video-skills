---
name: auto-tutorial-studio
description: 给定网站地址、软件名称和一份粗略章节大纲，由技能驱动真实操作、由用户用外部录屏工具采集，再对采集到的视频做逐句对位剪辑、动效叠加、转场、BGM 压低、逐帧遮挡校验并导出可播放成片。用于制作实操教程视频。只写口播稿用 tutorial-voiceover；需要人工把关每一步、或者由技能自己录屏用 tutorial-video-recorder。
---

# 自动化教程视频工作室

给定一个网站地址或软件名称，再给一份粗略的章节顺序，本技能负责：

**驱动真实操作 → 通知用户录制 → 导入采集素材 → 建立可检索索引 → 逐句对位剪辑 → 动效与转场 → 逐帧校验 → 成片**

## 分工：技能做什么，用户做什么

| | 谁来做 | 说明 |
|---|---|---|
| 录制画面 | **用户** | 用户用 OBS / ShareX / 系统录屏等外部工具录，技能**不录屏**。外部录制保留原生分辨率与清晰度，也能录进系统声音 |
| 真实操作 | **技能** | 技能用浏览器 CDP 或窗口自动化完成每一步，并记录操作位置 |
| 剪辑、配音、动效、校验 | **技能** | 全部自动化，带闸门 |
| 素材入库 | **用户** | 操作结束后把录好的视频给技能（路径即可） |

技能**不占用用户的硬件鼠标**：浏览器操作走 CDP 元素点击，不移动真实指针。

## 七条铁律

1. **采集归用户，技能不录屏。** 技能只负责操作与后期。用户录完必须把文件交给技能。
2. **操作归技能，录制窗口归用户。** 技能在用户宣布「开始录制」后才动手，操作结束立刻通知用户停止录制。
3. **录制期间屏幕上不能有控制台。** 技能自己的每一条命令都必须无窗口执行，
   并且开启 `guard` 把新弹出的控制台和浮层立刻藏掉——它们都会被录进去。
4. **只藏窗口，不关窗口。** 用 `SW_HIDE`，用 `guard stop` 统一恢复。
5. **每一镜绑定一句口播。** `plan.json` 里每个 beat 声明它服务于第几句口播，章节总长必须等于该章口播总长。
6. **念出来的数字必须从真实界面读到。** 顶点数、价格、版本号一律以界面为准。
7. **遮挡审计是闸门，不是报告。** 构建前逐帧 OCR 出片窗口，发现控制台 / 通知 / 启动页就**拒绝出片**。
   BGM 要给人声让位，成片 −16 LUFS、真峰 ≤ −1.5 dBFS。

## 阶段 0 · 立项

确认三件事，缺一不可：

- **目标**：网站地址或桌面软件的名称、版本。
- **大纲**：章节顺序（用户给粗略的就行，细节由技能补）。
- **录制方案**：用户用什么录屏工具、录哪块显示器、是否录系统声音、输出格式与分辨率。

同时确定**采集范围**（录整屏还是某个窗口）和**是否分段录制**。
一段一功能的多次录制最好维护；一次录全流程也可以，靠索引定位。

## 阶段 1 · 写稿

`build/script.md`，每章一个 `## ` 标题，格式是 `## <章节ID> | <显示标题>`：

```markdown
## ch00 | 开场

先看我做出来的东西。

这个手办是从一张图生成的。

## ch01 | 注册与登录

官网是 hi3d.ai，浏览器打开就能用。
```

- **章节ID** 只能是小写字母、数字、下划线、连字符（TTS 清单要求），必须与 `plan.json` 的章节 `id` 完全一致。
  同一章的多段自动编成 `ch01`、`ch01_1`、`ch01_2`。
- **显示标题** 给章节卡用，可以是中文。
- 章节ID 之间不要互为前缀（`ch0` 与 `ch01` 会串），建议统一用 `chNN`。
- 开头先给成果（最有辨识度的画面），再回到操作。
- 边缘功能一句话带过，篇幅留给「决定结果的那几个参数」和「怎么检查做对了」。
- 提示词、代码、配置用 ``` 围栏代码块保存——**代码块不会被念出来**，只作画面。
- 每段口播都要有对应的真实画面；暂时没有画面的段落，改写成画面能证明的话。

## 阶段 2 · 配音

```bash
python scripts/studio.py voice --project <项目>
```

按 `## <章节ID> | <标题>` 切章、空行断句、代码块和 `>` 备注跳过，产出 `build/audio_map.json`，每段带**实测时长**。

关注输出的 `chars/s`：低于 3.0 的段落是 TTS 异常（某次合成被拉长），改写后重生成，不要带着它进剪辑。

## 阶段 3 · 实操与外部录制 ★

这是本技能的核心环节。

### 3.1 准备录制环境

```bash
python scripts/studio.py windows                      # 找到目标窗口
python scripts/studio.py prep --project <项目> \
    --keep-title "Chrome" --maximize-title "Chrome"   # 藏掉无关窗口，目标窗口最大化
```

`prep` 永远不会藏技能自己的终端（按祖先 PID 排除）。

### 3.2 启动屏幕守卫 ★

**这一步不能省。** 技能在操作过程中会执行命令（ffmpeg、ffprobe、python 子进程），
每一个都可能弹出一个控制台窗口，正好落在用户的采集屏上被录进去。

```bash
python scripts/studio.py guard start --project <项目> \
    --protect "DeepSeek Harness" --protect "Chrome"
```

守卫是一个独立的后台进程，每 0.25 秒扫一遍窗口列表，把**新出现的控制台类窗口**
（`ConsoleWindowClass`、cmd、PowerShell、终端、xshell 等）和输入法 / 通知浮层
立即 `SW_HIDE` 掉。它记住藏过哪些窗口，`guard stop` 时原样恢复。

- `--protect` 保护正在演示的程序（按标题匹配，**必须传**）。
- 默认只藏技能自己进程树里的**控制台**窗口，不藏 GUI——用户的会话界面保持可见。
- 需要连 GUI 一起藏时加 `--hide-own-gui`。
- 守卫的判据是「真实重叠」：只压到采集区几像素的窗口（在另一块屏上的）不会被误藏。

### 3.3 通知用户开始

明确告诉用户：**请开始录制，完成后回复「已录制」**。
在用户确认之前不要动手——否则这段准备动作会被录进去。

### 3.4 执行真实操作

按章节顺序操作，每一步：

- 浏览器：用 CDP 元素点击，不移动真实鼠标。
  点击前把坐标记下来，后面标注要用：`python scripts/studio.py aim <项目> <x> <y> --note "导出格式"`。
- 桌面软件：窗口激活 + 键盘优先；确需点击时用真实鼠标，但要提醒用户录制期间不要动鼠标。
- 需要确认状态时，不必打断用户——自己抓一帧看一眼：

```bash
python scripts/studio.py shot --out <项目>/build/check.png
```

- **登录、授权、验证码**：保持录制，由用户自己完成，技能只负责确认是否成功。
  剪掉凭据、验证码、密钥——可以在登录前和成功后分段录制，再按依赖顺序拼接。
- 技能自己的每一条命令都必须**无窗口**执行：`scripts/h.ps1` 以 `CreateNoWindow`
  启动子进程，技能内部的子进程一律带 `CREATE_NO_WINDOW`。

### 3.5 通知用户停止

操作全部做完后立刻告诉用户：**操作结束，请停止录制，并把视频文件路径给我。**
等待用户返回路径。不要自行去找或删除任何录制文件。

### 3.6 收尾

```bash
python scripts/studio.py guard stop --project <项目>      # 停止守卫并恢复窗口
python scripts/studio.py guard status --project <项目>    # 确认采集屏上没有残留的控制台
```

`guard status` 会列出采集区里仍然可见的控制台类窗口——**必须为空**才算干净。
如果有残留，说明有程序在反复弹窗，让用户重录那一段。

## 阶段 4 · 导入素材并建立索引

```bash
python scripts/studio.py import --project <项目> --file "D:\video\rec.mp4"
python scripts/studio.py index  --project <项目> --clip rec.mp4 --step 2
```

`index` 按固定步长扫描整段录像，逐帧 OCR 并记录：

- 每一时刻屏幕上有什么文字（**用来查某一秒在干什么**）；
- 视觉变化点（**天然的切点**）；
- 污染区间（控制台 / 通知气泡 / 启动页，**要避开**）。

产出 `build/index_<名字>.txt`（可直接检索）和 `.json`（结构化）。
**排镜头前先读索引**，不要凭印象猜时间码。

## 阶段 5 · 排镜头

`build/plan.json`：

```json
{
  "display": {"region": [0, 0, 1920, 1080]},
  "voice": {"voice": "zh-CN-XiaoxiaoNeural", "rate": "+8%"},
  "chapters": [
    {
      "id": "ch02", "num": "02", "title": "文生图", "sub": "三段式提示词，决定还原度",
      "beats": [
        {"seg": 0, "src": "rec.mp4", "in": 120,  "out": 134},
        {"seg": 1, "src": "rec.mp4", "in": 136,  "out": 152, "zoom": 1.0}
      ],
      "callouts": [
        {"seg": 1, "offset": 1.0, "text": "提示词三段\n角色 → 材质 → 构图",
         "x": 120, "y": 680, "pointer": "up"}
      ]
    }
  ]
}
```

- `src` 是 `raw/` 里的文件名（由 `import` 导入的用户录像），`in`/`out` 是秒。
- `seg` 是这一镜服务于第几句口播，按章节内计，从 0 开始。
- `zoom` > 1 做推近（静态裁切放大）。
- `callouts` 用 `seg` + `offset` 定位标注出现的时刻，跟着口播走；`x`/`y` 是标注位置（可用 `aim` 记下的操作坐标）。
- 章节 `id` 必须等于 `script.md` 的章节ID，也必须等于 `audio_map.json` 的段前缀。
- 完整示例见 `assets/plan.example.json`。

**排镜头的规矩**：

- 一镜服务一个操作重点，镜头长度由那句口播决定。
- 名称出现前或同时到达相应控件；关键反馈出现时留阅读时间。
- 相邻控件尽量共用一次取景，不要频繁跳。
- 合理压缩真实等待，但保留「触发动作 → 处理反馈」可辨认的时间。
- 缺画面的段落，改写成画面能证明的话，或者补录。
- 避开索引标出的污染区间。

## 阶段 6 · 剪辑、校验、交付

```bash
python scripts/studio.py build  --project <项目>
python scripts/studio.py verify --video <项目>/final.mp4 --expect-duration <口播总长>
```

`build` 自动完成：逐句配时切镜 → 章节标题卡 → 标注叠加 → 交叉溶解转场树 → BGM 侧链压低 → 响度归一。

**BGM**：把音乐放到 `build/bgm.wav`（或 `build/bgm_final.wav`），构建器按人声包络算出压低曲线后混入。
没有这个文件也能出片，只是没有背景音乐。

**审计不通过时**：按 `build/audit.txt` 的时间码换窗口或补录，改 `plan.json` 重跑。
常见做法是把受污染的 `in`/`out` 整体挪到干净区间，或让用户补录那一段。

**交付前自查**：

- 文件能完整播放，画面与声音轨道完整，时长贴近口播总长。
- 逐帧 OCR 审计零问题。
- 以正常速度看和听整片，重点回查：前 3–10 秒、每个功能切换、提交与成果、数字专名、等候段、结尾。
- 每个讲解点都找得到对应光标落点；没有大段静态换图、没有无内容停顿、BGM 没盖人声。

## 命令速查

```bash
studio init   <项目>                     # 建项目骨架
studio windows                            # 列窗口，找操作目标
studio prep   --project <项目> --maximize-title "Chrome"
studio guard  start --project <项目> --protect "Chrome"    # ★ 录制期间保持采集屏干净
studio shot   --out check.png            # 抓一帧确认状态
studio aim    --project <项目> 1001 720 --note "导出格式"
studio guard  stop  --project <项目>      # 停止守卫并恢复窗口
studio guard  status --project <项目>     # 确认没有残留控制台
studio import --project <项目> --file rec.mp4
studio index  --project <项目> --clip rec.mp4
studio voice  --project <项目>
studio build  --project <项目>
studio audit  --project <项目>
studio verify --video final.mp4
studio status --project <项目>
```

## 参考

- `references/production.md` — 录屏、配音、剪辑、混音、验收细则
- `references/troubleshooting.md` — 症状 → 根因 → 修法
- `assets/plan.example.json` — 镜头计划完整示例
