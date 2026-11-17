# ffmpeg 关键命令与坑

本 skill 实际用到的命令都在 `scripts/` 里。
这份文档记录它们的原理和踩过的坑，方便排查。

## 一、依赖

```bash
ffmpeg -version      # 需要 libx264 / aac / libass(subtitles 滤镜)
ffmpeg -filters | grep -E "subtitles|tpad|tile|sidechaincompress"
```

找不到 ffmpeg 时设环境变量 `FFMPEG_BIN` / `FFPROBE_BIN` 指向可执行文件。

## 二、抽帧与联系表

```bash
# 每 6 秒抽一帧，缩到 480 宽
ffmpeg -y -v error -i in.mp4 -vf "fps=1/6,scale=480:-2" -q:v 3 frame_%05d.jpg
```

联系表由 Pillow 拼成（`extract_frames.py`），每格左上角标了时间戳，比 ffmpeg 的 `tile` 滤镜更好用——`tile` 没法标时间。

## 三、场景检测

```bash
ffmpeg -v info -i in.mp4 -filter:v "select='gt(scene,0.35)',showinfo" -f null -
```

从 stderr 里抓 `pts_time:` 就是镜头切点。
阈值 0.35 对录屏比较合适；画面变化大的素材可以调到 0.5。

## 四、片段归一化（渲染的核心）

```bash
ffmpeg -y -ss 12.0 -i src.mp4 \
  -filter_complex "[0:v]trim=duration=6.5,setpts=PTS-STARTPTS,scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,fps=30,setsar=1[v]" \
  -map "[v]" -t 6.5 -an -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p part.mp4
```

要点：

- `-ss` 放在 `-i` 之前是快速定位（先跳后解）。
- `trim=duration=` 才是真正裁剪；只用 `-t` 的话输入流还在往下走，后面 `tpad` 就没有定格效果。
- `tpad=stop_mode=clone:stop_duration=X` 在流末尾克隆最后一帧，用来补时长。
- `scale` + `pad` 做等比缩放加黑边，保留全部画面；想裁满屏就再加 `crop`。
- `setsar=1` 必须有，否则拼接后可能变形。
- 所有分片参数完全一致，才能用 concat 免重编码拼接。

## 五、拼接

```bash
ffmpeg -y -v error -f concat -safe 0 -i parts/concat.txt -c copy -fflags +genpts video_silent.mp4
```

**坑**：concat 列表里的相对路径是相对**列表文件所在目录**解析的，不是相对当前工作目录。
列表文件在 `work/parts/concat.txt` 时，写 `parts/x.mp4` 会被解析成 `work/parts/parts/x.mp4`。
本 skill 直接写绝对路径（正斜杠）规避。

## 六、铺配音轨

每句配音单独延迟到位，再混合：

```bash
ffmpeg -y -i a1.mp3 -i a2.mp3 \
  -filter_complex "[0:a]aresample=48000,adelay=0:all=1[a0];[1:a]aresample=48000,adelay=4200:all=1[a1];[a0][a1]amix=inputs=2:normalize=0[out]" \
  -map "[out]" -c:a pcm_s16le -ar 48000 -ac 2 voice.wav
```

- `adelay` 的单位是毫秒。
- `amix` 必须加 `normalize=0`，否则混合后整体被压低。
- `adelay=...:all=1` 对所有声道生效（单声道写法在立体声输入上会报错）。

## 七、烧字幕

```bash
ffmpeg -y -i video_silent.mp4 -i voice.wav \
  -filter_complex "[0:v]subtitles=filename='subtitles.ass':fontsdir='C\:/Windows/Fonts'[v];[1:a]anull[ao]" \
  -map "[v]" -map "[ao]" -c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p -c:a aac -b:a 192k -movflags +faststart out.mp4
```

**坑（Windows）**：滤镜里的路径如果带冒号（盘符 `C:`），必须转义成 `C\:`，而且整个取值要用单引号包住。
否则 ffmpeg 会把冒号当成选项分隔符，报 `No option name near '/Windows/Fonts'`。

字体查找：libass 会用系统字体，Windows 上 `Microsoft YaHei`（微软雅黑）可直接用。
换字体时改 `work/subtitles.ass` 里的 `Style:` 行。

## 八、响度归一化

```bash
... -filter_complex "[1:a]aresample=48000,loudnorm=I=-16:TP=-1.5:LRA=11[vo]" ...
```

- `-16 LUFS` 是抖音/小红书/B 站通用目标。
- `TP=-1.5` 防止削波。
- 这是单遍 loudnorm，精度够用；追求更准可以跑两遍 loudnorm（measure + apply）。

## 九、质检滤镜

```bash
# 整体响度
ffmpeg -i out.mp4 -af ebur128=peak=true -f null -            # 抓 "I: -16.0 LUFS"

# 黑帧
ffmpeg -i out.mp4 -vf blackdetect=d=0.6:pix_th=0.10 -an -f null -

# 长静音
ffmpeg -i out.mp4 -af silencedetect=n=-45dB:d=1.5 -vn -f null -

# 长定格
ffmpeg -i out.mp4 -vf freezedetect=n=-60dB:d=2.0 -an -f null -
```

长定格通常是"画面撑不住口播"的信号，应该回到脚本补画面，而不是接受它。

## x264 报 "malloc of size ... failed"（-542398533）

**现象**：成片合成阶段失败，报

```
x264 [error]: malloc of size 7088704 failed
[enc:libx264] Error submitting video frame to the encoder
Task finished with error code: -542398533
```

**原因**：x264 每个编码线程都要开帧缓冲。机器核数多（比如 16 核）、
可用内存又只剩几个 G 时，1080p + 烧字幕 + 响度归一这条滤镜链会把内存吃光。
不是编码参数错，是**内存不够**。

**解法**：限制编码线程数。

```bash
python scripts/render_video.py ... --threads 4
```

`render_video.py` 会把线程数同时传给 `-threads` 和 `-x264-params threads=`，
并且**失败时自动降级重试**（4 → 2 → 1），所以不用手工重跑。

排查内存：

```powershell
(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1KB   # MB
```

另外：渲染前把别的吃内存的程序关掉（浏览器多开标签页、IDE 索引、虚拟机）。
