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
    source_track_map=TRACK_MAP,
    dest_track_map=TRACK_MAP,
    chapters=True,
    attachments=True,
    tags=True,
    output_path="/dest/movie.mkv.animux.tmp",
)

ALL_DEST = [0, 1, 2, 3, 4]


def _dest_opts(cmd: list[str]) -> list[str]:
    dest_i = cmd.index("/dest/movie.mkv")
    return cmd[3:dest_i]


def _source_opts(cmd: list[str]) -> list[str]:
    dest_i = cmd.index("/dest/movie.mkv")
    src_i = cmd.index("/src/movie.mkv")
    return cmd[dest_i + 1 : src_i]


def test_dest_video_only():
    cmd = build_mkvmerge_cmd(**{**BASE, "source_track_ids": [1], "dest_track_ids": [0]})
    dest_opts = _dest_opts(cmd)
    assert dest_opts == ["--video-tracks", "0", "--no-audio", "--no-subtitles"]


def test_dest_drops_unselected_audio():
    cmd = build_mkvmerge_cmd(**{**BASE, "source_track_ids": [], "dest_track_ids": [0, 2, 3]})
    dest_opts = _dest_opts(cmd)
    assert dest_opts.index("--audio-tracks") == 2
    assert dest_opts[dest_opts.index("--audio-tracks") + 1] == "2"
    assert "--video-tracks" in dest_opts


def test_audio_only_transfer():
    cmd = build_mkvmerge_cmd(**{**BASE, "source_track_ids": [1, 2], "dest_track_ids": ALL_DEST})
    src_opts = _source_opts(cmd)
    assert src_opts[0] == "--no-video"
    idx = src_opts.index("--audio-tracks")
    assert src_opts[idx + 1] == "1,2"
    assert "--no-subtitles" in src_opts
    assert cmd[-1] == "/src/movie.mkv"


def test_subtitle_only_transfer():
    cmd = build_mkvmerge_cmd(**{**BASE, "source_track_ids": [3, 4], "dest_track_ids": ALL_DEST})
    src_opts = _source_opts(cmd)
    idx_sub = src_opts.index("--subtitle-tracks")
    assert src_opts[idx_sub + 1] == "3,4"
    assert "--no-audio" in src_opts


def test_no_chapters_flag_present_when_false():
    cmd = build_mkvmerge_cmd(**{**BASE, "source_track_ids": [], "dest_track_ids": ALL_DEST, "chapters": False})
    assert "--no-chapters" in _source_opts(cmd)


def test_no_chapters_flag_absent_when_true():
    cmd = build_mkvmerge_cmd(**{**BASE, "source_track_ids": [], "dest_track_ids": ALL_DEST, "chapters": True})
    assert "--no-chapters" not in _source_opts(cmd)


def test_no_attachments_flag():
    cmd_off = build_mkvmerge_cmd(**{**BASE, "source_track_ids": [], "dest_track_ids": ALL_DEST, "attachments": False})
    assert "--no-attachments" in _source_opts(cmd_off)

    cmd_on = build_mkvmerge_cmd(**{**BASE, "source_track_ids": [], "dest_track_ids": ALL_DEST, "attachments": True})
    assert "--no-attachments" not in _source_opts(cmd_on)


def test_no_tags_flag():
    cmd_off = build_mkvmerge_cmd(**{**BASE, "source_track_ids": [], "dest_track_ids": ALL_DEST, "tags": False})
    src_opts = _source_opts(cmd_off)
    assert "--no-global-tags" in src_opts
    assert "--no-track-tags" in src_opts

    cmd_on = build_mkvmerge_cmd(**{**BASE, "source_track_ids": [], "dest_track_ids": ALL_DEST, "tags": True})
    src_opts_on = _source_opts(cmd_on)
    assert "--no-global-tags" not in src_opts_on
    assert "--no-track-tags" not in src_opts_on


def test_output_path_is_animux_tmp():
    output = "/dest/movie.mkv.animux.tmp"
    cmd = build_mkvmerge_cmd(**{**BASE, "source_track_ids": [], "dest_track_ids": ALL_DEST, "output_path": output})
    assert cmd[cmd.index("-o") + 1] == output
    assert cmd[cmd.index("-o") + 1].endswith(".animux.tmp")


def test_source_file_is_last():
    cmd = build_mkvmerge_cmd(**{**BASE, "source_track_ids": [1], "dest_track_ids": ALL_DEST})
    assert cmd[-1] == "/src/movie.mkv"
