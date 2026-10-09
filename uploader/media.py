"""ffprobe/ffmpeg helpers: media metadata, thumbnails, and splitting large files."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv", ".wmv"}
AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".m4a", ".ogg", ".opus", ".aac"}


@dataclass
class VideoInfo:
    duration: float = 0.0
    width: int = 0
    height: int = 0


@dataclass
class AudioInfo:
    duration: float = 0.0
    title: str = ""
    performer: str = ""


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


async def _run(cmd: list[str], timeout: float = 60.0) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, stdout, stderr


async def probe_video(path: Path) -> VideoInfo:
    if not ffmpeg_available():
        return VideoInfo()
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        code, out, err = await _run(cmd)
        if code != 0:
            logger.warning("ffprobe failed for %s: %s", path, err.decode(errors="ignore"))
            return VideoInfo()
        data = json.loads(out.decode(errors="ignore") or "{}")
        streams = data.get("streams") or [{}]
        fmt = data.get("format") or {}
        width = int(streams[0].get("width") or 0)
        height = int(streams[0].get("height") or 0)
        duration = float(fmt.get("duration") or 0.0)
        return VideoInfo(duration=duration, width=width, height=height)
    except Exception:  # noqa: BLE001 - metadata is best-effort
        logger.exception("Failed to probe video %s", path)
        return VideoInfo()


async def probe_audio(path: Path) -> AudioInfo:
    if not ffmpeg_available():
        return AudioInfo()
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:format_tags=title,artist",
        "-of",
        "json",
        str(path),
    ]
    try:
        code, out, err = await _run(cmd)
        if code != 0:
            return AudioInfo()
        data = json.loads(out.decode(errors="ignore") or "{}")
        fmt = data.get("format") or {}
        tags = fmt.get("tags") or {}
        return AudioInfo(
            duration=float(fmt.get("duration") or 0.0),
            title=tags.get("title", ""),
            performer=tags.get("artist", ""),
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to probe audio %s", path)
        return AudioInfo()


async def generate_thumbnail(video_path: Path, duration: float, out_path: Optional[Path] = None) -> Optional[Path]:
    if not ffmpeg_available():
        return None
    out_path = out_path or video_path.with_suffix(".thumb.jpg")
    timestamp = max(duration * 0.1, 0.0)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{timestamp:.2f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=320:-1",
        str(out_path),
    ]
    try:
        code, _, err = await _run(cmd, timeout=30.0)
        if code != 0 or not out_path.exists():
            logger.warning("Thumbnail generation failed for %s: %s", video_path, err.decode(errors="ignore"))
            return None
        return out_path
    except Exception:  # noqa: BLE001
        logger.exception("Failed to generate thumbnail for %s", video_path)
        return None


async def split_file_binary(path: Path, part_size: int, out_dir: Optional[Path] = None) -> list[Path]:
    """Split any file into raw byte-range parts named `<name>.001`, `.002`, ... """
    out_dir = out_dir or path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    size = path.stat().st_size
    parts: list[Path] = []
    part_count = math.ceil(size / part_size)

    def _do_split() -> list[Path]:
        results = []
        with open(path, "rb") as src:
            for i in range(part_count):
                part_path = out_dir / f"{path.name}.{i + 1:03d}"
                with open(part_path, "wb") as dst:
                    remaining = part_size
                    while remaining > 0:
                        chunk = src.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        dst.write(chunk)
                        remaining -= len(chunk)
                results.append(part_path)
        return results

    loop = asyncio.get_running_loop()
    parts = await loop.run_in_executor(None, _do_split)
    return parts


async def split_video_ffmpeg(path: Path, segment_seconds: int, out_dir: Optional[Path] = None) -> list[Path]:
    """Split a video into playable segments via ffmpeg stream copy (keeps each part a valid video)."""
    if not ffmpeg_available():
        return await split_file_binary(path, segment_seconds * 1_000_000)
    out_dir = out_dir or path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = out_dir / f"{path.stem}.part%03d{path.suffix}"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(path),
        "-c",
        "copy",
        "-map",
        "0",
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-reset_timestamps",
        "1",
        str(pattern),
    ]
    code, _, err = await _run(cmd, timeout=600.0)
    if code != 0:
        logger.warning("Video split failed, falling back to binary split: %s", err.decode(errors="ignore"))
        return await split_file_binary(path, segment_seconds * 1_000_000)
    return sorted(out_dir.glob(f"{path.stem}.part*{path.suffix}"))


def classify_extension(ext: str) -> str:
    ext = ext.lower()
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return "document"
