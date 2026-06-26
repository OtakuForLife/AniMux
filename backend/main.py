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


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not shutil.which("mkvmerge"):
        raise RuntimeError("mkvmerge not found on PATH — install mkvtoolnix and try again")
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


class RemuxRequest(BaseModel):
    source_path: str
    dest_path: str
    track_ids: list[int]
    dest_track_ids: list[int]
    chapters: bool = True
    attachments: bool = True
    tags: bool = True


def _probe_track_map(path: Path) -> dict[int, str]:
    result = subprocess.run(
        ["mkvmerge", "-J", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode not in (0, 1):
        raise HTTPException(status_code=500, detail=f"Failed to probe file: {path.name}")
    data = json.loads(result.stdout)
    return {t["id"]: t["type"] for t in data.get("tracks", [])}


@app.post("/api/remux")
async def start_remux(req: RemuxRequest):
    src_root = Path(os.environ.get("SOURCE_DIR", SOURCE_DIR))
    dst_root = Path(os.environ.get("DEST_DIR", DEST_DIR))

    src_full = (src_root / req.source_path).resolve()
    dst_full = (dst_root / req.dest_path).resolve()

    try:
        src_full.relative_to(src_root.resolve())
        dst_full.relative_to(dst_root.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal not allowed")

    if not src_full.exists():
        raise HTTPException(status_code=404, detail="Source file not found")
    if not dst_full.exists():
        raise HTTPException(status_code=404, detail="Destination file not found")
    if not req.dest_track_ids:
        raise HTTPException(status_code=400, detail="At least one destination track must be selected")

    source_track_map = _probe_track_map(src_full)
    dest_track_map = _probe_track_map(dst_full)

    job_id = await remux.start_job(
        source_path=str(src_full),
        dest_path=str(dst_full),
        source_track_ids=req.track_ids,
        source_track_map=source_track_map,
        dest_track_ids=req.dest_track_ids,
        dest_track_map=dest_track_map,
        chapters=req.chapters,
        attachments=req.attachments,
        tags=req.tags,
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
