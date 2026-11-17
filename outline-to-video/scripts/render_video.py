"""阶段 8：渲染成片。

按 timeline.json 逐镜取材 → 归一化 → 拼接 → 铺配音 → 烧字幕 → 混 BGM → 输出 MP4。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from common import (  # noqa: E402
    StageError,
    StageLogger,
    ensure_dir,
    ffmpeg_bin,
    load_json,
    rel,
    run,
)


def escape_filter_path(path: str) -> str:
    """ffmpeg filtergraph 里的路径转义。"""
    return path.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def render_shot(shot: dict, out_path: pathlib.Path, width: int, height: int, fps: int,
                crf: int, preset: str, fonts_dir: str | None, ken_burns: float = 0.0,
                threads: int = 4) -> None:
    # duration 是这一镜在成片里的总长（含定格尾巴），
    # freeze 是其中靠后那段定格——它是从源素材末尾克隆补上的，不是真实素材
    total = float(shot["duration"])
    freeze = float(shot.get("freeze") or 0.0)
    dur = max(0.0, total - freeze)
    if ken_burns > 0.0005:
        # 轻微 Ken Burns：先整体放大留出漂移余量，再让裁切窗口缓慢移动。
        # 目的是消除静态界面/静态成片的「死画面」观感，同时不损失有效分辨率
        # （等效采样与直接 scale 到目标尺寸一致）。
        bw = int(round(width * (1 + ken_burns)))
        bh = int(round(height * (1 + ken_burns)))
        bw += bw % 2
        bh += bh % 2
        x_expr = f"(iw-ow)/2+(iw-ow)*0.85*min(1,t/{max(total, 0.001):.3f})"
        y_expr = f"(ih-oh)/2+(ih-oh)*0.45*min(1,t/{max(total, 0.001):.3f})"
        geom = (
            f",scale={bw}:{bh}:force_original_aspect_ratio=decrease"
            f",pad={bw}:{bh}:(ow-iw)/2:(oh-ih)/2:color=black"
            f",crop={width}:{height}:x='{x_expr}':y='{y_expr}'"
        )
    else:
        geom = (
            f",scale={width}:{height}:force_original_aspect_ratio=decrease"
            + f",pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    vf = (
        f"[0:v]trim=duration={dur:.3f},setpts=PTS-STARTPTS"
        + (f",tpad=stop_mode=clone:stop_duration={freeze:.3f}" if freeze > 0.01 else "")
        + geom
        + f",fps={fps},setsar=1[v]"
    )
    cmd = [
        ffmpeg_bin(), "-y", "-v", "error",
        "-ss", f"{float(shot['in']):.3f}", "-i", shot["src"],
        "-filter_complex", vf, "-map", "[v]", "-t", f"{total:.3f}",
        "-an", "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-r", str(fps),
        "-threads", str(threads), "-x264-params", f"threads={threads}",
        str(out_path),
    ]
    run(cmd, timeout=1800)


def concat_parts(parts: list[pathlib.Path], out_path: pathlib.Path, work_dir: pathlib.Path) -> None:
    # concat 列表里的相对路径是相对「列表文件所在目录」解析的，这里统一写绝对路径（前斜杠）。
    list_file = work_dir / "parts" / "concat.txt"
    list_file.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in parts) + "\n", encoding="utf-8"
    )
    cmd = [
        ffmpeg_bin(), "-y", "-v", "error", "-f", "concat", "-safe", "0",
        "-i", list_file.resolve().as_posix(),
        "-c", "copy", "-fflags", "+genpts", str(out_path.resolve()),
    ]
    run(cmd, cwd=work_dir, timeout=1800)


def build_voice_track(timeline: dict, work_dir: pathlib.Path) -> pathlib.Path:
    """把每句配音按绝对时间铺到一条静音底轨上。"""
    inputs = []
    delays = []
    index = 0
    for section in timeline.get("audio", []):
        section_start = float(section["start"])
        for sentence in section.get("sentences", []):
            inputs.append(sentence["audio"])
            delay_ms = int(round((section_start + float(sentence.get("start") or 0.0)) * 1000))
            delays.append(delay_ms)
            index += 1
    if not inputs:
        raise StageError("时间线里没有配音音频", hint="先运行 doubao_tts.py 生成配音")

    cmd = [ffmpeg_bin(), "-y", "-v", "error"]
    for path in inputs:
        cmd += ["-i", path]
    chains = []
    labels = []
    for i, delay_ms in enumerate(delays):
        label = f"a{i}"
        chains.append(f"[{i}:a]aresample=48000,adelay={delay_ms}:all=1[{label}]")
        labels.append(f"[{label}]")
    chains.append(f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0,aresample=48000[out]")
    cmd += ["-filter_complex", ";".join(chains), "-map", "[out]",
            "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", "voice.wav"]
    run(cmd, cwd=work_dir, timeout=1800)
    return work_dir / "voice.wav"


def main() -> int:
    ap = argparse.ArgumentParser(description="渲染最终 MP4")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--timeline", required=True)
    ap.add_argument("--out", required=True, help="输出 MP4 路径")
    ap.add_argument("--bgm", help="可选背景音乐")
    ap.add_argument("--bgm-volume", type=float, default=0.10)
    ap.add_argument("--loudnorm", type=float, default=-16.0, help="整体响度目标（LUFS），0 表示不做归一化")
    ap.add_argument("--crf", type=int, default=18)
    ap.add_argument("--preset", default="medium")
    ap.add_argument("--no-subtitles", action="store_true")
    ap.add_argument("--subtitles", default="subtitles.ass",
                    help="字幕文件名（相对 --work-dir）。预览片要用别的名字，别覆盖正式字幕")
    ap.add_argument("--fonts-dir", default=r"C:\Windows\Fonts" if os.name == "nt" else "")
    ap.add_argument("--keep-parts", action="store_true")
    ap.add_argument("--threads", type=int, default=4,
                    help="x264 编码线程数（默认 4）。多核机器上 x264 每个线程都要开帧缓冲，"
                         "1080p + 烧字幕 + 响度归一的滤镜链很容易把内存吃光，"
                         "报 'malloc of size ... failed' / '-542398533'。调小即可")
    ap.add_argument("--ken-burns", type=float, default=0.0,
                    help="轻微推拉的幅度比例（默认 0 关闭）。注意：ffmpeg 的 crop 会对浮点坐标取整，"
                         "运镜会变成阶梯式跳变，观感上是画面抖动；文字界面建议保持 0")
    args = ap.parse_args()

    work_dir = pathlib.Path(args.work_dir).resolve()
    logger = StageLogger(work_dir, "render")
    timeline = load_json(args.timeline)
    meta = timeline["meta"]
    width, height, fps = int(meta["width"]), int(meta["height"]), int(meta["fps"])
    shots = timeline["shots"]

    parts_dir = ensure_dir(work_dir / "parts")
    for old in parts_dir.glob("part_*.mp4"):
        old.unlink()

    logger.log(f"渲染 {len(shots)} 个镜头 → {width}x{height}@{fps}")
    parts: list[pathlib.Path] = []
    for i, shot in enumerate(shots, start=1):
        part = parts_dir / f"part_{i:04d}.mp4"
        render_shot(shot, part, width, height, fps, args.crf, "veryfast", args.fonts_dir,
                    args.ken_burns, args.threads)
        parts.append(part)
        logger.log(f"  [{i}/{len(shots)}] {shot['id']} {shot['video']} "
                   f"{shot['in']:.1f}→{shot['out']:.1f}s" + (f" (+定格{shot['freeze']:.1f}s)" if shot.get("freeze") else ""))

    silent = work_dir / "video_silent.mp4"
    concat_parts(parts, silent, work_dir)
    logger.log(f"拼接完成 → {silent}")

    voice = build_voice_track(timeline, work_dir)
    logger.log(f"配音轨完成 → {voice}")

    inputs = ["-i", rel(silent, work_dir), "-i", rel(voice, work_dir)]
    bgm_index = None
    if args.bgm:
        bgm_index = 2
        inputs += ["-i", str(pathlib.Path(args.bgm).resolve())]

    ass_file = work_dir / args.subtitles
    use_subs = (not args.no_subtitles) and ass_file.exists()
    if use_subs:
        # 路径里的冒号必须转义，且整个取值要用单引号包住，否则 filtergraph 会把冒号当成选项分隔符。
        sub_filter = f"subtitles=filename='{escape_filter_path(rel(ass_file, work_dir))}'"
        if args.fonts_dir:
            sub_filter += f":fontsdir='{escape_filter_path(args.fonts_dir)}'"
        video_filter = f"[0:v]{sub_filter}[v]"
    else:
        video_filter = "[0:v]null[v]"

    audio_filter = "[1:a]aresample=48000"
    if args.loudnorm:
        audio_filter += f",loudnorm=I={args.loudnorm}:TP=-1.5:LRA=11"
    audio_filter += "[vo]"
    if bgm_index is not None:
        audio_filter += f";[vo][{bgm_index}:a]volume={args.bgm_volume}[mix]"
        map_audio = "[mix]"
    else:
        audio_filter += ";[vo]anull[ao]"
        map_audio = "[ao]"

    base = [ffmpeg_bin(), "-y", "-v", "error"] + inputs + [
        "-filter_complex", f"{video_filter};{audio_filter}",
        "-map", "[v]", "-map", map_audio,
        "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-t", f"{float(meta['duration']):.3f}",
        "-movflags", "+faststart",
    ]
    # x264 每个线程都要开帧缓冲。机器核数多、内存又紧的时候，
    # 1080p + 烧字幕 + 响度归一的滤镜链会把内存吃光，
    # 报 "malloc of size ... failed"（-542398533）。
    # 失败就逐级减少线程数重试，直到只剩单线程。
    threads = max(1, args.threads)
    while True:
        cmd = base + ["-threads", str(threads), "-x264-params", f"threads={threads}",
                      str(pathlib.Path(args.out).resolve())]
        try:
            run(cmd, cwd=work_dir, timeout=7200)
            break
        except StageError as err:
            if threads <= 1:
                raise
            threads = max(1, threads // 2)
            logger.warn(f"合成失败（多半是内存不够），降到 {threads} 线程重试：{str(err)[:200]}")
    logger.log(f"成片完成 → {args.out}")

    if not args.keep_parts:
        for part in parts:
            part.unlink()
        for tmp in (silent, voice):
            if tmp.exists():
                tmp.unlink()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StageError as err:
        print(f"[ERROR] {err}", file=sys.stderr)
        if err.hint:
            print(f"[HINT]  {err.hint}", file=sys.stderr)
        raise SystemExit(2)
