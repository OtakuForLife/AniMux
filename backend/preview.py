"""Windowed ffmpeg preview — clip T±X, cache video/audio/subs, fast track switching."""

import asyncio
import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

PREVIEW_CACHE_DIR = os.environ.get("PREVIEW_CACHE_DIR", "/tmp/animux-preview")
PREVIEW_WINDOW_SEC = int(os.environ.get("PREVIEW_WINDOW", "30"))

_BITMAP_SUB = frozenset({"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"})


@dataclass
class PreviewTrack:
    key: str          # "dest:1" | "source:2"
    side: str         # dest | source
    track_id: int
    kind: str         # audio | subtitles
    label: str
    codec: str = ""
    bitmap: bool = False


@dataclass
class PreviewSession:
    id: str
    source_path: str
    dest_path: str
    source_track_ids: list[int]
    dest_track_ids: list[int]
    source_track_offsets: dict[int, int]
    dest_track_offsets: dict[int, int]
    duration_sec: float
    audio_tracks: list[PreviewTrack]
    sub_tracks: list[PreviewTrack]
    cache_root: Path


@dataclass
class PreviewJob:
    id: str
    session_id: str
    status: str = "pending"  # pending | running | done | error
    progress_pct: int = 0
    log: list[str] = field(default_factory=list)
    error: str | None = None
    window_start: float = 0.0
    window_end: float = 0.0
    playback_path: str | None = None
    playback_url: str | None = None
    vtt_tracks: list[dict] = field(default_factory=list)
    audio_key: str | None = None
    sub_key: str | None = None
    sub_bitmap: bool = False


SESSIONS: dict[str, PreviewSession] = {}
JOBS: dict[str, PreviewJob] = {}


def _ffmpeg_esc_path(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "'\\''")


def probe_duration(path: str) -> float:
    r = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "json", path,
        ],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return 0.0
    data = json.loads(r.stdout)
    return float(data.get("format", {}).get("duration", 0) or 0)


def probe_file_tracks(path: str) -> list[dict]:
    r = subprocess.run(
        ["mkvmerge", "-J", path],
        capture_output=True, text=True,
    )
    if r.returncode not in (0, 1):
        return []
    return json.loads(r.stdout).get("tracks", [])


def mkv_track_stream_index(tracks: list[dict], track_id: int) -> int:
    idx = 0
    for t in tracks:
        if t["id"] == track_id:
            return idx
        if t["type"] in ("video", "audio", "subtitles"):
            idx += 1
    raise ValueError(f"track id {track_id} not found")


def _track_label(t: dict) -> str:
    props = t.get("properties", {})
    parts = []
    lang = props.get("language", "")
    if lang and lang != "und":
        parts.append(lang.upper())
    codec = t.get("codec", "")
    if codec:
        parts.append(codec)
    name = props.get("track_name", "")
    if name:
        parts.append(f'"{name}"')
    return " · ".join(parts) or f"track {t['id']}"


def _is_bitmap_sub(codec: str) -> bool:
    return codec.lower() in _BITMAP_SUB or "pgs" in codec.lower()


def build_preview_tracks(
    source_path: str,
    dest_path: str,
    source_track_ids: list[int],
    dest_track_ids: list[int],
    source_tracks: list[dict],
    dest_tracks: list[dict],
) -> tuple[list[PreviewTrack], list[PreviewTrack]]:
    audio: list[PreviewTrack] = []
    subs: list[PreviewTrack] = []

    for side, path, ids, raw in (
        ("dest", dest_path, dest_track_ids, dest_tracks),
        ("source", source_path, source_track_ids, source_tracks),
    ):
        id_set = set(ids)
        for t in raw:
            if t["id"] not in id_set:
                continue
            typ = t["type"]
            if typ == "video":
                continue
            codec = t.get("codec", "")
            if typ == "audio":
                audio.append(PreviewTrack(
                    key=f"{side}:{t['id']}", side=side, track_id=t["id"],
                    kind="audio", label=f"[{side}] {_track_label(t)}", codec=codec,
                ))
            elif typ in ("subtitles", "subtitle"):
                subs.append(PreviewTrack(
                    key=f"{side}:{t['id']}", side=side, track_id=t["id"],
                    kind="subtitles", label=f"[{side}] {_track_label(t)}", codec=codec,
                    bitmap=_is_bitmap_sub(codec),
                ))
    return audio, subs


def window_bounds(t_sec: float, window_sec: int, duration: float) -> tuple[float, float]:
    half = max(5, window_sec)
    start = max(0.0, t_sec - half)
    end = min(duration, t_sec + half)
    if end - start < 10 and duration >= 10:
        if start == 0:
            end = min(duration, 10.0)
        elif end == duration:
            start = max(0.0, duration - 10.0)
    return start, end


def window_dir(session: PreviewSession, start: float, end: float) -> Path:
    d = session.cache_root / f"w_{int(start * 1000)}_{int(end * 1000)}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_video_cmd(path: str, stream_idx: int, start: float, dur: float, out: str) -> list[str]:
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
        "-i", path,
        "-map", f"0:{stream_idx}",
        "-vf", "scale=-2:720",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-an", out,
    ]


def build_audio_cmd(
    path: str, stream_idx: int, start: float, dur: float,
    offset_ms: int, out: str,
) -> list[str]:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
    ]
    if offset_ms:
        cmd.extend(["-itsoffset", f"{offset_ms / 1000:.3f}"])
    cmd.extend([
        "-i", path,
        "-map", f"0:{stream_idx}",
        "-c:a", "aac", "-b:a", "128k",
        out,
    ])
    return cmd


def build_vtt_cmd(
    path: str, stream_idx: int, start: float, dur: float, offset_ms: int, out: str,
) -> list[str]:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if offset_ms:
        cmd.extend(["-itsoffset", f"{offset_ms / 1000:.3f}"])
    cmd.extend([
        "-i", path,
        "-ss", f"{start:.3f}",
        "-t", f"{dur:.3f}",
        "-map", f"0:{stream_idx}",
        "-c:s", "webvtt",
        out,
    ])
    return cmd


_VTT_TIME = re.compile(
    r"(\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}\s*-->\s*(\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}"
)


def clean_sub_text(text: str) -> str:
    """Strip ASS/SSA override tags that ffmpeg leaves in webvtt cues."""
    out = re.sub(r"\{[^}]*\}", "", text)
    out = out.replace("\\N", "\n").replace("\\n", "\n")
    out = re.sub(r"<[^>]+>", "", out)
    return out.strip()


def clean_vtt_file(path: Path) -> None:
    raw = path.read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"\n\n+", raw.replace("\r", ""))
    out_blocks: list[str] = []
    for block in blocks:
        if block.startswith("WEBVTT") or not block.strip():
            out_blocks.append(block)
            continue
        m = _VTT_TIME.search(block)
        if not m:
            out_blocks.append(block)
            continue
        head = block[: m.end()]
        body = clean_sub_text(block[m.end() :])
        if body:
            out_blocks.append(f"{head}\n{body}")
    path.write_text("\n\n".join(out_blocks) + "\n", encoding="utf-8")


def _vtt_ts_sec(raw: str) -> float:
    raw = raw.strip().replace(",", ".")
    parts = [float(p) for p in raw.split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return parts[0] * 60 + parts[1]


def _vtt_ts_str(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    if h:
        return f"{h:02d}:{m:02d}:{s:06.3f}"
    return f"{m:02d}:{s:06.3f}"


def normalize_vtt_window(path: Path, window_start: float) -> None:
    """Shift absolute cue times to 0-based window timeline."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"\n\n+", raw.replace("\r", ""))
    out_blocks: list[str] = []
    for block in blocks:
        if block.startswith("WEBVTT") or not block.strip():
            out_blocks.append(block)
            continue
        m = _VTT_TIME.search(block)
        if not m:
            out_blocks.append(block)
            continue
        start_s, end_s = [x.strip() for x in m.group(0).split("-->")]
        start = _vtt_ts_sec(start_s)
        end = _vtt_ts_sec(end_s)
        if start >= window_start - 1.0:
            start -= window_start
            end -= window_start
        head = f"{_vtt_ts_str(start)} --> {_vtt_ts_str(end)}"
        body = block[m.end() :].strip()
        if body:
            out_blocks.append(f"{head}\n{body}")
    path.write_text("\n\n".join(out_blocks) + "\n", encoding="utf-8")


def build_mux_cmd(video: str, audio: str | None, out: str) -> list[str]:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", video]
    if audio:
        cmd.extend(["-i", audio, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k"])
    else:
        cmd.extend(["-map", "0:v:0", "-c:v", "copy"])
    cmd.extend(["-movflags", "+faststart", out])
    return cmd


def build_pgs_window_cmd(
    dest_path: str,
    sub_path: str,
    sub_stream: int,
    audio_path: str | None,
    audio_stream: int | None,
    audio_offset_ms: int,
    start: float,
    dur: float,
    out: str,
) -> list[str]:
    esc = _ffmpeg_esc_path(sub_path)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
        "-i", dest_path,
    ]
    if audio_path:
        if audio_offset_ms:
            cmd.extend(["-itsoffset", f"{audio_offset_ms / 1000:.3f}"])
        cmd.extend(["-i", audio_path])
    cmd.extend(["-map", "0:v:0"])
    if audio_path:
        cmd.extend(["-map", "1:a:0"])
    cmd.extend([
        "-vf", f"scale=-2:720,subtitles='{esc}':si={sub_stream}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
    ])
    if audio_path:
        cmd.extend(["-c:a", "aac", "-b:a", "128k"])
    else:
        cmd.extend(["-an"])
    cmd.extend(["-movflags", "+faststart", out])
    return cmd


async def _run_ffmpeg(cmd: list[str], job: PreviewJob | None = None, progress_weight: int = 0) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()
        raise RuntimeError(err or "ffmpeg failed")
    if job and progress_weight:
        job.progress_pct = min(99, job.progress_pct + progress_weight)


def create_session(
    source_path: str,
    dest_path: str,
    source_track_ids: list[int],
    dest_track_ids: list[int],
    source_track_offsets: dict[int, int],
    dest_track_offsets: dict[int, int],
) -> PreviewSession:
    session_id = str(uuid.uuid4())
    cache_root = Path(PREVIEW_CACHE_DIR) / session_id
    cache_root.mkdir(parents=True, exist_ok=True)

    src_tracks = probe_file_tracks(source_path)
    dst_tracks = probe_file_tracks(dest_path)
    duration = probe_duration(dest_path) or probe_duration(source_path)

    audio, subs = build_preview_tracks(
        source_path, dest_path,
        source_track_ids, dest_track_ids,
        src_tracks, dst_tracks,
    )

    session = PreviewSession(
        id=session_id,
        source_path=source_path,
        dest_path=dest_path,
        source_track_ids=source_track_ids,
        dest_track_ids=dest_track_ids,
        source_track_offsets=source_track_offsets,
        dest_track_offsets=dest_track_offsets,
        duration_sec=duration,
        audio_tracks=audio,
        sub_tracks=subs,
        cache_root=cache_root,
    )
    SESSIONS[session_id] = session
    return session


def session_public(session: PreviewSession) -> dict:
    return {
        "session_id": session.id,
        "duration_sec": session.duration_sec,
        "window_sec": PREVIEW_WINDOW_SEC,
        "audio_tracks": [{"key": t.key, "label": t.label} for t in session.audio_tracks],
        "sub_tracks": [
            {"key": t.key, "label": t.label, "bitmap": t.bitmap}
            for t in session.sub_tracks
        ],
    }


def _track_by_key(session: PreviewSession, key: str | None) -> PreviewTrack | None:
    if not key:
        return None
    for t in session.audio_tracks + session.sub_tracks:
        if t.key == key:
            return t
    return None


def _offset_for(session: PreviewSession, track: PreviewTrack) -> int:
    if track.side == "source":
        return session.source_track_offsets.get(track.track_id, 0)
    return session.dest_track_offsets.get(track.track_id, 0)


def _file_for(session: PreviewSession, side: str) -> str:
    return session.dest_path if side == "dest" else session.source_path


def update_session_offsets(
    session: PreviewSession,
    source_offsets: dict[int, int],
    dest_offsets: dict[int, int],
) -> None:
    session.source_track_offsets = source_offsets
    session.dest_track_offsets = dest_offsets


def invalidate_window_playback(wdir: Path) -> None:
    """Drop muxed/audio/sub cache so the window re-encodes with new offsets or tracks."""
    for pattern in ("audio_*.m4a", "play_*.mp4", "baked_*.mp4", "sub_*.vtt"):
        for p in wdir.glob(pattern):
            p.unlink(missing_ok=True)


async def start_window_job(
    session_id: str,
    t_sec: float,
    window_sec: int,
    audio_key: str | None,
    sub_key: str | None,
    prefetch_all: bool = True,
    force: bool = False,
) -> str:
    session = SESSIONS.get(session_id)
    if session is None:
        raise KeyError("session not found")

    job_id = str(uuid.uuid4())
    start, end = window_bounds(t_sec, window_sec, session.duration_sec)
    dur = end - start

    job = PreviewJob(
        id=job_id, session_id=session_id,
        window_start=start, window_end=end,
        audio_key=audio_key, sub_key=sub_key,
    )
    JOBS[job_id] = job
    asyncio.create_task(_run_window_job(session, job, dur, prefetch_all, force))
    return job_id


def text_sub_vtt_ready(
    session: PreviewSession, window_start: float, window_end: float, sub_key: str | None,
) -> bool:
    if not sub_key:
        return True
    tr = _track_by_key(session, sub_key)
    if not tr or tr.bitmap:
        return True
    wdir = window_dir(session, window_start, window_end)
    return (wdir / f"sub_{sub_key.replace(':', '_')}.vtt").exists()


def try_cached_playback(
    session: PreviewSession,
    window_start: float,
    window_end: float,
    audio_key: str | None,
    sub_key: str | None,
) -> dict | None:
    """Return immediate playback info if assets are already cached."""
    wdir = window_dir(session, window_start, window_end)
    if not (wdir / "video.mp4").exists():
        return None

    sub_tr = _track_by_key(session, sub_key)
    if sub_tr and sub_tr.bitmap:
        baked = wdir / f"baked_{sub_key.replace(':', '_')}.mp4"
        if not baked.exists():
            return None
        out = _playback_response(session, wdir, str(baked), sub_key, sub_tr.bitmap)
    else:
        playback = wdir / f"play_{audio_key or 'novid'}.mp4"
        if audio_key:
            ap = wdir / f"audio_{audio_key.replace(':', '_')}.m4a"
            if not ap.exists():
                return None
        if not playback.exists():
            return None
        out = _playback_response(session, wdir, str(playback), sub_key, False)

    out["window_start"] = window_start
    out["window_end"] = window_end
    return out


def _playback_response(
    session: PreviewSession, wdir: Path, playback: str,
    sub_key: str | None, sub_bitmap: bool,
) -> dict:
    vtt = []
    if not sub_bitmap:
        for t in session.sub_tracks:
            if t.bitmap:
                continue
            vp = wdir / f"sub_{t.key.replace(':', '_')}.vtt"
            if vp.exists():
                vtt.append({
                    "key": t.key,
                    "label": t.label,
                    "url": f"/api/preview/files/{session.id}/{wdir.name}/{vp.name}",
                })
    return {
        "status": "done",
        "progress_pct": 100,
        "playback_url": f"/api/preview/files/{session.id}/{wdir.name}/{Path(playback).name}",
        "vtt_tracks": vtt,
        "sub_bitmap": sub_bitmap,
        "sub_key": sub_key,
    }


async def start_switch_job(
    session_id: str,
    window_start: float,
    window_end: float,
    audio_key: str | None,
    sub_key: str | None,
) -> str:
    session = SESSIONS.get(session_id)
    if session is None:
        raise KeyError("session not found")

    job_id = str(uuid.uuid4())
    job = PreviewJob(
        id=job_id, session_id=session_id,
        window_start=window_start, window_end=window_end,
        audio_key=audio_key, sub_key=sub_key,
    )
    JOBS[job_id] = job
    asyncio.create_task(_run_switch_job(session, job))
    return job_id


async def _run_window_job(
    session: PreviewSession, job: PreviewJob, dur: float, prefetch_all: bool, force: bool = False,
) -> None:
    job.status = "running"
    wdir = window_dir(session, job.window_start, job.window_end)
    video_path = wdir / "video.mp4"

    try:
        if force:
            invalidate_window_playback(wdir)

        dst_tracks = probe_file_tracks(session.dest_path)
        v_idx = mkv_track_stream_index(dst_tracks, next(t["id"] for t in dst_tracks if t["type"] == "video"))

        if not video_path.exists():
            job.log.append("Encoding video window…")
            await _run_ffmpeg(
                build_video_cmd(session.dest_path, v_idx, job.window_start, dur, str(video_path)),
                job, 40,
            )

        audio_keys = [t.key for t in session.audio_tracks] if prefetch_all else ([job.audio_key] if job.audio_key else [])
        for ak in audio_keys:
            ap = wdir / f"audio_{ak.replace(':', '_')}.m4a"
            if ap.exists():
                continue
            tr = _track_by_key(session, ak)
            if not tr:
                continue
            tracks = probe_file_tracks(_file_for(session, tr.side))
            s_idx = mkv_track_stream_index(tracks, tr.track_id)
            job.log.append(f"Extracting audio {ak}…")
            await _run_ffmpeg(build_audio_cmd(
                _file_for(session, tr.side), s_idx,
                job.window_start, dur, _offset_for(session, tr), str(ap),
            ), job, 20 // max(1, len(audio_keys)))

        sub_keys = [t.key for t in session.sub_tracks if not t.bitmap] if prefetch_all else []
        if job.sub_key:
            tr = _track_by_key(session, job.sub_key)
            if tr and not tr.bitmap and job.sub_key not in sub_keys:
                sub_keys.append(job.sub_key)

        for sk in sub_keys:
            vp = wdir / f"sub_{sk.replace(':', '_')}.vtt"
            if vp.exists():
                continue
            tr = _track_by_key(session, sk)
            if not tr:
                continue
            tracks = probe_file_tracks(_file_for(session, tr.side))
            s_idx = mkv_track_stream_index(tracks, tr.track_id)
            job.log.append(f"Extracting subtitles {sk}…")
            await _run_ffmpeg(build_vtt_cmd(
                _file_for(session, tr.side), s_idx,
                job.window_start, dur, _offset_for(session, tr), str(vp),
            ), job, 10 // max(1, len(sub_keys)))
            clean_vtt_file(vp)
            normalize_vtt_window(vp, job.window_start)

        await _finalize_playback(session, job, wdir, video_path)
        job.status = "done"
        job.progress_pct = 100
    except Exception as e:
        job.status = "error"
        job.error = str(e)


async def _run_switch_job(session: PreviewSession, job: PreviewJob) -> None:
    job.status = "running"
    dur = job.window_end - job.window_start
    wdir = window_dir(session, job.window_start, job.window_end)
    video_path = wdir / "video.mp4"

    try:
        if not video_path.exists():
            raise RuntimeError("Video window not cached — load preview first")

        sub_tr = _track_by_key(session, job.sub_key)
        if sub_tr and sub_tr.bitmap:
            job.sub_bitmap = True
            baked = wdir / f"baked_{job.sub_key.replace(':', '_')}.mp4"
            if not baked.exists():
                tracks = probe_file_tracks(_file_for(session, sub_tr.side))
                s_idx = mkv_track_stream_index(tracks, sub_tr.track_id)
                sub_file = _file_for(session, sub_tr.side)
                audio_path = None
                audio_stream = None
                audio_offset = 0
                if job.audio_key:
                    atr = _track_by_key(session, job.audio_key)
                    if atr:
                        audio_path = _file_for(session, atr.side)
                        a_tracks = probe_file_tracks(audio_path)
                        audio_stream = mkv_track_stream_index(a_tracks, atr.track_id)
                        audio_offset = _offset_for(session, atr)
                job.log.append("Burning bitmap subtitles…")
                await _run_ffmpeg(build_pgs_window_cmd(
                    session.dest_path, sub_file, s_idx,
                    audio_path, audio_stream, audio_offset,
                    job.window_start, dur, str(baked),
                ), job, 80)
            job.playback_path = str(baked)
            job.playback_url = f"/api/preview/files/{session.id}/{wdir.name}/{baked.name}"
            job.vtt_tracks = []
        else:
            ak = job.audio_key
            if ak:
                ap = wdir / f"audio_{ak.replace(':', '_')}.m4a"
                if not ap.exists():
                    tr = _track_by_key(session, ak)
                    if tr:
                        tracks = probe_file_tracks(_file_for(session, tr.side))
                        s_idx = mkv_track_stream_index(tracks, tr.track_id)
                        job.log.append(f"Extracting audio {ak}…")
                        await _run_ffmpeg(build_audio_cmd(
                            _file_for(session, tr.side), s_idx,
                            job.window_start, dur, _offset_for(session, tr), str(ap),
                        ), job, 50)
            if job.sub_key:
                tr = _track_by_key(session, job.sub_key)
                if tr and not tr.bitmap:
                    vp = wdir / f"sub_{job.sub_key.replace(':', '_')}.vtt"
                    if not vp.exists():
                        tracks = probe_file_tracks(_file_for(session, tr.side))
                        s_idx = mkv_track_stream_index(tracks, tr.track_id)
                        job.log.append(f"Extracting subtitles {job.sub_key}…")
                        await _run_ffmpeg(build_vtt_cmd(
                            _file_for(session, tr.side), s_idx,
                            job.window_start, dur, _offset_for(session, tr), str(vp),
                        ), job, 30)
                        clean_vtt_file(vp)
                        normalize_vtt_window(vp, job.window_start)
            await _finalize_playback(session, job, wdir, video_path)

        job.status = "done"
        job.progress_pct = 100
    except Exception as e:
        job.status = "error"
        job.error = str(e)


async def _finalize_playback(
    session: PreviewSession, job: PreviewJob, wdir: Path, video_path: Path,
) -> None:
    audio_path = None
    if job.audio_key:
        ap = wdir / f"audio_{job.audio_key.replace(':', '_')}.m4a"
        if ap.exists():
            audio_path = str(ap)

    playback = wdir / f"play_{job.audio_key or 'novid'}.mp4"
    if not playback.exists():
        await _run_ffmpeg(build_mux_cmd(str(video_path), audio_path, str(playback)), job, 10)

    job.playback_path = str(playback)
    job.playback_url = f"/api/preview/files/{session.id}/{wdir.name}/{playback.name}"

    sub_tr = _track_by_key(session, job.sub_key)
    job.sub_bitmap = bool(sub_tr and sub_tr.bitmap)
    job.vtt_tracks = []
    if not job.sub_bitmap:
        for t in session.sub_tracks:
            if t.bitmap:
                continue
            vp = wdir / f"sub_{t.key.replace(':', '_')}.vtt"
            if vp.exists():
                job.vtt_tracks.append({
                    "key": t.key,
                    "label": t.label,
                    "url": f"/api/preview/files/{session.id}/{wdir.name}/{vp.name}",
                })


def job_public(job: PreviewJob) -> dict:
    return {
        "status": job.status,
        "progress_pct": job.progress_pct,
        "log_tail": job.log[-30:],
        "error": job.error,
        "window_start": job.window_start,
        "window_end": job.window_end,
        "playback_url": job.playback_url,
        "vtt_tracks": job.vtt_tracks,
        "audio_key": job.audio_key,
        "sub_key": job.sub_key,
        "sub_bitmap": job.sub_bitmap,
    }


def resolve_cache_file(session_id: str, window_name: str, filename: str) -> Path | None:
    session = SESSIONS.get(session_id)
    if session is None:
        return None
    path = (session.cache_root / window_name / filename).resolve()
    try:
        path.relative_to(session.cache_root.resolve())
    except ValueError:
        return None
    if path.exists():
        return path
    return None


def list_cached_windows(session: PreviewSession) -> list[dict]:
    out: list[dict] = []
    root = session.cache_root
    if not root.exists():
        return out
    for d in root.iterdir():
        if not d.is_dir() or not d.name.startswith("w_"):
            continue
        parts = d.name.split("_")
        if len(parts) < 3:
            continue
        try:
            start = int(parts[1]) / 1000
            end = int(parts[2]) / 1000
        except ValueError:
            continue
        if (d / "video.mp4").exists():
            out.append({"start": start, "end": end})
    return sorted(out, key=lambda w: w["start"])


def cleanup_session(session_id: str) -> None:
    session = SESSIONS.pop(session_id, None)
    if session and session.cache_root.exists():
        shutil.rmtree(session.cache_root, ignore_errors=True)
