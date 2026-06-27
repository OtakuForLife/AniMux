"""FastAPI backend for AniMux."""

import asyncio
import json
import os
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import remux
import preview


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not shutil.which("mkvmerge"):
        raise RuntimeError("mkvmerge not found on PATH — install mkvtoolnix and try again")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found on PATH — install ffmpeg and try again")
    yield


app = FastAPI(title="AniMux", lifespan=lifespan)

SOURCE_DIR = os.environ.get("SOURCE_DIR", "/source")
DEST_DIR = os.environ.get("DEST_DIR", "/destination")

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


@app.get("/", response_class=HTMLResponse)
def serve_index():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>AniMux — frontend not built yet</h1>")


def _resolve_dir(dir_param: str) -> str:
    if dir_param == "source":
        return os.environ.get("SOURCE_DIR", SOURCE_DIR)
    if dir_param == "destination":
        return os.environ.get("DEST_DIR", DEST_DIR)
    raise HTTPException(status_code=400, detail="dir must be 'source' or 'destination'")


@app.get("/api/files")
def list_files(dir: str = Query(...)):
    root = Path(_resolve_dir(dir))
    if not root.exists():
        raise HTTPException(status_code=404, detail=f"Directory not found: {root}")

    files = []
    for p in root.rglob("*.mkv"):
        files.append({
            "name": p.name,
            "path": p.relative_to(root).as_posix(),
            "size": p.stat().st_size,
        })
    return files


@app.get("/api/probe")
def probe_file(dir: str = Query(...), path: str = Query(...)):
    root = Path(_resolve_dir(dir))
    full_path = (root / path).resolve()

    # Prevent path traversal outside the root
    try:
        full_path.relative_to(root.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal not allowed")

    if not full_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    result = subprocess.run(
        ["mkvmerge", "-J", str(full_path)],
        capture_output=True, text=True,
    )
    if result.returncode not in (0, 1):  # mkvmerge returns 1 for warnings
        raise HTTPException(status_code=500, detail=result.stderr)

    data = json.loads(result.stdout)
    tracks = [
        {
            "id": t["id"],
            "type": t["type"],
            "codec": t["codec"],
            "language": t.get("properties", {}).get("language", ""),
            "name": t.get("properties", {}).get("track_name", ""),
        }
        for t in data.get("tracks", [])
    ]
    attachments = [
        {
            "id": a["id"],
            "name": a["file_name"],
            "mime_type": a["content_type"],
        }
        for a in data.get("attachments", [])
    ]
    return {"tracks": tracks, "attachments": attachments}


class TransferRequest(BaseModel):
    source_path: str
    dest_path: str
    track_ids: list[int]
    dest_track_ids: list[int]
    source_track_offsets: dict[str, int] = {}
    dest_track_offsets: dict[str, int] = {}
    chapters: bool = True
    attachments: bool = True
    tags: bool = True
    preview_duration_sec: int = 180


def _parse_offsets(raw: dict[str, int]) -> dict[int, int]:
    return {int(k): v for k, v in raw.items() if v}


def _resolve_transfer_paths(source_path: str, dest_path: str) -> tuple[Path, Path, Path, Path]:
    src_root = Path(os.environ.get("SOURCE_DIR", SOURCE_DIR))
    dst_root = Path(os.environ.get("DEST_DIR", DEST_DIR))
    src_full = (src_root / source_path).resolve()
    dst_full = (dst_root / dest_path).resolve()
    try:
        src_full.relative_to(src_root.resolve())
        dst_full.relative_to(dst_root.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal not allowed")
    if not src_full.exists():
        raise HTTPException(status_code=404, detail="Source file not found")
    if not dst_full.exists():
        raise HTTPException(status_code=404, detail="Destination file not found")
    return src_root, dst_root, src_full, dst_full


def _prepare_dest_tracks(req: TransferRequest, dest_track_map: dict[int, str]) -> list[int]:
    dest_video_ids = [i for i, typ in dest_track_map.items() if typ == "video"]
    dest_track_ids = list(set(req.dest_track_ids) | set(dest_video_ids))
    if not dest_track_ids:
        raise HTTPException(status_code=400, detail="At least one destination track must be selected")
    return dest_track_ids


class PreviewSessionRequest(TransferRequest):
    pass


class PreviewWindowRequest(BaseModel):
    session_id: str
    t_sec: float = 0
    window_sec: int = 0
    audio_key: str | None = None
    sub_key: str | None = None
    source_track_offsets: dict[str, int] = {}
    dest_track_offsets: dict[str, int] = {}
    force: bool = False


class PreviewSwitchRequest(BaseModel):
    session_id: str
    window_start: float
    window_end: float
    audio_key: str | None = None
    sub_key: str | None = None
    source_track_offsets: dict[str, int] = {}
    dest_track_offsets: dict[str, int] = {}
    force: bool = False


class RemuxRequest(TransferRequest):
    pass


def _probe_tracks(path: Path) -> list[dict]:
    result = subprocess.run(
        ["mkvmerge", "-J", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode not in (0, 1):
        raise HTTPException(status_code=500, detail=f"Failed to probe file: {path.name}")
    return json.loads(result.stdout).get("tracks", [])


def _probe_track_map(path: Path) -> dict[int, str]:
    return {t["id"]: t["type"] for t in _probe_tracks(path)}


def _default_preview_audio(session_data: preview.PreviewSession) -> str | None:
    return session_data.audio_tracks[0].key if session_data.audio_tracks else None


@app.post("/api/preview/session")
async def create_preview_session(req: PreviewSessionRequest):
    _, _, src_full, dst_full = _resolve_transfer_paths(req.source_path, req.dest_path)
    source_track_map = _probe_track_map(src_full)
    dest_track_map = _probe_track_map(dst_full)
    dest_track_ids = _prepare_dest_tracks(req, dest_track_map)

    session = preview.create_session(
        source_path=str(src_full),
        dest_path=str(dst_full),
        source_track_ids=req.track_ids,
        dest_track_ids=dest_track_ids,
        source_track_offsets=_parse_offsets(req.source_track_offsets),
        dest_track_offsets=_parse_offsets(req.dest_track_offsets),
    )
    return preview.session_public(session)


def _apply_preview_offsets(session: preview.PreviewSession, req) -> None:
    preview.update_session_offsets(
        session,
        _parse_offsets(req.source_track_offsets),
        _parse_offsets(req.dest_track_offsets),
    )


@app.post("/api/preview/window")
async def start_preview_window(req: PreviewWindowRequest):
    session = preview.SESSIONS.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Preview session not found")

    _apply_preview_offsets(session, req)

    window_sec = req.window_sec or int(os.environ.get("PREVIEW_WINDOW", "30"))
    window_sec = max(5, min(window_sec, 120))
    t_sec = max(0.0, min(req.t_sec, session.duration_sec or req.t_sec))
    audio_key = req.audio_key or _default_preview_audio(session)
    start, end = preview.window_bounds(t_sec, window_sec, session.duration_sec)

    if req.force:
        preview.invalidate_window_playback(preview.window_dir(session, start, end))

    if not req.force:
        cached = preview.try_cached_playback(session, start, end, audio_key, req.sub_key)
        if cached:
            return {"cached": True, **cached}

    job_id = await preview.start_window_job(
        req.session_id, t_sec, window_sec, audio_key, req.sub_key, force=req.force,
    )
    return {"job_id": job_id, "cached": False}


@app.post("/api/preview/switch")
async def switch_preview_tracks(req: PreviewSwitchRequest):
    session = preview.SESSIONS.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Preview session not found")

    _apply_preview_offsets(session, req)

    if req.force:
        preview.invalidate_window_playback(
            preview.window_dir(session, req.window_start, req.window_end),
        )

    if not req.force:
        cached = preview.try_cached_playback(
            session, req.window_start, req.window_end, req.audio_key, req.sub_key,
        )
        if cached and preview.text_sub_vtt_ready(
            session, req.window_start, req.window_end, req.sub_key,
        ):
            return {"cached": True, **cached}

    job_id = await preview.start_switch_job(
        req.session_id, req.window_start, req.window_end,
        req.audio_key, req.sub_key,
    )
    return {"job_id": job_id, "cached": False}


@app.get("/api/preview/session/{session_id}/cache")
def get_preview_cache(session_id: str):
    session = preview.SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Preview session not found")
    return {"windows": preview.list_cached_windows(session)}


@app.get("/api/preview/jobs/{job_id}")
def get_preview_job(job_id: str):
    job = preview.JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Preview job not found")
    return preview.job_public(job)


@app.get("/api/preview/files/{session_id}/{window_name}/{filename}")
def get_preview_cache_file(session_id: str, window_name: str, filename: str):
    path = preview.resolve_cache_file(session_id, window_name, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="Preview file not found")
    media = "video/mp4"
    if filename.endswith(".vtt"):
        media = "text/vtt; charset=utf-8"
    elif filename.endswith(".m4a"):
        media = "audio/mp4"
    return FileResponse(
        path, media_type=media,
        headers={"Access-Control-Allow-Origin": "*"},
    )


@app.post("/api/remux")
async def start_remux(req: RemuxRequest):
    _, _, src_full, dst_full = _resolve_transfer_paths(req.source_path, req.dest_path)
    source_track_map = _probe_track_map(src_full)
    dest_track_map = _probe_track_map(dst_full)
    dest_track_ids = _prepare_dest_tracks(req, dest_track_map)

    job_id = await remux.start_job(
        source_path=str(src_full),
        dest_path=str(dst_full),
        source_track_ids=req.track_ids,
        source_track_map=source_track_map,
        dest_track_ids=dest_track_ids,
        dest_track_map=dest_track_map,
        chapters=req.chapters,
        attachments=req.attachments,
        tags=req.tags,
        source_track_offsets=_parse_offsets(req.source_track_offsets),
        dest_track_offsets=_parse_offsets(req.dest_track_offsets),
    )
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = remux.JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "status": job.status,
        "progress_pct": job.progress_pct,
        "log_tail": job.log[-50:],
        "error": job.error,
    }


@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str):
    job = remux.JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_generator():
        sent = 0
        while True:
            while sent < len(job.log):
                line = job.log[sent]
                sent += 1
                yield f"data: {line}\n\n"
            if job.status in ("done", "error"):
                yield f"data: [STATUS:{job.status}]\n\n"
                break
            await asyncio.sleep(0.2)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
