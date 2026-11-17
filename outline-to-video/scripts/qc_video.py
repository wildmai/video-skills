"""阶段 9：成片质检。

检查时长、分辨率、音轨、整体响度、黑帧、长静音、定格等，
按 PASS / WARN / FAIL 输出结论。FAIL 时退出码为 1。
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from common import (  # noqa: E402
    StageError,
    StageLogger,
    ffprobe_bin,
    ffmpeg_bin,
    load_json,
    run,
)

LOUDNESS_TARGET = -16.0
LOUDNESS_TOLERANCE = 2.5


def analyze_loudness(path: str) -> float | None:
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostats", "-i", path,
           "-af", "ebur128=peak=true", "-f", "null", "-"]
    proc = run(cmd, check=False, timeout=3600)
    matches = re.findall(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", proc.stderr or "")
    if not matches:
        return None
    return float(matches[-1])


def analyze_black(path: str, min_duration: float = 0.1) -> list[tuple[float, float]]:
    """找出成片里的黑画面。

    阈值取 0.1s（30fps 下 3 帧）而不是 0.6s：
    录屏素材偶尔掉帧会夹进一小段纯黑，成片上就是一闪而过的黑屏。
    0.6s 的阈值会漏掉这种（实测一段 0.40s 的黑闪就被漏了）。
    """
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostats", "-i", path,
           "-vf", f"blackdetect=d={min_duration}:pix_th=0.10", "-an", "-f", "null", "-"]
    proc = run(cmd, check=False, timeout=3600)
    out = []
    for m in re.finditer(r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)", proc.stderr or ""):
        out.append((float(m.group(1)), float(m.group(2))))
    return out


def analyze_silence(path: str, min_duration: float = 1.5, threshold: str = "-45dB") -> list[float]:
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostats", "-i", path,
           "-af", f"silencedetect=n={threshold}:d={min_duration}", "-vn", "-f", "null", "-"]
    proc = run(cmd, check=False, timeout=3600)
    return [float(m.group(1)) for m in re.finditer(r"silence_duration:\s*([\d.]+)", proc.stderr or "")]


def analyze_freeze(path: str, min_duration: float = 2.0) -> list[float]:
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostats", "-i", path,
           "-vf", f"freezedetect=n=-60dB:d={min_duration}", "-an", "-f", "null", "-"]
    proc = run(cmd, check=False, timeout=3600)
    return [float(m.group(1)) for m in re.finditer(r"freeze_duration:\s*([\d.]+)", proc.stderr or "")]


def main() -> int:
    ap = argparse.ArgumentParser(description="成片质检")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--video", required=True)
    ap.add_argument("--timeline", default="")
    ap.add_argument("--expect-duration", type=float, default=0.0)
    ap.add_argument("--skip-heavy", action="store_true", help="跳过黑帧/静音/定格检测（快检）")
    args = ap.parse_args()

    work_dir = pathlib.Path(args.work_dir)
    logger = StageLogger(work_dir, "qc")
    video = pathlib.Path(args.video).resolve()
    if not video.exists():
        raise StageError(f"成片不存在：{video}", hint="先运行 render_video.py")

    failures: list[str] = []
    warnings: list[str] = []

    probe = run([ffprobe_bin(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                 str(video)])
    import json as _json
    info = _json.loads(probe.stdout)
    fmt = info.get("format", {})
    streams = info.get("streams", [])
    duration = float(fmt.get("duration", 0) or 0)
    vstreams = [s for s in streams if s.get("codec_type") == "video"]
    astreams = [s for s in streams if s.get("codec_type") == "audio"]
    width = int(vstreams[0].get("width", 0)) if vstreams else 0
    height = int(vstreams[0].get("height", 0)) if vstreams else 0
    size_mb = float(fmt.get("size", 0)) / 1024 / 1024

    logger.log(f"文件：{video.name}｜{size_mb:.1f} MB｜{duration:.1f}s｜{width}x{height}")
    if not vstreams:
        failures.append("没有视频流")
    if not astreams:
        failures.append("没有音频流")
    else:
        logger.log(f"音频：{astreams[0].get('codec_name')} {astreams[0].get('sample_rate')}Hz "
                   f"{astreams[0].get('channels')}ch")

    expected = args.expect_duration
    timeline_path = pathlib.Path(args.timeline) if args.timeline else None
    if timeline_path is not None and not timeline_path.is_absolute():
        timeline_path = work_dir / timeline_path
    if not expected and timeline_path and timeline_path.exists():
        expected = float(load_json(timeline_path)["meta"]["duration"])
    if expected:
        drift = duration - expected
        logger.log(f"期望时长 {expected:.1f}s，实际 {duration:.1f}s（差 {drift:+.2f}s）")
        if abs(drift) > 1.5:
            failures.append(f"成片时长与时间线相差 {drift:+.2f}s")
        elif abs(drift) > 0.5:
            warnings.append(f"成片时长与时间线相差 {drift:+.2f}s")

    loud = analyze_loudness(str(video))
    if loud is None:
        warnings.append("无法读取响度")
    else:
        logger.log(f"整体响度：{loud:.1f} LUFS（目标 {LOUDNESS_TARGET} ± {LOUDNESS_TOLERANCE}）")
        if abs(loud - LOUDNESS_TARGET) > LOUDNESS_TOLERANCE:
            warnings.append(f"响度 {loud:.1f} LUFS 偏离目标 {LOUDNESS_TARGET}，建议在渲染时开 loudnorm")

    if not args.skip_heavy:
        blacks = analyze_black(str(video))
        if blacks:
            logger.warn(f"检测到 {len(blacks)} 段黑帧：{[(round(a, 2), round(b, 2)) for a, b in blacks[:8]]}")
            warnings.append(f"存在 {len(blacks)} 段黑画面（≥0.1s），成片上会闪黑。"
                            f"素材掉帧造成的，跑 scan_black.py 定位并避开")
        silences = analyze_silence(str(video))
        long_sil = [s for s in silences if s >= 2.0]
        if long_sil:
            logger.warn(f"检测到 {len(long_sil)} 段超过 2s 的静音")
            warnings.append(f"存在 {len(long_sil)} 段长静音（可能是配音缺段）")
        freezes = analyze_freeze(str(video))
        if freezes:
            logger.warn(f"检测到 {len(freezes)} 段超过 2s 的定格画面")
            # 定格有两种成因，处理方式完全不同：
            #   素材不足 → 时间线把镜头拉长了（timeline 里该镜头 freeze > 0），
            #              画面是重复帧，必须补素材；
            #   素材静止 → 录屏时鼠标没动、面板没变，画面本来就不动，
            #              这是内容属性，不是缺陷，不必补素材。
            stretched = 0
            if timeline_path and timeline_path.exists():
                tl = load_json(timeline_path)
                stretched = sum(1 for s in tl.get("shots", [])
                                if float(s.get("freeze") or 0) > 0.01)
            if stretched:
                warnings.append(f"存在 {len(freezes)} 段长定格，其中 {stretched} 个镜头是"
                                f"素材不足被拉长的（画面是重复帧，建议补素材）")
            else:
                warnings.append(f"存在 {len(freezes)} 段长定格（时间线未拉伸任何镜头，"
                                f"是录屏素材本身静止导致，属内容属性，无需补素材）")

    for w in warnings:
        logger.warn(w)
    for f in failures:
        logger.error(f)
    if failures:
        logger.error(f"质检未通过：{len(failures)} 项失败 / {len(warnings)} 项警告")
        return 1
    logger.log(f"质检通过（{len(warnings)} 项警告）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StageError as err:
        print(f"[ERROR] {err}", file=sys.stderr)
        if err.hint:
            print(f"[HINT]  {err.hint}", file=sys.stderr)
        raise SystemExit(2)
