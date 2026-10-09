import asyncio

import pytest

from uploader.progress import ProgressTracker, format_progress_text, render_bar


def test_render_bar_empty():
    assert render_bar(0.0, width=10) == "░" * 10


def test_render_bar_full():
    assert render_bar(1.0, width=10) == "█" * 10


def test_render_bar_half():
    bar = render_bar(0.5, width=10)
    assert bar.count("█") == 5
    assert bar.count("░") == 5


def test_render_bar_clamps_out_of_range():
    assert render_bar(-1.0, width=4) == "░" * 4
    assert render_bar(2.0, width=4) == "█" * 4


def test_format_progress_text_contains_percent_and_phase():
    text = format_progress_text("download", "movie.mp4", 50, 100, 10.0)
    assert "50.0%" in text
    assert "movie.mp4" in text
    assert "دانلود" in text


def test_format_progress_text_upload_phase_label():
    text = format_progress_text("upload", "movie.mp4", 10, 100, 1.0)
    assert "آپلود" in text


def test_format_progress_text_zero_total_handles_gracefully():
    text = format_progress_text("download", "movie.mp4", 0, 0, 0.0)
    assert "?" in text


async def test_progress_tracker_throttles_updates():
    calls = []

    async def sink(text: str) -> None:
        calls.append(text)

    tracker = ProgressTracker(sink=sink, title="file", phase="download", interval=60.0)
    await tracker.update(10, 100)
    await tracker.update(20, 100)
    await tracker.update(30, 100)

    assert len(calls) == 1  # only the first call goes through before the interval elapses


async def test_progress_tracker_always_flushes_on_completion():
    calls = []

    async def sink(text: str) -> None:
        calls.append(text)

    tracker = ProgressTracker(sink=sink, title="file", phase="upload", interval=60.0)
    await tracker.update(10, 100)
    await tracker.update(100, 100)  # done == total -> forced flush even inside the interval

    assert len(calls) == 2


async def test_progress_tracker_force_flag():
    calls = []

    async def sink(text: str) -> None:
        calls.append(text)

    tracker = ProgressTracker(sink=sink, title="file", phase="download", interval=60.0)
    await tracker.update(10, 100)
    await tracker.update(20, 100, force=True)

    assert len(calls) == 2
