"""阶段 6b：增量重合成「文变了」的句子，并就地更新 tts/index.json。

doubao_tts.py 每次都会把整篇脚本重新合成一遍（97 句 = 97 次 API 调用）。
改一两句文案时完全没必要全量重跑。
本脚本按「句子文本」把新旧两句对齐：文本没变的直接复用已有音频和字级时间戳，
只有真正改动的句子才调用 TTS，最后重排整节的起始时间。

复用旧配音的两个硬规矩（违反会导致字幕顺序错乱）：

1. `api_words` 是**句内相对秒**，原样搬运，不能再减句首偏移；
2. 复用前校验时间戳是否越界（`min(start) >= 0` 且 `max(end) <= 句长`），
   越界说明这份数据已经坏了，必须重新合成。

用法:
  python scripts/tts_patch.py --work-dir <work> [--script <work>/script.json]
                              [--index <work>/tts/index.json] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil

from common import EnvConfig, StageLogger
import doubao_tts as tts
import word_align

TTS_DIRNAME = "tts"


def api_words_usable(words: list[dict], duration: float) -> bool:
    """检查 API 原始时间戳是否还可用。

    豆包返回的时间戳是「句内相对秒」，必须落在 [0, 音频时长] 内。
    越界说明这份数据被错误地平移过（例如早期版本把「节内绝对秒」
    当成「句内相对秒」又减了一次句首偏移），拿去对齐只会把词元
    指到错误的发声区间上，必须重新合成。
    """
    if not words:
        return False
    lo = min(float(w["start"]) for w in words)
    hi = max(float(w["end"]) for w in words)
    return lo >= -0.01 and hi <= duration + 0.3


def align(new_texts: list[str], old_entries: list[dict]) -> list[dict | None]:
    """把新句子按文本顺序对齐到旧条目；对不上就是 None（需要重新合成）。"""
    aligned: list[dict | None] = []
    cursor = 0
    for text in new_texts:
        hit = None
        for k in range(cursor, len(old_entries)):
            if old_entries[k]["text"] == text:
                hit = old_entries[k]
                cursor = k + 1
                break
        aligned.append(hit)
    return aligned


def main() -> int:
    ap = argparse.ArgumentParser(description="增量重合成改动的口播句子")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--script", default="")
    ap.add_argument("--index", default="")
    ap.add_argument("--dry-run", action="store_true", help="只报告要重合成哪些句子")
    args = ap.parse_args()

    work_dir = pathlib.Path(args.work_dir).resolve()
    tts_dir = work_dir / TTS_DIRNAME
    stage_dir = work_dir / "tts_stage"
    script_path = pathlib.Path(args.script) if args.script else work_dir / "script.json"
    index_path = pathlib.Path(args.index) if args.index else tts_dir / "index.json"

    logger = StageLogger(work_dir, "tts_patch")
    script = json.loads(script_path.read_text(encoding="utf-8"))
    index = json.loads(index_path.read_text(encoding="utf-8"))

    env = EnvConfig(work_dir)
    api_key, _ = env.pick(None, "DOUBAO_API_KEY")
    speaker, _ = env.pick(None, "DOUBAO_SPEAKER")
    resource_id, _ = env.pick(None, "DOUBAO_RESOURCE_ID", tts.DEFAULT_RESOURCE_ID)
    model_key, _ = env.pick(None, "DOUBAO_MODEL", "auto")
    if not api_key or not speaker:
        logger.warn("缺少 DOUBAO_API_KEY / DOUBAO_SPEAKER，无法合成")
        return 2

    model_value = tts.MODEL_ALIASES.get(model_key.lower(), model_key.strip())
    if resource_id in tts.ICL_RESOURCE_IDS and not model_value:
        logger.warn("声音复刻音色必须带 model 参数")
        return 2
    if resource_id not in tts.ICL_RESOURCE_IDS:
        model_value = ""
    audio_params = {"format": "mp3", "sample_rate": 24000, "speech_rate": 0, "pitch": 0}
    additions = tts.build_additions(None)

    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True)

    new_index = {
        "engine": "doubao",          # 正式配音引擎
        "speaker": speaker,
        "resource_id": resource_id,
        "model": model_value,
        "format": "mp3",
        "sample_rate": 24000,
        "sections": {},
    }
    total_chars = 0
    changed = 0
    # 索引里可能存着预览配音（edge-tts）。正式配音必须整篇用豆包重来，
    # 绝不能把预览稿当成「已合成好的句子」复用掉——音色会串。
    old_engine = index.get("engine") or "doubao"
    if old_engine != "doubao":
        logger.warn(f"现有配音引擎是 {old_engine}（预览稿），本次将整篇用豆包重新合成")

    for sec in script["sections"]:
        sid = sec["id"]
        raw_vo = (sec.get("vo") or "").strip()
        vo, notes = tts.sanitize_text(raw_vo)
        for note in notes:
            logger.warn(f"{sid}: {note}")
        sentences = tts.split_sentences(vo)
        old_entries = index["sections"].get(sid, {}).get("sentences", [])
        aligned = align(sentences, old_entries)

        entries = []
        cursor = 0.0
        for i, (text, old) in enumerate(zip(sentences, aligned), start=1):
            name = f"{sid}_{i:03d}.mp3"
            target = stage_dir / name
            api_words = []
            regions = []
            reuse = old
            if old is not None and old_engine != "doubao":
                # 旧音频是预览稿（edge-tts）：音色不对，不能复用，必须用豆包重合成
                logger.warn(f"  {sid}[{i}] 旧音频来自 {old_engine} 预览稿，改用豆包重合成")
                reuse = None
                old = None
            if old is not None:
                base = float(old["start"])
                # 保留 API 原始时间戳：对齐必须始终从同一份输入出发，才能幂等。
                # api_words 是「句内相对秒」，原样复用；
                # 老索引里的 words 是「节内绝对秒」，要减掉本句起点才是句内相对秒。
                src_api = old.get("api_words")
                if src_api:
                    cand = [{"word": w["word"], "start": round(float(w["start"]), 3),
                             "end": round(float(w["end"]), 3)} for w in src_api]
                else:
                    cand = [{"word": w["word"], "start": round(float(w["start"]) - base, 3),
                             "end": round(float(w["end"]) - base, 3)} for w in old["words"]]
                if not api_words_usable(cand, float(old["duration"])):
                    logger.warn(f"  {sid}[{i}] 旧时间戳越界已失效，需重新合成")
                    reuse = None
                else:
                    rel_words = cand
                    api_words = cand
                    regions = old.get("regions") or []
            if reuse is not None:
                src = pathlib.Path(old["audio"])
                shutil.copyfile(src, target)
                duration = float(old["duration"])
            else:
                changed += 1
                logger.log(f"  {sid}[{i}] 重新合成：{text[:40]}")
                if args.dry_run:
                    duration, rel_words = 0.0, []
                else:
                    result = tts.synthesize(text, api_key, speaker, resource_id,
                                            audio_params, additions, model_value or None,
                                            logger=logger)
                    target.write_bytes(result["audio"])
                    duration = tts.measure_duration(target)
                    rel_words = [{"word": w["word"], "start": round(float(w["start"]), 3),
                                  "end": round(float(w["end"]), 3)} for w in result["words"]]
                    api_words = list(rel_words)
                    if duration <= 0:
                        duration = round(max((w["end"] for w in rel_words), default=0.0), 3)
                    if rel_words and rel_words[-1]["end"] > duration + 0.5:
                        scale = duration / rel_words[-1]["end"]
                        rel_words = [{"word": w["word"], "start": round(w["start"] * scale, 3),
                                      "end": round(w["end"] * scale, 3)} for w in rel_words]
            # 用音频校正字级时间戳：豆包对中英混排的词元时间戳不可靠
            # （拉丁词元时长被压成 30ms，字幕会闪断），详见 word_align.py
            if rel_words and not args.dry_run:
                rel_words = word_align.align_words(rel_words, str(target), duration)
                if not regions:
                    regions = [[round(a, 3), round(b, 3)] for a, b in
                               word_align.detect_regions(str(target), duration)]
            words = [{"word": w["word"], "start": round(cursor + w["start"], 3),
                      "end": round(cursor + w["end"], 3)} for w in rel_words]
            entries.append({
                "index": i,
                "text": text,
                "audio": str((tts_dir / name).resolve()),
                "duration": round(duration, 3),
                "start": round(cursor, 3),
                "end": round(cursor + duration, 3),
                "words": words,
                "api_words": api_words,
                "regions": regions,
            })
            cursor = round(cursor + duration, 3)
            total_chars += len(text)
        new_index["sections"][sid] = {"duration": round(cursor, 3), "sentences": entries}
        logger.log(f"{sid}：{len(entries)} 句 / {cursor:.2f}s")

    new_index["total_duration"] = round(sum(s["duration"] for s in new_index["sections"].values()), 3)
    new_index["total_chars"] = total_chars

    if args.dry_run:
        logger.log(f"dry-run：将重合成 {changed} 句，最终 {new_index['total_duration']:.1f}s")
        shutil.rmtree(stage_dir)
        return 0

    # 换装：新音频全部就位后再替换旧目录，避免中途覆盖掉还要复用的源文件
    for old_file in tts_dir.glob("*.mp3"):
        old_file.unlink()
    for staged in stage_dir.glob("*.mp3"):
        shutil.move(str(staged), str(tts_dir / staged.name))
    shutil.rmtree(stage_dir)
    index_path.write_text(
        json.dumps(new_index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.log(f"完成：重合成 {changed} 句，总时长 {new_index['total_duration']:.1f}s / {total_chars} 字")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
