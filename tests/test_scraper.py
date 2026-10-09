from uploader.scraper import parse_media_candidates


def test_generic_link_text_falls_back_to_heading_and_data_attrs():
    # Mirrors the real-world markup used by many Persian music-download sites
    # (WordPress "mk_mcbx"-style templates): every download button says the same
    # generic "دانلود کیفیت 320", but each <article> carries data-artist/data-song,
    # and the <h2> post title repeats it too.
    html = """
    <article data-artist="داریوش" data-song='سپید و سیاه'>
      <h2><a href="/p1">دانلود آهنگ داریوش سپید و سیاه</a></h2>
      <a href="/dl/a.mp3">دانلود کیفیت 320</a>
    </article>
    <article data-artist="داریوش" data-song='یاور همیشه مومن'>
      <h2><a href="/p2">دانلود آهنگ داریوش یاور همیشه مومن</a></h2>
      <a href="/dl/b.mp3">دانلود کیفیت 320</a>
    </article>
    """
    candidates = parse_media_candidates(html, "https://example.com/tag/x")
    labels = [c.label for c in candidates]
    assert labels == ["داریوش - سپید و سیاه (320)", "داریوش - یاور همیشه مومن (320)"]


def test_heading_used_when_no_data_attrs():
    html = """
    <article>
      <h2>Awesome Song Title</h2>
      <a href="/dl/a.mp3">Download</a>
    </article>
    """
    candidates = parse_media_candidates(html, "https://example.com/page")
    assert candidates[0].label == "Awesome Song Title"


def test_meaningful_link_text_is_not_overridden():
    html = """
    <article data-artist="X" data-song="Y">
      <h2>Some Heading</h2>
      <a href="/dl/a.mp3">My Actual Great Title</a>
    </article>
    """
    candidates = parse_media_candidates(html, "https://example.com/page")
    assert candidates[0].label == "My Actual Great Title"


def test_data_song_url_value_is_not_used_as_a_title():
    # Some audio-player widgets reuse the "data-song" attribute name for the
    # media URL itself (not a title) — that must never leak into a label.
    html = """
    <article>
      <span class="play" data-song="https://cdn.example.com/track.mp3">play</span>
      <a href="/dl/track.mp3">دانلود کیفیت 320</a>
    </article>
    """
    candidates = parse_media_candidates(html, "https://example.com/page")
    assert "http" not in candidates[0].label


def test_article_boundary_resets_stale_data_song():
    html = """
    <article data-artist="X" data-song="First Song">
      <a href="/dl/a.mp3">دانلود</a>
    </article>
    <article>
      <a href="/dl/b.mp3">دانلود</a>
    </article>
    """
    candidates = parse_media_candidates(html, "https://example.com/page")
    assert candidates[0].label == "X - First Song"
    assert candidates[1].label != "X - First Song"


def test_duplicate_labels_get_disambiguated():
    html = """
    <a href="/dl.mp3?id=1">دانلود</a>
    <a href="/dl.mp3?id=2">دانلود</a>
    """
    candidates = parse_media_candidates(html, "https://example.com/page")
    assert candidates[0].label != candidates[1].label


def test_og_audio_meta_tag_picked_up():
    html = '<meta property="og:audio" content="/media/track.mp3">'
    candidates = parse_media_candidates(html, "https://example.com/page")
    assert len(candidates) == 1
    assert candidates[0].kind == "audio"
    assert candidates[0].url == "https://example.com/media/track.mp3"


def test_non_media_links_are_ignored():
    html = '<a href="/about">About</a><a href="/dl/song.mp3">Song</a>'
    candidates = parse_media_candidates(html, "https://example.com/page")
    assert len(candidates) == 1
    assert candidates[0].url.endswith("song.mp3")


def test_duplicate_urls_are_deduplicated():
    html = """
    <a href="/dl/a.mp3">First mention</a>
    <a href="/dl/a.mp3">Second mention</a>
    """
    candidates = parse_media_candidates(html, "https://example.com/page")
    assert len(candidates) == 1
