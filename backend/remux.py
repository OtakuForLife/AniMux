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


def _track_opts(
    track_ids: list[int],
    track_map: dict[int, str],
    *,
    no_video: bool = False,
) -> list[str]:
    opts: list[str] = []
    if no_video:
        opts.append("--no-video")
    else:
        video_ids = [str(i) for i in track_ids if track_map.get(i) == "video"]
        if video_ids:
            opts.extend(["--video-tracks", ",".join(video_ids)])
        else:
            opts.append("--no-video")

    audio_ids = [str(i) for i in track_ids if track_map.get(i) == "audio"]
    if audio_ids:
        opts.extend(["--audio-tracks", ",".join(audio_ids)])
    else:
        opts.append("--no-audio")

    sub_ids = [str(i) for i in track_ids if track_map.get(i) == "subtitles"]
    if sub_ids:
        opts.extend(["--subtitle-tracks", ",".join(sub_ids)])
    else:
        opts.append("--no-subtitles")
    return opts


def build_mkvmerge_cmd(
    dest_path: str,
    source_path: str,
    source_track_ids: list[int],
    source_track_map: dict[int, str],
    dest_track_ids: list[int],
    dest_track_map: dict[int, str],
    chapters: bool,
    attachments: bool,
    tags: bool,
    output_path: str,
) -> list[str]:
    cmd = ["mkvmerge", "-o", output_path]
    cmd.extend(_track_opts(dest_track_ids, dest_track_map))
    cmd.append(dest_path)
    cmd.extend(_track_opts(source_track_ids, source_track_map, no_video=True))
    if not chapters:
        cmd.append("--no-chapters")
    if not attachments:
        cmd.append("--no-attachments")
    if not tags:
        cmd.extend(["--no-global-tags", "--no-track-tags"])
    cmd.append(source_path)
    return cmd


async def start_job(
    source_path: str,
    dest_path: str,
    source_track_ids: list[int],
    source_track_map: dict[int, str],
    dest_track_ids: list[int],
    dest_track_map: dict[int, str],
    chapters: bool,
    attachments: bool,
    tags: bool,
) -> str:
    job_id = str(uuid.uuid4())
    job = Job(id=job_id)
    JOBS[job_id] = job
    asyncio.create_task(_run_job(
        job_id, source_path, dest_path,
        source_track_ids, source_track_map,
        dest_track_ids, dest_track_map,
        chapters, attachments, tags,
    ))
    return job_id


async def _run_job(
    job_id: str,
    source_path: str,
    dest_path: str,
    source_track_ids: list[int],
    source_track_map: dict[int, str],
    dest_track_ids: list[int],
    dest_track_map: dict[int, str],
    chapters: bool,
    attachments: bool,
    tags: bool,
) -> None:
    job = JOBS[job_id]
    job.status = "running"

    tmp_path = dest_path + ".animux.tmp"
    cmd = build_mkvmerge_cmd(
        dest_path, source_path,
        source_track_ids, source_track_map,
        dest_track_ids, dest_track_map,
        chapters, attachments, tags, tmp_path,
    )

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

    if proc.returncode in (0, 1):  # mkvmerge returns 1 for warnings
        os.replace(tmp_path, dest_path)
        job.status = "done"
        job.progress_pct = 100
    else:
        job.status = "error"
        job.error = stderr_bytes.decode(errors="replace")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


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
