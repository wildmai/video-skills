"""outline-to-video 共享工具层。

提供：日志、ffmpeg/ffprobe 调用、JSON 读写、路径解析、字幕解析、.env 配置。
所有阶段脚本都从这里取基础设施，保证日志格式与错误语义一致。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Iterable

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".wmv", ".ts", ".mpg", ".mpeg", ".mts"}
SIDECAR_EXTS = {".srt", ".vtt", ".ass", ".ssa"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

# skill 根目录（scripts 的上一级），.env 的默认落点
SKILL_ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_FILENAME = ".env"
_FALLBACK_ENV_TEMPLATE = """\
# 豆包语音配置（改完直接生效，不用改命令行）
DOUBAO_API_KEY=
DOUBAO_SPEAKER=
DOUBAO_RESOURCE_ID=seed-icl-2.0
DOUBAO_MODEL=auto
"""


def _load_env_template() -> str:
    """模板以 assets/env.example 为唯一来源，避免两处内容漂移。"""
    example = SKILL_ROOT / "assets" / "env.example"
    if example.exists():
        return example.read_text(encoding="utf-8")
    return _FALLBACK_ENV_TEMPLATE


ENV_TEMPLATE = _load_env_template()

FFMPEG_CANDIDATES = [
    "ffmpeg",
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
]
FFPROBE_CANDIDATES = [
    "ffprobe",
    r"C:\ffmpeg\bin\ffprobe.exe",
    r"C:\ProgramData\chocolatey\bin\ffprobe.exe",
    r"C:\Program Files\ffmpeg\bin\ffprobe.exe",
]


class StageError(RuntimeError):
    """阶段失败，附带可执行的修复建议。"""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


class StageLogger:
    """写文件日志 + 同步打印到 stdout。

    每个阶段一个日志文件：<work_dir>/logs/<stage>.log。
    """

    def __init__(self, work_dir: str | os.PathLike, stage: str) -> None:
        self.stage = stage
        self.dir = pathlib.Path(work_dir) / "logs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{stage}.log"
        self._fh = self.path.open("a", encoding="utf-8")

    def log(self, message: str, level: str = "INFO") -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {level:<5} {message}"
        print(line, flush=True)
        self._fh.write(line + "\n")
        self._fh.flush()

    def warn(self, message: str) -> None:
        self.log(message, "WARN")

    def error(self, message: str) -> None:
        self.log(message, "ERROR")

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def __enter__(self) -> "StageLogger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def resolve_binary(name: str, candidates: Iterable[str]) -> str:
    """按 env 覆盖 → PATH → 常见安装位置 的顺序定位可执行文件。"""
    env_key = f"{name.upper()}_BIN"
    override = os.environ.get(env_key)
    if override:
        if pathlib.Path(override).exists():
            return override
        found = shutil.which(override)
        if found:
            return found
        raise StageError(f"环境变量 {env_key}={override} 指向的文件不存在", hint="检查该变量指向的可执行文件路径")
    for candidate in candidates:
        if os.path.sep in candidate or (os.name == "nt" and ":" in candidate):
            if pathlib.Path(candidate).exists():
                return candidate
            continue
        found = shutil.which(candidate)
        if found:
            return found
    raise StageError(f"找不到 {name} 可执行文件", hint=f"安装 ffmpeg 并加入 PATH，或设置 {name.upper()}_BIN 环境变量")


def ffmpeg_bin() -> str:
    return resolve_binary("ffmpeg", FFMPEG_CANDIDATES)


def ffprobe_bin() -> str:
    return resolve_binary("ffprobe", FFPROBE_CANDIDATES)


def run(cmd: list[str], logger: StageLogger | None = None, timeout: int = 3600, check: bool = True,
        cwd: str | os.PathLike | None = None) -> subprocess.CompletedProcess:
    """执行外部命令，失败时把 stderr 尾部带进异常，便于排查。"""
    if logger:
        logger.log("exec: " + " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, cwd=cwd)
    if check and proc.returncode != 0:
        tail = (proc.stderr or "")[-2000:]
        raise StageError(f"命令失败（退出码 {proc.returncode}）：{' '.join(cmd[:6])} ...\n{tail}",
                         hint="查看同目录 logs/ 下的阶段日志获取完整 stderr")
    return proc


def ffprobe_json(path: str | os.PathLike) -> dict:
    cmd = [ffprobe_bin(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    proc = run(cmd)
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise StageError(f"ffprobe 输出不是合法 JSON：{path}", hint=str(exc)) from exc


def parse_rate(value: str | None) -> float:
    if not value:
        return 0.0
    if "/" in value:
        num, den = value.split("/", 1)
        try:
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        except ValueError:
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def media_info(path: str | os.PathLike) -> dict:
    """抽取单个媒体文件的关键元数据。"""
    data = ffprobe_json(path)
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = 0.0
    for candidate in (fmt.get("duration"), (video or {}).get("duration"), (audio or {}).get("duration")):
        try:
            if candidate:
                duration = float(candidate)
                break
        except (TypeError, ValueError):
            continue

    rotation = 0
    if video:
        for side in video.get("side_data_list", []) or []:
            if "rotation" in side:
                try:
                    rotation = int(float(side["rotation"]))
                except (TypeError, ValueError):
                    rotation = 0

    width = int(video.get("width", 0)) if video else 0
    height = int(video.get("height", 0)) if video else 0
    if abs(rotation) % 180 == 90:
        width, height = height, width

    return {
        "path": str(path),
        "duration": round(duration, 3),
        "width": width,
        "height": height,
        "fps": round(parse_rate((video or {}).get("avg_frame_rate") or (video or {}).get("r_frame_rate")), 3),
        "rotation": rotation,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "audio_codec": (audio or {}).get("codec_name", ""),
        "video_codec": (video or {}).get("codec_name", ""),
        "size_mb": round(float(fmt.get("size", 0)) / 1024 / 1024, 2),
        "bitrate": int(fmt.get("bit_rate", 0) or 0),
    }


def load_json(path: str | os.PathLike, default: Any = None) -> Any:
    p = pathlib.Path(path)
    if not p.exists():
        if default is not None:
            return default
        raise StageError(f"文件不存在：{p}", hint="先运行产出该文件的上游阶段脚本")
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: str | os.PathLike, data: Any) -> None:
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def ensure_dir(path: str | os.PathLike) -> pathlib.Path:
    p = pathlib.Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def rel(path: str | os.PathLike, start: str | os.PathLike) -> str:
    """尽量返回相对路径（用于生成可移植的清单文件）。"""
    try:
        return os.path.relpath(str(path), str(start)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


# ---------------------------------------------------------------- 字幕解析

_TIME_RE = re.compile(
    r"(?P<h>\d+):(?P<m>\d{1,2}):(?P<s>\d{1,2})[.,](?P<ms>\d{1,3})"
)


def _parse_timestamp(raw: str) -> float | None:
    m = _TIME_RE.search(raw)
    if not m:
        return None
    ms = m.group("ms").ljust(3, "0")
    return int(m.group("h")) * 3600 + int(m.group("m")) * 60 + int(m.group("s")) + int(ms) / 1000.0


def parse_subtitle_file(path: str | os.PathLike) -> list[dict]:
    """解析 srt / vtt / ass，返回 [{start, end, text}]。"""
    p = pathlib.Path(path)
    raw = p.read_text(encoding="utf-8", errors="replace")
    ext = p.suffix.lower()

    if ext in {".ass", ".ssa"}:
        return _parse_ass(raw)

    blocks = re.split(r"\r?\n\s*\r?\n", raw.strip())
    segments: list[dict] = []
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        if lines[0].upper().startswith("WEBVTT"):
            lines = lines[1:]
            if not lines:
                continue
        if lines[0].upper().startswith("NOTE"):
            continue
        if "-->" not in block:
            continue
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        start_raw, end_raw = lines[idx].split("-->", 1)
        start = _parse_timestamp(start_raw)
        end = _parse_timestamp(end_raw)
        if start is None or end is None:
            continue
        text = " ".join(lines[idx + 1:])
        text = re.sub(r"<[^>]+>", "", text)
        text = text.replace("&nbsp;", " ").strip()
        if text:
            segments.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    segments.sort(key=lambda s: s["start"])
    return segments


def _parse_ass(raw: str) -> list[dict]:
    segments: list[dict] = []
    in_events = False
    for line in raw.splitlines():
        low = line.strip().lower()
        if low.startswith("[events]"):
            in_events = True
            continue
        if in_events and low.startswith("[") and not low.startswith("[events]"):
            in_events = False
        if not in_events or not line.strip() or line.strip().startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) < 10:
            continue
        start = _parse_timestamp(parts[1])
        end = _parse_timestamp(parts[2])
        if start is None or end is None:
            continue
        text = re.sub(r"\{[^}]*\}", "", parts[9]).replace("\\N", " ").strip()
        if text:
            segments.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    segments.sort(key=lambda s: s["start"])
    return segments


def find_sidecar(video_path: str | os.PathLike) -> pathlib.Path | None:
    """在视频同目录找同名外挂字幕（srt/vtt/ass）。"""
    p = pathlib.Path(video_path)
    for ext in sorted(SIDECAR_EXTS):
        for cand in (p.with_suffix(ext), p.with_suffix(ext.upper())):
            if cand.exists():
                return cand
        for cand in p.parent.glob(p.stem + ".*"):
            if cand.suffix.lower() in SIDECAR_EXTS:
                return cand
    return None


def list_videos(media_dir: str | os.PathLike) -> list[pathlib.Path]:
    root = pathlib.Path(media_dir).expanduser().resolve()
    if not root.exists():
        raise StageError(f"素材目录不存在：{root}", hint="确认用户给出的视频路径")
    if root.is_file():
        return [root]
    videos = [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    if not videos:
        raise StageError(f"目录里没有找到视频文件：{root}",
                         hint=f"支持的扩展名：{', '.join(sorted(VIDEO_EXTS))}")
    return videos


def fmt_ts(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def parse_ts(raw: str) -> float:
    t = _parse_timestamp(raw)
    if t is None:
        raise StageError(f"无法解析时间戳：{raw}")
    return t


def eprint(*args) -> None:
    print(*args, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- .env 配置

def parse_env_file(path: str | os.PathLike) -> dict[str, str]:
    """解析 .env：支持空行、# 注释、export 前缀、单双引号包裹的值。"""
    values: dict[str, str] = {}
    p = pathlib.Path(path)
    try:
        raw = p.read_text(encoding="utf-8-sig")
    except OSError:
        return values
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("export "):
            text = text[len("export "):].strip()
        if "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def find_env_files(start: str | os.PathLike | None = None) -> list[pathlib.Path]:
    """按优先级找 .env（越靠前优先级越高）：

    1. start 目录下的 .env
    2. 从 start 逐层向上（项目根、其上级）
    3. 用户全局 ~/.outline-to-video/.env
    4. skill 根目录 .env（只读兜底，便于本机复用）
    """
    found: list[pathlib.Path] = []
    current = pathlib.Path(start).resolve() if start else pathlib.Path.cwd().resolve()
    for _ in range(8):
        candidate = current / ENV_FILENAME
        if candidate.exists() and candidate not in found:
            found.append(candidate)
        if current.parent == current:
            break
        current = current.parent
    global_env = pathlib.Path.home() / ".outline-to-video" / ENV_FILENAME
    if global_env.exists() and global_env not in found:
        found.append(global_env)
    skill_env = SKILL_ROOT / ENV_FILENAME
    if skill_env.exists() and skill_env not in found:
        found.append(skill_env)
    return found


class EnvConfig:
    """配置解析：命令行 > 进程环境变量 > .env 文件（就近优先）。

    只关心以 DOUBAO_ 开头的键，其余环境变量不读。
    """

    PREFIX = "DOUBAO_"

    def __init__(self, start: str | os.PathLike | None = None) -> None:
        self.files = find_env_files(start)
        self.values: dict[str, str] = {}
        self.sources: dict[str, str] = {}
        # 由远及近加载，近的覆盖远的
        for env_file in reversed(self.files):
            for key, value in parse_env_file(env_file).items():
                if not key.startswith(self.PREFIX):
                    continue
                self.values[key] = value
                self.sources[key] = str(env_file)
        # 进程环境变量优先级最高
        for key, value in os.environ.items():
            if key.startswith(self.PREFIX):
                self.values[key] = value
                self.sources[key] = "进程环境变量"

    def get(self, key: str, default: str | None = None) -> str | None:
        value = self.values.get(key)
        return value if value not in (None, "") else default

    def pick(self, cli_value: str | None, key: str, default: str | None = None) -> tuple[str | None, str]:
        """命令行给了就用命令行的，否则回落到环境变量/.env/默认值。"""
        if cli_value not in (None, ""):
            return cli_value, "命令行"
        value = self.get(key)
        if value is not None:
            return value, self.sources.get(key, ".env")
        return default, "默认值"

    @staticmethod
    def mask(value: str | None) -> str:
        """日志/配置回显时给密钥打码。"""
        if not value:
            return "(未设置)"
        if len(value) <= 8:
            return "*" * len(value)
        return f"{value[:4]}****{value[-4:]}"

    def report(self, keys: list[str]) -> list[str]:
        lines = []
        for key in keys:
            value = self.values.get(key)
            shown = self.mask(value) if "KEY" in key or "TOKEN" in key else (value or "(未设置)")
            lines.append(f"  {key} = {shown}   ← {self.sources.get(key, '未配置')}")
        return lines

    def ensure_env_file(self, directory: str | os.PathLike | None = None) -> pathlib.Path:
        """在工作目录里建一个可编辑的 .env 模板。

        刻意不建在 skill 目录里：skill 目录会被打包分发，密钥不该进包。
        """
        target_dir = pathlib.Path(directory) if directory else pathlib.Path.cwd()
        target_dir.mkdir(parents=True, exist_ok=True)
        env_file = target_dir / ENV_FILENAME
        if not env_file.exists():
            env_file.write_text(ENV_TEMPLATE, encoding="utf-8")
        return env_file
