"""Small stateless helpers: filename parsing, hashing, formatting, caption templates."""

from __future__ import annotations

import hashlib
import mimetypes
import re
import string
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from email.message import Message
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

_INVALID_FS_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_DEFAULT_NAME = "file"


def sanitize_filename(name: str, max_length: int = 180) -> str:
    """Strip characters that are illegal (or awkward) in filenames, keep it bounded."""
    name = unicodedata.normalize("NFC", name).strip()
    name = _INVALID_FS_CHARS.sub("_", name)
    name = name.strip(" .")
    if not name:
        return _DEFAULT_NAME
    if len(name) > max_length:
        stem, ext = Path(name).stem, Path(name).suffix
        keep = max_length - len(ext)
        name = stem[: max(keep, 1)] + ext
    return name


def filename_from_content_disposition(header: Optional[str]) -> Optional[str]:
    """RFC 6266 aware extraction (handles filename and filename*=UTF-8''...)."""
    if not header:
        return None
    msg = Message()
    msg["content-disposition"] = header
    filename = msg.get_filename()
    if filename:
        return sanitize_filename(unquote(filename))
    # Fall back to manual parsing for filename*= (RFC 5987) if get_filename missed it.
    match = re.search(r"filename\*\s*=\s*([^;]+)", header, flags=re.IGNORECASE)
    if match:
        value = match.group(1).strip().strip('"')
        if "''" in value:
            _, _, value = value.partition("''")
        return sanitize_filename(unquote(value))
    return None


def filename_from_url(url: str) -> Optional[str]:
    path = urlparse(url).path
    name = unquote(Path(path).name)
    return sanitize_filename(name) if name else None


def extension_from_content_type(content_type: Optional[str]) -> str:
    if not content_type:
        return ""
    content_type = content_type.split(";")[0].strip()
    ext = mimetypes.guess_extension(content_type)
    return ext or ""


def resolve_filename(
    url: str,
    content_disposition: Optional[str] = None,
    content_type: Optional[str] = None,
) -> str:
    """Priority: Content-Disposition > URL path > Content-Type (extension only, generic stem)."""
    name = filename_from_content_disposition(content_disposition)
    if name:
        return name

    name = filename_from_url(url)
    if name and "." in name:
        return name

    ext = extension_from_content_type(content_type)
    if name:
        return name + ext if ext and not name.endswith(ext) else name
    return _DEFAULT_NAME + ext


def human_size(num_bytes: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024.0
    return f"{size:.2f} TB"


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def sha256_partial(path: Path, size: int, chunk: int = 1024 * 1024) -> str:
    """Hash the first MB + last MB + file size — cheap dedup fingerprint for large files."""
    hasher = hashlib.sha256()
    hasher.update(str(size).encode())
    with open(path, "rb") as fh:
        head = fh.read(chunk)
        hasher.update(head)
        if size > chunk:
            fh.seek(max(size - chunk, 0))
            tail = fh.read(chunk)
            hasher.update(tail)
    return hasher.hexdigest()


def sha256_url(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


_SAFE_FORMATTER = string.Formatter()


class _SilentDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass
class CaptionContext:
    filename: str = ""
    size: str = ""
    duration: str = ""
    resolution: str = ""
    ext: str = ""
    date: str = ""
    index: str = ""

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "size": self.size,
            "duration": self.duration,
            "resolution": self.resolution,
            "ext": self.ext,
            "date": self.date,
            "index": self.index,
        }


def render_caption(template: str, context: CaptionContext, footer: str = "") -> str:
    """Render {variable} placeholders, leaving unknown ones untouched instead of raising."""
    rendered = _SAFE_FORMATTER.vformat(template, (), _SilentDict(context.as_dict()))
    if footer:
        rendered = f"{rendered}\n{footer}" if rendered else footer
    return rendered


def truncate_caption(caption: str, limit: int) -> str:
    if len(caption) <= limit:
        return caption
    suffix = "…"
    return caption[: max(limit - len(suffix), 0)] + suffix


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def parse_links_message(text: str) -> tuple[list[str], str]:
    """Parse a message where the first block of lines are URLs and the rest is the caption.

    Multiple URLs can be given (one per line) followed by an optional ``---`` separator
    and a shared caption. If no separator is present, the first line is the URL and
    everything after it is the caption (single-link mode).
    """
    lines = text.strip().splitlines()
    if not lines:
        return [], ""

    url_re = re.compile(r"^https?://\S+$", re.IGNORECASE)

    if any(line.strip() == "---" for line in lines):
        sep_index = next(i for i, line in enumerate(lines) if line.strip() == "---")
        url_lines = lines[:sep_index]
        caption_lines = lines[sep_index + 1 :]
    else:
        url_lines = []
        caption_lines = []
        for i, line in enumerate(lines):
            if url_re.match(line.strip()):
                url_lines.append(line)
            else:
                caption_lines = lines[i:]
                break
        else:
            caption_lines = []

    urls = [line.strip() for line in url_lines if url_re.match(line.strip())]
    caption = "\n".join(caption_lines).strip()
    return urls, caption


def free_disk_space(path: Path) -> int:
    import shutil

    usage = shutil.disk_usage(path if path.exists() else path.parent)
    return usage.free
