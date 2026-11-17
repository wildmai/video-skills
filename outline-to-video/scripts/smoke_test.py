"""端到端冒烟测试。

用合成的素材视频跑通「清点 → 转写 → 抽帧 → 匹配 → 编排 → 渲染 → 质检」全链路。
豆包 TTS 用本地合成的等长音频替代，验证时间线与渲染逻辑。
用法：python smoke_test.py [--work-dir DIR] [--keep]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import ffmpeg_bin, ffprobe_bin, run  # noqa: E402

PY = sys.executable


def sh(args: list[str], cwd: pathlib.Path | None = None) -> None:
    print(f"\n$ {' '.join(args)}", flush=True)
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.stdout:
        print(proc.stdout.strip()[-3000:])
    if proc.returncode != 0:
        print(proc.stderr.strip()[-3000:], file=sys.stderr)
        raise SystemExit(f"步骤失败（退出码 {proc.returncode}）")


def make_media(media_dir: pathlib.Path) -> None:
    """生成 3 段合成素材：不同颜色 + 音频 + 外挂字幕。"""
    media_dir.mkdir(parents=True, exist_ok=True)
    specs = [
        ("intro.mp4", "color=c=0x1E3A8A:s=1280x720:d=18", 18, "开场：今天我们讲清楚三件事"),
        ("core.mp4", "color=c=0x065F46:s=1280x720:d=24", 24, "核心：先讲结论，再拆步骤，最后给清单"),
        ("demo.mp4", "color=c=0x7C2D12:s=1280x720:d=20", 20, "演示：打开设置面板，第一项就是关键开关"),
    ]
    for name, src, dur, line in specs:
        out = media_dir / name
        cmd = [ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi", "-i", src,
               "-f", "lavfi", "-i", f"sine=frequency={300 + 60 * len(name)}:duration={dur}",
               "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-shortest", str(out)]
        run(cmd, timeout=600)
        srt = out.with_suffix(".srt")
        srt.write_text(
            f"1\n00:00:01,000 --> 00:00:04,000\n{line}\n\n"
            f"2\n00:00:05,000 --> 00:00:08,000\n这是第 {len(line)} 个要点，用来测试时间戳对齐\n",
            encoding="utf-8",
        )
        print(f"生成素材 {out.name} + {srt.name}")


def fake_tts(script: dict, out_dir: pathlib.Path) -> pathlib.Path:
    """用本地合成的音频伪造豆包 TTS 的输出结构。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    index = {"speaker": "fake", "resource_id": "fake", "sections": {}}
    for sec in script["sections"]:
        sid = sec["id"]
        sentences = []
        cursor = 0.0
        parts = [p for p in sec["vo"].replace("。", "。\n").split("\n") if p.strip()]
        for i, text in enumerate(parts, start=1):
            text = text.strip()
            dur = max(1.2, len(text) / 5.2)
            audio = out_dir / f"{sid}_{i:03d}.mp3"
            run([ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi", "-i",
                 f"sine=frequency={220 + 30 * i}:duration={dur:.3f}",
                 "-c:a", "libmp3lame", "-q:a", "5", str(audio)], timeout=600)
            words = []
            step = dur / max(1, len(text))
            for j, ch in enumerate(text):
                words.append({"word": ch, "start": round(cursor + j * step, 3),
                              "end": round(cursor + (j + 1) * step, 3)})
            sentences.append({
                "index": i, "text": text, "audio": str(audio.resolve()),
                "duration": round(dur, 3), "start": round(cursor, 3),
                "end": round(cursor + dur, 3), "words": words,
            })
            cursor = round(cursor + dur, 3)
        index["sections"][sid] = {"duration": cursor, "sentences": sentences}
    path = out_dir / "index.json"
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="outline-to-video 端到端冒烟测试")
    ap.add_argument("--work-dir", default="")
    ap.add_argument("--real-tts", action="store_true",
                    help="用真实豆包 TTS 配音（需要 .env 里配好 API Key 与音色 ID）")
    ap.add_argument("--preview-tts", action="store_true",
                    help="用 Edge TTS 出试听稿（免费，需要网络与 edge-tts 包）")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    root = pathlib.Path(args.work_dir).resolve() if args.work_dir else pathlib.Path("outline-smoke").resolve()
    if root.exists() and not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    media_dir = root / "media"
    work_dir = root / "work"
    media_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    print("=== 1. 生成合成素材 ===")
    make_media(media_dir)

    print("\n=== 2. 清点素材 ===")
    sh([PY, str(HERE / "probe_media.py"), "--media-dir", str(media_dir), "--work-dir", str(work_dir)])

    print("\n=== 3. 转写（使用外挂字幕） ===")
    sh([PY, str(HERE / "transcribe.py"), "--work-dir", str(work_dir)])

    print("\n=== 4. 抽帧 + 分镜 ===")
    sh([PY, str(HERE / "extract_frames.py"), "--work-dir", str(work_dir), "--interval", "4"])

    print("\n=== 5. 大纲匹配 ===")
    outline = root / "outline.md"
    outline.write_text(
        "# 冒烟测试大纲\n\n"
        "## 开场钩子\n"
        "结论先说：三个步骤搞定\n"
        "## 核心方法\n"
        "先讲结论 再拆步骤\n"
        "## 演示\n"
        "设置面板 关键开关\n",
        encoding="utf-8",
    )
    sh([PY, str(HERE / "match_outline.py"), "--work-dir", str(work_dir), "--outline", str(outline)])

    print("\n=== 6. 生成脚本并校验 ===")
    inventory = json.loads((work_dir / "inventory.json").read_text(encoding="utf-8"))
    by_stem = {v["stem"]: v["id"] for v in inventory["videos"]}
    script = {
        "title": "冒烟测试成片",
        "target_duration": 40,
        "meta": {"width": 1920, "height": 1080, "fps": 30},
        "sections": [
            {"id": "s1", "outline": "开场钩子", "role": "hook",
             "vo": "别急着动手，先记住这三个步骤，一分钟就能搞定整套流程。",
             "clips": [{"video": by_stem["intro"], "start": 0.0, "end": 6.0, "note": "开场画面"}]},
            {"id": "s2", "outline": "核心方法", "role": "point",
             "vo": "第一步给结论，第二步拆步骤，第三步补清单，顺序反了就会返工。",
             "clips": [{"video": by_stem["core"], "start": 1.0, "end": 12.0, "note": "核心演示"}]},
            {"id": "s3", "outline": "演示", "role": "cta",
             "vo": "打开设置面板，第一个开关就是关键项，动手试一次就全明白了。",
             "clips": [{"video": by_stem["demo"], "start": 0.0, "end": 10.0, "note": "收尾演示"}]},
        ],
    }
    script_path = work_dir / "script.json"
    script_path.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
    sh([PY, str(HERE / "validate_script.py"), "--work-dir", str(work_dir), "--script", str(script_path)])

    if args.preview_tts:
        print("\n=== 7. 预览配音（Edge TTS，免费） ===")
        sh([PY, str(HERE / "tts_preview.py"), "--work-dir", str(work_dir),
            "--script", str(script_path)])
        # 预览配音的索引单独放一份，和正式配音的 tts/index.json 互不覆盖
        tts_index = work_dir / "tts" / "index_preview.json"
    elif args.real_tts:
        print("\n=== 7. 配音（真实豆包 TTS） ===")
        sh([PY, str(HERE / "doubao_tts.py"), "--script", str(script_path),
            "--out-dir", str(work_dir / "tts"), "--work-dir", str(work_dir)])
        tts_index = work_dir / "tts" / "index.json"
    else:
        print("\n=== 7. 配音（本地合成替代豆包） ===")
        tts_index = fake_tts(script, work_dir / "tts")

    print("\n=== 8. 编排时间线 ===")
    sh([PY, str(HERE / "build_timeline.py"), "--work-dir", str(work_dir),
        "--script", str(script_path), "--tts-index", str(tts_index)])

    print("\n=== 9. 渲染成片 ===")
    out = root / "out.mp4"
    sh([PY, str(HERE / "render_video.py"), "--work-dir", str(work_dir),
        "--timeline", str(work_dir / "timeline.json"), "--out", str(out)])

    print("\n=== 10. 质检 ===")
    sh([PY, str(HERE / "qc_video.py"), "--work-dir", str(work_dir), "--video", str(out),
        "--timeline", str(work_dir / "timeline.json")])

    size_mb = out.stat().st_size / 1024 / 1024
    dur = float(json.loads(run([ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
                                "-of", "json", str(out)]).stdout)["format"]["duration"])
    print(f"\n冒烟测试通过：{out}（{size_mb:.1f} MB / {dur:.1f}s），耗时 {time.time() - t0:.0f}s")
    if not args.keep:
        print(f"（工作目录保留在 {root}，加 --keep 可自动清理）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
