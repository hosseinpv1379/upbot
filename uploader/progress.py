"""Throttled progress tracking and rendering for download/upload callbacks."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from uploader.utils import human_duration, human_size

Phase = str  # "download" | "upload"


def render_bar(fraction: float, width: int = 18) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    return "█" * filled + "░" * (width - filled)


def format_progress_text(
    phase: Phase,
    title: str,
    done: int,
    total: int,
    speed_bytes_per_sec: float,
    extra_lines: Optional[list[str]] = None,
) -> str:
    fraction = (done / total) if total else 0.0
    percent = fraction * 100
    bar = render_bar(fraction)
    eta = (total - done) / speed_bytes_per_sec if speed_bytes_per_sec > 0 and total else 0
    phase_fa = "دانلود" if phase == "download" else "آپلود"
    lines = [
        f"📦 {title}",
        f"{phase_fa}: {bar} {percent:5.1f}%",
        f"{human_size(done)} / {human_size(total) if total else '?'}",
        f"سرعت: {human_size(speed_bytes_per_sec)}/s   ETA: {human_duration(eta)}",
    ]
    if extra_lines:
        lines.extend(extra_lines)
    return "\n".join(lines)


ProgressCallback = Callable[[int, int], Awaitable[None]]


@dataclass
class ProgressTracker:
    """Wraps a slow async "edit message" call so it fires at most every `interval` seconds.

    Usage: call `update(done, total)` frequently (e.g. from a download/upload callback);
    internally it recomputes speed via an exponential moving average and only awaits the
    provided `sink` coroutine at most once per `interval` seconds (always flushing the
    final 100% update).
    """

    sink: Callable[[str], Awaitable[None]]
    title: str
    phase: Phase
    interval: float = 4.0
    extra_lines_provider: Optional[Callable[[], list[str]]] = None

    _last_emit: float = field(default=0.0, init=False)
    _last_done: int = field(default=0, init=False)
    _last_time: float = field(default_factory=time.monotonic, init=False)
    _speed: float = field(default=0.0, init=False)
    _lock_busy: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self._last_time = time.monotonic()

    def _update_speed(self, done: int) -> None:
        now = time.monotonic()
        dt = now - self._last_time
        if dt > 0:
            instantaneous = (done - self._last_done) / dt
            # Exponential moving average smooths out bursty chunk arrivals.
            alpha = 0.3
            self._speed = (
                instantaneous if self._speed == 0 else alpha * instantaneous + (1 - alpha) * self._speed
            )
        self._last_done = done
        self._last_time = now

    async def update(self, done: int, total: int, force: bool = False) -> None:
        self._update_speed(done)
        now = time.monotonic()
        is_final = total > 0 and done >= total
        if not force and not is_final and (now - self._last_emit) < self.interval:
            return
        if self._lock_busy:
            return
        self._lock_busy = True
        try:
            self._last_emit = now
            extra = self.extra_lines_provider() if self.extra_lines_provider else None
            text = format_progress_text(self.phase, self.title, done, total, self._speed, extra)
            await self.sink(text)
        finally:
            self._lock_busy = False
