"""Synthesize or reuse narration clips with Edge TTS."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory


def read_manifest(path):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Manifest must be a JSON object.")
    voice = data.get("voice")
    rate = data.get("rate", "+0%")
    if not isinstance(voice, str) or not voice.strip():
        raise ValueError("Manifest requires a nonempty voice.")
    if not isinstance(rate, str) or not re.fullmatch(r"[+-]\d+%", rate):
        raise ValueError("Rate must use a signed percent, such as +0%.")
    segments = data.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("Manifest requires at least one narration segment.")
    seen = set()
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("Each segment must be an object.")
        clip_id = segment.get("id")
        if not isinstance(clip_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", clip_id):
            raise ValueError("Segment IDs must be 1-48 lowercase letters, digits, underscores or hyphens.")
        if clip_id in seen:
            raise ValueError(f"Duplicate segment ID: {clip_id}")
        seen.add(clip_id)
        if not isinstance(segment.get("text"), str) or not segment["text"].strip():
            raise ValueError(f"Segment has no narration: {clip_id}")
    return voice, rate, segments


def render_segment(output, voice, rate, segment):
    narration = segment["text"].strip()
    payload = json.dumps([voice, rate, narration], ensure_ascii=False).encode("utf-8")
    fingerprint = hashlib.sha256(payload).hexdigest()[:16]
    clip = output / f'{segment["id"]}-{fingerprint}.mp3'
    if clip.is_file() and clip.stat().st_size:
        return clip
    with TemporaryDirectory(dir=output) as temporary:
        staging = Path(temporary)
        text_path = staging / "narration.txt"
        media_path = staging / "narration.mp3"
        text_path.write_text(narration, encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "edge_tts",
                "--voice",
                voice,
                f"--rate={rate}",
                "--file",
                str(text_path),
                "--write-media",
                str(media_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
        if result.returncode:
            error = result.stderr.strip().splitlines()
            detail = error[-1].split(", url=", 1)[0] if error else f"exit code {result.returncode}"
            raise RuntimeError(f'Edge TTS failed for {segment["id"]}: {detail}')
        if not media_path.is_file() or not media_path.stat().st_size:
            raise RuntimeError(f'Edge TTS returned no audio for {segment["id"]}.')
        media_path.replace(clip)
    return clip


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="UTF-8 JSON narration manifest")
    parser.add_argument("output", type=Path, help="Directory for generated MP3 segments")
    args = parser.parse_args()
    try:
        voice, rate, segments = read_manifest(args.manifest)
        output = args.output.resolve()
        output.mkdir(parents=True, exist_ok=True)
        clips = {
            segment["id"]: str(render_segment(output, voice, rate, segment))
            for segment in segments
        }
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(clips, indent=2))


if __name__ == "__main__":
    main()

