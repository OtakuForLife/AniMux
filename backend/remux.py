"""mkvmerge command builder + async subprocess runner."""

import asyncio
import os
import uuid
from dataclasses import dataclass, field


@dataclass
class Job:
    id: str
    status: str = "pending"  # pending | running | done | error
    progress_pct: int = 0
    log: list[str] = field(default_factory=list)
    error: str | None = None


JOBS: dict[str, Job] = {}


def build_mkvmerge_cmd(
    dest_path: str,
    source_path: str,
    track_ids: list[int],
    track_map: dict[int, str],  # {id: "audio"|"subtitles"|"video"|...}
    chapters: bool,
    attachments: bool,
    tags: bool,
    output_path: str,
) -> list[str]:
    audio_ids = [str(i) for i in track_ids if track_map.get(i) == "audio"]
    sub_ids = [str(i) for i in track_ids if track_map.get(i) == "subtitles"]

    cmd = [
        "mkvmerge", "-o", output_path,
        dest_path,
        "--video-tracks", "",
        "--audio-tracks", ",".join(audio_ids) if audio_ids else "",
        "--subtitle-tracks", ",".join(sub_ids) if sub_ids else "",
        *(["--no-chapters"] if not chapters else []),
        *(["--no-attachments"] if not attachments else []),
        *(["--no-tags"] if not tags else []),
        source_path,
    ]
    return cmd


async def start_job(
    source_path: str,
    dest_path: str,
    track_ids: list[int],
    track_map: dict[int, str],
    chapters: bool,
    attachments: bool,
    tags: bool,
) -> str:
    job_id = str(uuid.uuid4())
    job = Job(id=job_id)
    JOBS[job_id] = job
    asyncio.create_task(_run_job(job_id, source_path, dest_path, track_ids, track_map, chapters, attachments, tags))
    return job_id


async def _run_job(
    job_id: str,
    source_path: str,
    dest_path: str,
    track_ids: list[int],
    track_map: dict[int, str],
    chapters: bool,
    attachments: bool,
    tags: bool,
) -> None:
    job = JOBS[job_id]
    job.status = "running"

    tmp_path = dest_path + ".animux.tmp"
    cmd = build_mkvmerge_cmd(dest_path, source_path, track_ids, track_map, chapters, attachments, tags, tmp_path)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        job.log.append(line)
        if line.startswith("Progress:"):
            try:
                job.progress_pct = int(line.split(":")[1].strip().rstrip("%"))
            except (IndexError, ValueError):
                pass

    _, stderr_bytes = await proc.communicate()

    if proc.returncode == 0:
        os.replace(tmp_path, dest_path)
        job.status = "done"
        job.progress_pct = 100
    else:
        job.status = "error"
        job.error = stderr_bytes.decode(errors="replace")


if __name__ == "__main__":
    import json, subprocess, sys

    if len(sys.argv) < 2:
        print("Usage: python remux.py <file.mkv>")
        sys.exit(1)

    result = subprocess.run(["mkvmerge", "-J", sys.argv[1]], capture_output=True, text=True)
    data = json.loads(result.stdout)
    tracks = [
        {"id": t["id"], "type": t["type"], "codec": t["codec"], "language": t.get("properties", {}).get("language", ""), "name": t.get("properties", {}).get("track_name", "")}
        for t in data.get("tracks", [])
    ]
    print(json.dumps(tracks, indent=2))
