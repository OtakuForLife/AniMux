"""Unit tests for build_mkvmerge_cmd — no subprocess or mkvmerge binary needed."""

from remux import build_mkvmerge_cmd


TRACK_MAP = {
    0: "video",
    1: "audio",
    2: "audio",
    3: "subtitles",
    4: "subtitles",
}

BASE = dict(
    dest_path="/dest/movie.mkv",
    source_path="/src/movie.mkv",
    track_map=TRACK_MAP,
    chapters=True,
    attachments=True,
    tags=True,
    output_path="/dest/movie.mkv.animux.tmp",
)


def test_audio_only_transfer():
    cmd = build_mkvmerge_cmd(**{**BASE, "track_ids": [1, 2]})
    assert "--audio-tracks" in cmd
    idx = cmd.index("--audio-tracks")
    assert cmd[idx + 1] == "1,2"
    idx_sub = cmd.index("--subtitle-tracks")
    assert cmd[idx_sub + 1] == ""


def test_subtitle_only_transfer():
    cmd = build_mkvmerge_cmd(**{**BASE, "track_ids": [3, 4]})
    idx_sub = cmd.index("--subtitle-tracks")
    assert cmd[idx_sub + 1] == "3,4"
    idx_aud = cmd.index("--audio-tracks")
    assert cmd[idx_aud + 1] == ""


def test_no_chapters_flag_present_when_false():
    cmd = build_mkvmerge_cmd(**{**BASE, "track_ids": [], "chapters": False})
    assert "--no-chapters" in cmd


def test_no_chapters_flag_absent_when_true():
    cmd = build_mkvmerge_cmd(**{**BASE, "track_ids": [], "chapters": True})
    assert "--no-chapters" not in cmd


def test_no_attachments_flag():
    cmd_off = build_mkvmerge_cmd(**{**BASE, "track_ids": [], "attachments": False})
    assert "--no-attachments" in cmd_off

    cmd_on = build_mkvmerge_cmd(**{**BASE, "track_ids": [], "attachments": True})
    assert "--no-attachments" not in cmd_on


def test_no_tags_flag():
    cmd_off = build_mkvmerge_cmd(**{**BASE, "track_ids": [], "tags": False})
    assert "--no-tags" in cmd_off

    cmd_on = build_mkvmerge_cmd(**{**BASE, "track_ids": [], "tags": True})
    assert "--no-tags" not in cmd_on


def test_output_path_is_animux_tmp():
    output = "/dest/movie.mkv.animux.tmp"
    cmd = build_mkvmerge_cmd(**{**BASE, "track_ids": [], "output_path": output})
    assert cmd[cmd.index("-o") + 1] == output
    assert cmd[cmd.index("-o") + 1].endswith(".animux.tmp")


def test_source_always_has_video_tracks_excluded():
    cmd = build_mkvmerge_cmd(**{**BASE, "track_ids": [1]})
    assert "--video-tracks" in cmd
    idx = cmd.index("--video-tracks")
    assert cmd[idx + 1] == ""
