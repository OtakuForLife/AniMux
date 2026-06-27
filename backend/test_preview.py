"""Unit tests for windowed preview helpers."""

from preview import (
    build_audio_cmd,
    build_mux_cmd,
    build_video_cmd,
    build_vtt_cmd,
    window_bounds,
    mkv_track_stream_index,
    _ffmpeg_esc_path,
    _is_bitmap_sub,
)


def test_window_bounds_midfile():
    start, end = window_bounds(600, 30, 3600)
    assert start == 570
    assert end == 630


def test_window_bounds_start():
    start, end = window_bounds(10, 30, 3600)
    assert start == 0
    assert end == 40


def test_window_bounds_end():
    start, end = window_bounds(3590, 30, 3600)
    assert end == 3600
    assert start == 3560


def test_mkv_track_stream_index():
    tracks = [
        {"id": 1, "type": "video"},
        {"id": 2, "type": "audio"},
        {"id": 3, "type": "subtitles"},
    ]
    assert mkv_track_stream_index(tracks, 1) == 0
    assert mkv_track_stream_index(tracks, 2) == 1
    assert mkv_track_stream_index(tracks, 3) == 2


def test_build_video_cmd():
    cmd = build_video_cmd("/d.mkv", 0, 60.0, 30.0, "/out.mp4")
    assert "-ss" in cmd and "60.000" in cmd
    assert cmd[-1] == "/out.mp4"
    assert "-an" in cmd


def test_build_audio_cmd_with_offset():
    cmd = build_audio_cmd("/s.mkv", 1, 0, 30, 500, "/a.m4a")
    assert "-itsoffset" in cmd
    assert "0.500" in cmd


def test_build_mux_cmd():
    cmd = build_mux_cmd("/v.mp4", "/a.m4a", "/p.mp4")
    assert "-c:v" in cmd and "copy" in cmd
    assert cmd[-1] == "/p.mp4"


def test_build_vtt_cmd():
    cmd = build_vtt_cmd("/f.mkv", 2, 10, 20, 0, "/s.vtt")
    assert "webvtt" in cmd
    assert cmd[-1] == "/s.vtt"
    assert cmd.index("-i") < cmd.index("-ss")
    cmd2 = build_vtt_cmd("/f.mkv", 2, 10, 20, 500, "/s.vtt")
    assert cmd2.index("-itsoffset") < cmd2.index("-i")


def test_clean_sub_text():
    from preview import clean_sub_text

    raw = r"{\1\c&HFFFFFF&\clip(195,4,214.5,59.5)\t(0,4496)}Die Tagebücher"
    assert clean_sub_text(raw) == "Die Tagebücher"
    assert clean_sub_text("line1\\Nline2") == "line1\nline2"


def test_normalize_vtt_window(tmp_path):
    from preview import normalize_vtt_window

    p = tmp_path / "s.vtt"
    p.write_text(
        "WEBVTT\n\n"
        "00:07:17.629 --> 00:07:20.000\n"
        "Hello\n",
        encoding="utf-8",
    )
    normalize_vtt_window(p, 437.629)
    text = p.read_text(encoding="utf-8")
    assert "00:00.000" in text or "00:00:00.000" in text
    assert "Hello" in text


def test_text_sub_vtt_ready(tmp_path):
    from preview import PreviewSession, PreviewTrack, text_sub_vtt_ready, window_dir

    session = PreviewSession(
        id="x", source_path="/s", dest_path="/d",
        source_track_ids=[], dest_track_ids=[],
        source_track_offsets={}, dest_track_offsets={},
        duration_sec=100,
        audio_tracks=[],
        sub_tracks=[PreviewTrack(
            key="source:1", side="source", track_id=1,
            kind="subtitles", label="sub", bitmap=False,
        )],
        cache_root=tmp_path / "cache",
    )
    wdir = window_dir(session, 0, 30)
    wdir.mkdir(parents=True, exist_ok=True)
    assert not text_sub_vtt_ready(session, 0, 30, "source:1")
    (wdir / "sub_source_1.vtt").write_text("WEBVTT\n\n", encoding="utf-8")
    assert text_sub_vtt_ready(session, 0, 30, "source:1")


def test_bitmap_sub_detection():
    assert _is_bitmap_sub("hdmv_pgs_subtitle")
    assert not _is_bitmap_sub("ass")


def test_list_cached_windows_empty(tmp_path):
    from preview import PreviewSession, list_cached_windows, PreviewTrack

    session = PreviewSession(
        id="x", source_path="/s", dest_path="/d",
        source_track_ids=[], dest_track_ids=[],
        source_track_offsets={}, dest_track_offsets={},
        duration_sec=100, audio_tracks=[], sub_tracks=[],
        cache_root=tmp_path / "cache",
    )
    assert list_cached_windows(session) == []


def test_list_cached_windows_parses_dirs(tmp_path):
    from preview import PreviewSession, list_cached_windows

    cache = tmp_path / "cache"
    w = cache / "w_60000_90000"
    w.mkdir(parents=True)
    (w / "video.mp4").write_bytes(b"x")
    session = PreviewSession(
        id="x", source_path="/s", dest_path="/d",
        source_track_ids=[], dest_track_ids=[],
        source_track_offsets={}, dest_track_offsets={},
        duration_sec=100, audio_tracks=[], sub_tracks=[],
        cache_root=cache,
    )
    wins = list_cached_windows(session)
    assert len(wins) == 1
    assert wins[0]["start"] == 60.0
    assert wins[0]["end"] == 90.0

    assert _ffmpeg_esc_path("C:/foo/bar.mkv") == "C\\:/foo/bar.mkv"
