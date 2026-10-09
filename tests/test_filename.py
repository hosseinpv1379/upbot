from uploader.utils import (
    filename_from_content_disposition,
    filename_from_url,
    parse_links_message,
    resolve_filename,
    sanitize_filename,
)


def test_sanitize_filename_strips_illegal_chars():
    assert sanitize_filename('bad:name*?.mp4') == "bad_name__.mp4"


def test_sanitize_filename_defaults_when_empty():
    assert sanitize_filename("   ") == "file"


def test_sanitize_filename_truncates_long_names():
    long_name = "a" * 300 + ".mp4"
    result = sanitize_filename(long_name, max_length=20)
    assert len(result) <= 20
    assert result.endswith(".mp4")


def test_content_disposition_simple():
    header = 'attachment; filename="movie.mp4"'
    assert filename_from_content_disposition(header) == "movie.mp4"


def test_content_disposition_rfc5987():
    header = "attachment; filename*=UTF-8''%D9%81%DB%8C%D9%84%D9%85.mp4"
    name = filename_from_content_disposition(header)
    assert name.endswith(".mp4")
    assert name != ""


def test_content_disposition_none():
    assert filename_from_content_disposition(None) is None


def test_filename_from_url():
    assert filename_from_url("https://example.com/videos/clip.mkv?x=1") == "clip.mkv"
    assert filename_from_url("https://example.com/") is None


def test_resolve_filename_prefers_content_disposition():
    name = resolve_filename(
        "https://example.com/download?id=1",
        content_disposition='attachment; filename="real.mp4"',
        content_type="video/mp4",
    )
    assert name == "real.mp4"


def test_resolve_filename_falls_back_to_url():
    name = resolve_filename("https://example.com/path/song.mp3")
    assert name == "song.mp3"


def test_resolve_filename_falls_back_to_content_type():
    name = resolve_filename("https://example.com/download?id=1", content_type="image/png")
    assert name.endswith(".png")


def test_parse_links_message_single_url_with_caption():
    text = "https://example.com/a.mp4\nMy caption\nsecond line"
    urls, caption = parse_links_message(text)
    assert urls == ["https://example.com/a.mp4"]
    assert caption == "My caption\nsecond line"


def test_parse_links_message_multiple_urls_with_separator():
    text = "https://example.com/a.mp4\nhttps://example.com/b.mp4\n---\nShared caption"
    urls, caption = parse_links_message(text)
    assert urls == ["https://example.com/a.mp4", "https://example.com/b.mp4"]
    assert caption == "Shared caption"


def test_parse_links_message_only_urls_no_caption():
    text = "https://example.com/a.mp4\nhttps://example.com/b.mp4"
    urls, caption = parse_links_message(text)
    assert len(urls) == 2
    assert caption == ""


def test_parse_links_message_no_urls():
    urls, caption = parse_links_message("just some text, no links")
    assert urls == []
