"""
main.py — FastAPI Backend for Elite Video Downloader
=====================================================
Endpoints:
  POST /api/fetch           → Extract video metadata via yt-dlp
  GET  /api/download        → Stream a video/audio file as a download
  GET  /api/merge-download  → Merge video+audio via FFmpeg then stream
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import yt_dlp
import httpx
import re
import asyncio
import traceback
import sys
import os
import shutil
import uuid
import tempfile


# Fix Windows console encoding — cp1252 can't handle emoji/unicode
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# ───────────────────────────────────────────────
# FFmpeg Detection
# ───────────────────────────────────────────────
FFMPEG_PATH = None

def _find_ffmpeg() -> str | None:
    """Locate the ffmpeg binary. Checks system PATH first, then imageio-ffmpeg."""
    # 1. Check system PATH
    ffmpeg_on_path = shutil.which("ffmpeg")
    if ffmpeg_on_path:
        return ffmpeg_on_path
    # 2. Check imageio-ffmpeg bundled binary
    try:
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and os.path.isfile(path):
            return path
    except ImportError:
        pass
    return None

FFMPEG_PATH = _find_ffmpeg()

# Temp directory for merge operations
MERGE_TEMP_DIR = os.path.join(tempfile.gettempdir(), "elite_downloader_merge")
os.makedirs(MERGE_TEMP_DIR, exist_ok=True)

# ───────────────────────────────────────────────
# App Initialization
# ───────────────────────────────────────────────
app = FastAPI(
    title="Elite Video Downloader API",
    description="Backend API for fetching and downloading videos from YouTube, Instagram, TikTok & Facebook.",
    version="1.0.0",
)

# CORS — allow the frontend (any origin in dev) to call us
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # Tighten this in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def startup():
    global FFMPEG_PATH
    FFMPEG_PATH = _find_ffmpeg()
    if FFMPEG_PATH:
        print(f"[STARTUP] FFmpeg found: {FFMPEG_PATH}")
    else:
        print("[STARTUP] WARNING: FFmpeg not found! Video+audio merging will not work.")
        print("[STARTUP] Install with: pip install imageio-ffmpeg")


# ───────────────────────────────────────────────
# Pydantic Request / Response Models
# ───────────────────────────────────────────────
class FetchRequest(BaseModel):
    url: str

class MergeDownloadRequest(BaseModel):
    video_url: str
    audio_url: str
    title: str = "video"
    ext: str = "mp4"


# ───────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────

def _human_size(nbytes: int | float | None) -> str:
    """Convert bytes to a human-readable string like '120 MB'."""
    if not nbytes:
        return "Unknown"
    for unit in ("B", "KB", "MB", "GB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def _classify_format(f: dict) -> str:
    """Decide if a yt-dlp format dict is 'video+audio', 'video-only', or 'audio-only'."""
    has_video = f.get("vcodec", "none") != "none"
    has_audio = f.get("acodec", "none") != "none"
    if has_video and has_audio:
        return "video+audio"
    elif has_video:
        return "video-only"
    elif has_audio:
        return "audio-only"
    return "other"


def _resolution_label(f: dict) -> str:
    """Build a friendly resolution label like '1080p' or '320kbps'."""
    height = f.get("height")
    if height:
        return f"{height}p"
    abr = f.get("abr")
    if abr:
        return f"{int(abr)}kbps"
    return f.get("format_note", "Unknown")


def _detect_platform(url: str) -> str:
    """Detect the platform from the URL domain."""
    url_lower = url.lower()
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "youtube"
    if "instagram.com" in url_lower or "instagr.am" in url_lower:
        return "instagram"
    if "facebook.com" in url_lower or "fb.watch" in url_lower or "fb.com" in url_lower:
        return "facebook"
    if "tiktok.com" in url_lower:
        return "tiktok"
    return "other"


def extract_video_info(url: str) -> dict:
    """
    Use yt-dlp to extract metadata + available formats for a URL.
    Returns a structured dict ready for the frontend.

    For Instagram/Facebook/TikTok: if no pre-muxed video+audio formats exist,
    synthesizes merged entries by pairing best audio with each video stream.
    """
    # Resolve cookies.txt — check the script directory first, then the project root
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    cookies_path = next(
        (
            p for p in (
                os.path.join(script_dir, "cookies.txt"),
                os.path.join(project_root, "cookies.txt"),
            )
            if os.path.isfile(p)
        ),
        None,
    )

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "ignoreerrors": False,          # Don't silently swallow extraction failures
        "source_address": "0.0.0.0",    # Force IPv4 — avoids IPv6 datacenter blocks
        "extractor_args": {
            "youtube": {
                # Modern client fallback chain: tv → web → mweb → android → ios
                "player_client": ["tv", "web", "mweb", "android", "ios"],
            }
        },
    }

    # Strictly pass cookies.txt when present (helps bypass bot/age-gate on Render)
    if cookies_path:
        ydl_opts["cookiefile"] = cookies_path
        print(f"[FETCH] Using cookies from: {cookies_path}")
    else:
        print("[FETCH] No cookies.txt found — proceeding without cookies")

    # Point yt-dlp at our FFmpeg so it can probe formats properly
    if FFMPEG_PATH:
        ydl_opts["ffmpeg_location"] = os.path.dirname(FFMPEG_PATH)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    platform = _detect_platform(url)

    # ── Build clean format lists ──
    video_audio = []
    video_only  = []
    audio_only  = []

    seen_resolutions = {"video+audio": set(), "video-only": set(), "audio-only": set()}

    for f in info.get("formats", []):
        kind = _classify_format(f)
        if kind == "other":
            continue

        label = _resolution_label(f)

        # De-duplicate: keep the best (last) format per resolution per category
        if label in seen_resolutions[kind]:
            target_list = (
                video_audio if kind == "video+audio" else
                video_only  if kind == "video-only"  else
                audio_only
            )
            target_list[:] = [item for item in target_list if item["resolution"] != label]

        seen_resolutions[kind].add(label)

        entry = {
            "format_id":  f.get("format_id", ""),
            "resolution": label,
            "ext":        f.get("ext", "mp4"),
            "filesize":   _human_size(f.get("filesize") or f.get("filesize_approx")),
            "url":        f.get("url", ""),
            "codec":      f.get("vcodec", f.get("acodec", "")),
        }

        if kind == "video+audio":
            video_audio.append(entry)
        elif kind == "video-only":
            video_only.append(entry)
        else:
            audio_only.append(entry)

    # Sort: highest quality first
    def _sort_key(item):
        res = item["resolution"]
        num = re.search(r"\d+", res)
        return int(num.group()) if num else 0

    video_audio.sort(key=_sort_key, reverse=True)
    video_only.sort(key=_sort_key, reverse=True)
    audio_only.sort(key=_sort_key, reverse=True)

    # ── Synthesize merged video+audio for platforms with separate streams ──
    # If the "Video + Audio" tab would be empty but we have video-only + audio-only,
    # create synthetic merged entries so the user gets a one-click combined download.
    if not video_audio and video_only and audio_only:
        best_audio = audio_only[0]  # highest bitrate audio
        print(f"[FETCH] Platform '{platform}': No muxed formats. Synthesizing {len(video_only)} merged entries.")

        for vf in video_only:
            # Estimate combined size
            v_size = vf.get("filesize", "Unknown")
            merged_entry = {
                "format_id":  f"{vf['format_id']}+{best_audio['format_id']}",
                "resolution": vf["resolution"],
                "ext":        "mp4",
                "filesize":   v_size,
                "url":        "",          # not a direct URL
                "codec":      vf.get("codec", ""),
                # Merge metadata for the frontend
                "merge":      True,
                "video_url":  vf["url"],
                "audio_url":  best_audio["url"],
            }
            video_audio.append(merged_entry)

    return {
        "title":       info.get("title", "Untitled Video"),
        "thumbnail":   info.get("thumbnail", ""),
        "duration":    info.get("duration_string", info.get("duration", "")),
        "uploader":    info.get("uploader", "Unknown"),
        "view_count":  info.get("view_count"),
        "webpage_url": info.get("webpage_url", url),
        "platform":    platform,
        "formats": {
            "video_audio": video_audio,
            "video_only":  video_only,
            "audio_only":  audio_only,
        },
    }


# ───────────────────────────────────────────────
# API Endpoints
# ───────────────────────────────────────────────

@app.get("/")
def root():
    return {"message": "🎬 Elite Video Downloader API is running!", "docs": "/docs"}


# ── 1. Fetch Video Metadata ──────────────────
@app.post("/api/fetch")
def fetch_video(req: FetchRequest):
    """
    Accept a video URL → return title, thumbnail, and all available
    download qualities grouped by video+audio / video-only / audio-only.
    """
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL cannot be empty.")

    try:
        print(f"\n[FETCH] Extracting info for: {url}")
        data = extract_video_info(url)
        print(f"[FETCH] SUCCESS - title: {data.get('title', '?')}")
        return {"success": True, "data": data}
    except yt_dlp.utils.DownloadError as e:
        print(f"\n[FETCH] ERROR (yt-dlp DownloadError) for URL: {url}")
        print(f"[FETCH] Error message: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=f"Could not process this URL: {str(e)}")
    except Exception as e:
        print(f"\n[FETCH] ERROR (Unexpected) for URL: {url}")
        print(f"[FETCH] Error type: {type(e).__name__}")
        print(f"[FETCH] Error message: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")


# Auth endpoints removed — downloads are 100% free for all users


# ── 4. Download Streamer ─────────────────────
@app.get("/api/download")
async def download_video(
    url: str   = Query(..., description="Direct media URL from yt-dlp"),
    title: str = Query("video", description="Desired filename"),
    ext: str   = Query("mp4", description="File extension"),
):
    """
    Proxy-stream the media file to the browser so it triggers
    a download dialog instead of opening in a new tab.
    """
    if not url:
        raise HTTPException(status_code=400, detail="Download URL is required.")

    # Sanitize filename
    safe_title = re.sub(r'[\\/*?:"<>|]', "", title)[:100]
    filename = f"{safe_title}.{ext}"

    # Determine MIME type
    mime_map = {
        "mp4":  "video/mp4",
        "webm": "video/webm",
        "mkv":  "video/x-matroska",
        "mp3":  "audio/mpeg",
        "m4a":  "audio/mp4",
        "ogg":  "audio/ogg",
        "wav":  "audio/wav",
    }
    content_type = mime_map.get(ext, "application/octet-stream")

    async def stream_generator():
        """Stream the remote file in chunks so we don't load it all in RAM."""
        # Force IPv4 so the proxy egress matches yt-dlp's extraction
        # (googlevideo URLs are IP-bound; IPv4/IPv6 mismatch causes 403s)
        transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
        async with httpx.AsyncClient(follow_redirects=True, timeout=300, transport=transport) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(chunk_size=1024 * 256):
                    yield chunk

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Access-Control-Expose-Headers": "Content-Disposition",
    }

    return StreamingResponse(
        stream_generator(),
        media_type=content_type,
        headers=headers,
    )


# ── 5. Merge Download (Video + Audio → single .mp4) ──
@app.get("/api/merge-download")
async def merge_download(
    video_url: str = Query(..., description="Direct video-only stream URL"),
    audio_url: str = Query(..., description="Direct audio-only stream URL"),
    title: str     = Query("video", description="Desired filename"),
):
    """
    Download video-only and audio-only streams separately, merge them
    with FFmpeg into a single .mp4, then stream the result to the browser.
    """
    if not FFMPEG_PATH:
        raise HTTPException(
            status_code=500,
            detail="FFmpeg is not available on this server. Install with: pip install imageio-ffmpeg"
        )

    if not video_url or not audio_url:
        raise HTTPException(status_code=400, detail="Both video_url and audio_url are required.")

    # Sanitize filename
    safe_title = re.sub(r'[\\/*?:"<>|]', "", title)[:100]
    filename = f"{safe_title}.mp4"

    # Create unique temp directory for this merge
    job_id = uuid.uuid4().hex[:12]
    job_dir = os.path.join(MERGE_TEMP_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    video_path = os.path.join(job_dir, "video_stream.mp4")
    audio_path = os.path.join(job_dir, "audio_stream.m4a")
    output_path = os.path.join(job_dir, filename)

    try:
        print(f"\n[MERGE] Starting merge for: {safe_title}")

        # Download video and audio streams in parallel
        # Force IPv4 so the proxy egress matches yt-dlp's extraction
        # (googlevideo URLs are IP-bound; IPv4/IPv6 mismatch causes 403s)
        transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
        async with httpx.AsyncClient(follow_redirects=True, timeout=300, transport=transport) as client:
            # Download video stream
            print(f"[MERGE] Downloading video stream...")
            async with client.stream("GET", video_url) as resp:
                resp.raise_for_status()
                with open(video_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=1024 * 256):
                        f.write(chunk)

            # Download audio stream
            print(f"[MERGE] Downloading audio stream...")
            async with client.stream("GET", audio_url) as resp:
                resp.raise_for_status()
                with open(audio_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=1024 * 256):
                        f.write(chunk)

        # Merge with FFmpeg
        print(f"[MERGE] Merging with FFmpeg: {FFMPEG_PATH}")
        cmd = [
            FFMPEG_PATH,
            "-y",                   # Overwrite output
            "-i", video_path,       # Video input
            "-i", audio_path,       # Audio input
            "-c:v", "copy",         # Copy video codec (no re-encoding)
            "-c:a", "aac",          # Encode audio to AAC for .mp4 compat
            "-movflags", "+faststart",
            output_path,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace")
            print(f"[MERGE] FFmpeg error: {err_msg}")
            raise HTTPException(status_code=500, detail=f"FFmpeg merge failed: {err_msg[:500]}")

        print(f"[MERGE] SUCCESS - output: {output_path} ({os.path.getsize(output_path)} bytes)")

        # Stream the merged file
        file_size = os.path.getsize(output_path)

        async def stream_merged():
            try:
                with open(output_path, "rb") as f:
                    while chunk := f.read(1024 * 256):
                        yield chunk
            finally:
                # Cleanup temp files after streaming
                try:
                    shutil.rmtree(job_dir, ignore_errors=True)
                    print(f"[MERGE] Cleaned up temp dir: {job_dir}")
                except Exception:
                    pass

        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(file_size),
            "Access-Control-Expose-Headers": "Content-Disposition",
        }

        return StreamingResponse(
            stream_merged(),
            media_type="video/mp4",
            headers=headers,
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"[MERGE] ERROR: {type(e).__name__}: {e}")
        traceback.print_exc()
        # Cleanup on error
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Merge failed: {str(e)}")


# ───────────────────────────────────────────────
# Run with: uvicorn main:app --reload --port 8000
# ───────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
