from uploader.utils import CaptionContext, render_caption, truncate_caption


def test_render_caption_basic_substitution():
    ctx = CaptionContext(filename="movie", size="1.2 GB", duration="01:30", resolution="1920x1080", ext="mp4")
    result = render_caption("{filename} ({size}) {resolution}", ctx)
    assert result == "movie (1.2 GB) 1920x1080"


def test_render_caption_leaves_unknown_placeholders():
    ctx = CaptionContext(filename="movie")
    result = render_caption("{filename} - {unknown}", ctx)
    assert result == "movie - {unknown}"


def test_render_caption_appends_footer():
    ctx = CaptionContext(filename="movie")
    result = render_caption("{filename}", ctx, footer="Powered by uploader")
    assert result == "movie\nPowered by uploader"


def test_render_caption_empty_template_with_footer():
    ctx = CaptionContext()
    result = render_caption("", ctx, footer="footer only")
    assert result == "footer only"


def test_render_caption_index_variable():
    ctx = CaptionContext(filename="part", index="2/5")
    assert render_caption("{filename} [{index}]", ctx) == "part [2/5]"


def test_truncate_caption_under_limit_unchanged():
    assert truncate_caption("short caption", 1024) == "short caption"


def test_truncate_caption_over_limit():
    text = "x" * 2000
    result = truncate_caption(text, 1024)
    assert len(result) == 1024
    assert result.endswith("…")


def test_truncate_caption_exact_limit():
    text = "y" * 100
    assert truncate_caption(text, 100) == text
