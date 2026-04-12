import asyncio
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="Media Downloader")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = Path("/downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# In-memory job store
jobs: dict[str, dict] = {}


class DownloadRequest(BaseModel):
    url: str
    mode: str = "video"          # video | audio | playlist-video | playlist-audio
    quality: str = "best"        # best | 1080 | 720 | 480 | 360 | 240 | worst
    audio_format: str = "mp3"    # mp3 | m4a | opus | flac | wav
    video_format: str = "mp4"    # mp4 | mkv | webm
    playlist_start: int = 1
    playlist_end: Optional[int] = None
    embed_thumbnail: bool = True
    embed_metadata: bool = True
    subtitle_lang: str = ""      # e.g. "en" or empty


def build_ytdlp_args(req: DownloadRequest, job_id: str) -> list[str]:
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    args = [
        "yt-dlp",
        "--no-warnings",
        "--newline",
        "--progress",
        "--no-playlist" if "playlist" not in req.mode else "--yes-playlist",
        "-o", str(job_dir / "%(playlist_index)s%(playlist_index&. |)s%(title)s.%(ext)s"),
    ]

    # Playlist range
    if "playlist" in req.mode:
        args += ["--playlist-start", str(req.playlist_start)]
        if req.playlist_end:
            args += ["--playlist-end", str(req.playlist_end)]

    is_audio = "audio" in req.mode

    if is_audio:
        args += [
            "-x",
            "--audio-format", req.audio_format,
            "--audio-quality", "0",
        ]
        if req.embed_thumbnail:
            args += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]
        if req.embed_metadata:
            args += ["--add-metadata"]
    else:
        # Video quality selection
        if req.quality == "best":
            fmt = f"bestvideo[ext={req.video_format}]+bestaudio/best[ext={req.video_format}]/bestvideo+bestaudio/best"
        elif req.quality == "worst":
            fmt = "worstvideo+worstaudio/worst"
        else:
            h = req.quality  # e.g. "1080"
            fmt = (
                f"bestvideo[height<={h}][ext={req.video_format}]+bestaudio/"
                f"bestvideo[height<={h}]+bestaudio/"
                f"best[height<={h}]/best"
            )

        args += ["-f", fmt]

        if req.video_format in ("mp4", "mkv"):
            args += ["--merge-output-format", req.video_format]

        if req.embed_thumbnail:
            args += ["--embed-thumbnail"]
        if req.embed_metadata:
            args += ["--embed-metadata"]

    # Subtitles
    if req.subtitle_lang:
        args += [
            "--write-subs",
            "--write-auto-subs",
            "--sub-lang", req.subtitle_lang,
            "--embed-subs",
        ]

    args.append(req.url)
    return args


async def run_download(job_id: str, req: DownloadRequest):
    jobs[job_id]["status"] = "running"
    args = build_ytdlp_args(req, job_id)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        log_lines = []
        percent = 0.0
        speed = ""
        eta = ""
        filename = ""

        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            log_lines.append(line)

            # Parse progress line: [download]  45.3% of   23.45MiB at   2.34MiB/s ETA 00:08
            m = re.search(r"\[download\]\s+([\d.]+)%", line)
            if m:
                percent = float(m.group(1))

            ms = re.search(r"at\s+([\d.]+\s*\w+/s)", line)
            if ms:
                speed = ms.group(1)

            me = re.search(r"ETA\s+(\S+)", line)
            if me:
                eta = me.group(1)

            mf = re.search(r"\[download\] Destination: (.+)", line)
            if mf:
                filename = Path(mf.group(1)).name

            jobs[job_id].update({
                "percent": percent,
                "speed": speed,
                "eta": eta,
                "current_file": filename,
                "log": log_lines[-80:],  # keep last 80 lines
            })

        await proc.wait()

        if proc.returncode == 0:
            # Collect output files
            job_dir = DOWNLOAD_DIR / job_id
            files = [f.name for f in job_dir.iterdir() if f.is_file()]
            jobs[job_id].update({
                "status": "done",
                "percent": 100.0,
                "files": files,
                "eta": "",
                "speed": "",
            })
        else:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = "\n".join(log_lines[-20:])

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


@app.post("/api/download")
async def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "id": job_id,
        "status": "queued",
        "percent": 0.0,
        "speed": "",
        "eta": "",
        "current_file": "",
        "files": [],
        "log": [],
        "error": "",
        "url": req.url,
        "mode": req.mode,
        "created": time.time(),
    }
    background_tasks.add_task(run_download, job_id, req)
    return {"job_id": job_id}


@app.get("/api/job/{job_id}")
async def get_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    return jobs[job_id]


@app.get("/api/jobs")
async def list_jobs():
    return list(jobs.values())


@app.get("/api/download/{job_id}/{filename}")
async def download_file(job_id: str, filename: str):
    path = DOWNLOAD_DIR / job_id / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(path, filename=filename, media_type="application/octet-stream")


@app.delete("/api/job/{job_id}")
async def delete_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job_dir = DOWNLOAD_DIR / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir)
    del jobs[job_id]
    return {"ok": True}


@app.post("/api/info")
async def get_info(body: dict):
    url = body.get("url", "")
    if not url:
        raise HTTPException(400, "No URL provided")

    proc = await asyncio.create_subprocess_exec(
        "yt-dlp", "--dump-json", "--no-playlist", url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise HTTPException(400, stderr.decode("utf-8", errors="replace")[:500])

    try:
        info = json.loads(stdout)
        return {
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": [
                {
                    "format_id": f.get("format_id"),
                    "ext": f.get("ext"),
                    "height": f.get("height"),
                    "filesize": f.get("filesize"),
                    "vcodec": f.get("vcodec"),
                    "acodec": f.get("acodec"),
                }
                for f in info.get("formats", [])
                if f.get("height") or f.get("acodec") != "none"
            ],
        }
    except Exception:
        raise HTTPException(500, "Failed to parse media info")


# Cleanup jobs older than 2 hours on startup
@app.on_event("startup")
async def cleanup_old():
    if DOWNLOAD_DIR.exists():
        for d in DOWNLOAD_DIR.iterdir():
            if d.is_dir():
                try:
                    age = time.time() - d.stat().st_mtime
                    if age > 7200:
                        shutil.rmtree(d)
                except Exception:
                    pass
