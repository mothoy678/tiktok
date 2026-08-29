"""
Media extraction and download using yt-dlp + FFmpeg.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Awaitable

import yt_dlp

from config import (
    DOWNLOAD_TIMEOUT,
    MUSIC_DIR,
    QUALITY_MAP,
    VIDEOS_DIR,
    YTDLP_OPTS_BASE,
)
from utils import (
    best_height_at_or_below,
    cleanup_file,
    extract_available_heights,
    sanitize_filename,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]


class DownloadError(Exception):
    """Raised when extraction or download fails."""

    def __init__(self, message: str, *, user_message: str | None = None) -> None:
        super().__init__(message)
        self.user_message = user_message or message


def _run_yt_dlp(opts: dict, url: str) -> dict[str, Any]:
    """Synchronous yt-dlp extract_info (runs in executor)."""
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info:
            raise DownloadError(
                "No media information returned",
                user_message="Could not retrieve video information.",
            )
        return info


async def extract_info(url: str) -> dict[str, Any]:
    """Extract media metadata without downloading."""
    opts = {
        **YTDLP_OPTS_BASE,
        "skip_download": True,
    }
    loop = asyncio.get_running_loop()
    try:
        info = await asyncio.wait_for(
            loop.run_in_executor(None, _run_yt_dlp, opts, url),
            timeout=60,
        )
        return info
    except asyncio.TimeoutError as exc:
        raise DownloadError(
            "Extraction timed out",
            user_message="The request timed out. Please try again later.",
        ) from exc
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc).lower()
        if "private" in msg or "login" in msg:
            user_msg = "This content is private or requires login."
        elif "unavailable" in msg or "not available" in msg:
            user_msg = "This video is unavailable or has been removed."
        elif "geo" in msg or "region" in msg:
            user_msg = "This content is restricted in the current region."
        else:
            user_msg = "Could not process this TikTok URL."
        raise DownloadError(str(exc), user_message=user_msg) from exc
    except Exception as exc:
        logger.exception("Unexpected extraction error")
        raise DownloadError(
            str(exc),
            user_message="An unexpected error occurred while reading the video.",
        ) from exc


def parse_media_info(info: dict[str, Any]) -> dict[str, Any]:
    """Normalize useful fields from yt-dlp info dict."""
    formats = info.get("formats") or []
    heights = extract_available_heights(formats)
    # Also consider the overall height if present
    if info.get("height"):
        try:
            heights.add(int(info["height"]))
        except (TypeError, ValueError):
            pass

    uploader = (
        info.get("uploader")
        or info.get("creator")
        or info.get("channel")
        or info.get("uploader_id")
        or "Unknown"
    )
    # Prefer @username style
    uploader_id = info.get("uploader_id") or info.get("channel_id") or ""
    if uploader_id and not str(uploader).startswith("@"):
        creator_display = f"@{uploader_id}" if not str(uploader_id).startswith("@") else uploader_id
    else:
        creator_display = str(uploader)

    title = info.get("title") or info.get("fulltitle") or "TikTok Video"
    duration = info.get("duration")
    thumbnail = info.get("thumbnail")

    return {
        "id": info.get("id") or info.get("display_id") or "unknown",
        "title": title,
        "creator": creator_display,
        "duration": duration,
        "thumbnail": thumbnail,
        "heights": heights,
        "max_height": max(heights) if heights else None,
        "formats": formats,
        "raw": info,
    }


async def download_video(
    url: str,
    quality_key: str,
    available_heights: set[int],
    progress_cb: ProgressCallback | None = None,
    cancel_event: asyncio.Event | None = None,
) -> Path:
    """
    Download video at or below the requested quality.
    Returns path to the final video file.
    """
    requested = QUALITY_MAP.get(quality_key)
    if requested is None:
        try:
            requested = int(quality_key)
        except ValueError as exc:
            raise DownloadError(
                f"Unknown quality: {quality_key}",
                user_message="Invalid quality selection.",
            ) from exc

    best = best_height_at_or_below(available_heights, requested)
    if best is None:
        raise DownloadError(
            f"No stream <= {requested}p",
            user_message=f"{requested}p is not available for this video.",
        )

    # yt-dlp format: best video <= height + best audio, merge if needed
    # Prefer mp4 container for Telegram compatibility
    format_selector = (
        f"bestvideo[height<={best}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={best}]+bestaudio/"
        f"best[height<={best}]/"
        f"best"
    )

    out_template = str(VIDEOS_DIR / f"%(id)s_{best}p.%(ext)s")
    progress_state: dict[str, Any] = {"last_update": 0.0}

    def _hook(d: dict[str, Any]) -> None:
        if cancel_event and cancel_event.is_set():
            raise yt_dlp.utils.DownloadError("Download cancelled by user")
        if d.get("status") == "downloading" and progress_cb:
            # Throttle is handled by the async wrapper
            progress_state["data"] = d

    opts = {
        **YTDLP_OPTS_BASE,
        "format": format_selector,
        "outtmpl": out_template,
        "merge_output_format": "mp4",
        "progress_hooks": [_hook],
        "noprogress": True,
    }

    loop = asyncio.get_running_loop()
    last_progress_sent = 0.0

    async def _download() -> Path:
        nonlocal last_progress_sent

        def _run() -> dict:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)

        # Poll progress while download runs
        task = loop.run_in_executor(None, _run)
        while not task.done():
            if cancel_event and cancel_event.is_set():
                # yt-dlp doesn't support true cancel easily; we rely on timeout / process kill
                # Best effort: wait a bit then raise
                await asyncio.sleep(0.5)
                if not task.done():
                    raise DownloadError(
                        "Cancelled",
                        user_message="Download cancelled.",
                    )
            if progress_cb and "data" in progress_state:
                now = asyncio.get_event_loop().time()
                if now - last_progress_sent >= 2.5:
                    await progress_cb(progress_state["data"])
                    last_progress_sent = now
            await asyncio.sleep(0.4)

        info = await task
        if not info:
            raise DownloadError(
                "Download returned no info",
                user_message="Download failed.",
            )

        # Resolve output path
        filename = None
        if "requested_downloads" in info:
            for rd in info["requested_downloads"]:
                if rd.get("filepath"):
                    filename = rd["filepath"]
                    break
        if not filename:
            filename = info.get("_filename") or info.get("filename")
        if not filename:
            # Fallback search
            vid_id = info.get("id", "unknown")
            candidates = list(VIDEOS_DIR.glob(f"{vid_id}*"))
            if candidates:
                filename = str(candidates[0])
        if not filename or not Path(filename).is_file():
            raise DownloadError(
                "Output file not found after download",
                user_message="Download completed but file is missing.",
            )
        return Path(filename)

    try:
        path = await asyncio.wait_for(_download(), timeout=DOWNLOAD_TIMEOUT)
        return path
    except asyncio.TimeoutError as exc:
        raise DownloadError(
            "Download timed out",
            user_message="Download timed out. Please try a lower quality.",
        ) from exc
    except DownloadError:
        raise
    except yt_dlp.utils.DownloadError as exc:
        if "cancelled" in str(exc).lower():
            raise DownloadError("Cancelled", user_message="Download cancelled.") from exc
        raise DownloadError(str(exc), user_message="Download failed.") from exc
    except Exception as exc:
        logger.exception("Video download error")
        raise DownloadError(str(exc), user_message="An error occurred during download.") from exc


async def download_audio(
    url: str,
    progress_cb: ProgressCallback | None = None,
    cancel_event: asyncio.Event | None = None,
) -> tuple[Path, dict[str, Any]]:
    """
    Download best audio and convert to mp3 via FFmpeg if needed.
    Returns (path, metadata dict).
    """
    out_template = str(MUSIC_DIR / "%(id)s_audio.%(ext)s")
    progress_state: dict[str, Any] = {}

    def _hook(d: dict[str, Any]) -> None:
        if cancel_event and cancel_event.is_set():
            raise yt_dlp.utils.DownloadError("Download cancelled by user")
        if d.get("status") == "downloading":
            progress_state["data"] = d

    opts = {
        **YTDLP_OPTS_BASE,
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "progress_hooks": [_hook],
        "noprogress": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }

    loop = asyncio.get_running_loop()
    last_progress_sent = 0.0

    async def _download() -> tuple[Path, dict]:
        nonlocal last_progress_sent

        def _run() -> dict:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)

        task = loop.run_in_executor(None, _run)
        while not task.done():
            if cancel_event and cancel_event.is_set():
                await asyncio.sleep(0.5)
                if not task.done():
                    raise DownloadError("Cancelled", user_message="Download cancelled.")
            if progress_cb and "data" in progress_state:
                now = asyncio.get_event_loop().time()
                if now - last_progress_sent >= 2.5:
                    await progress_cb(progress_state["data"])
                    last_progress_sent = now
            await asyncio.sleep(0.4)

        info = await task
        if not info:
            raise DownloadError("No audio info", user_message="Audio download failed.")

        # After FFmpegExtractAudio the file should be .mp3
        vid_id = info.get("id", "unknown")
        candidates = list(MUSIC_DIR.glob(f"{vid_id}*"))
        # Prefer mp3
        mp3s = [c for c in candidates if c.suffix.lower() == ".mp3"]
        path = mp3s[0] if mp3s else (candidates[0] if candidates else None)
        if not path or not path.is_file():
            # Try requested_downloads
            for rd in info.get("requested_downloads") or []:
                fp = rd.get("filepath")
                if fp and Path(fp).is_file():
                    path = Path(fp)
                    break
        if not path or not path.is_file():
            raise DownloadError(
                "Audio file not found",
                user_message="Audio extraction failed.",
            )

        meta = {
            "title": info.get("title") or info.get("track") or "TikTok Music",
            "artist": (
                info.get("artist")
                or info.get("uploader")
                or info.get("creator")
                or info.get("uploader_id")
                or None
            ),
            "duration": info.get("duration"),
        }
        return path, meta

    try:
        return await asyncio.wait_for(_download(), timeout=DOWNLOAD_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise DownloadError(
            "Audio download timed out",
            user_message="Audio download timed out.",
        ) from exc
    except DownloadError:
        raise
    except Exception as exc:
        logger.exception("Audio download error")
        raise DownloadError(str(exc), user_message="Audio download failed.") from exc


async def convert_to_mp3(input_path: Path, output_path: Path | None = None) -> Path:
    """
    Convert any media file to MP3 using FFmpeg (safe subprocess).
    """
    if output_path is None:
        output_path = input_path.with_suffix(".mp3")

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-codec:a",
        "libmp3lame",
        "-q:a",
        "2",
        str(output_path),
    ]
    loop = asyncio.get_running_loop()

    def _run() -> None:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            raise DownloadError(
                f"FFmpeg failed: {result.stderr[:500]}",
                user_message="Audio conversion failed.",
            )

    try:
        await loop.run_in_executor(None, _run)
    except subprocess.TimeoutExpired as exc:
        raise DownloadError(
            "FFmpeg timed out",
            user_message="Audio conversion timed out.",
        ) from exc

    if not output_path.is_file():
        raise DownloadError(
            "FFmpeg output missing",
            user_message="Audio conversion failed.",
        )
    return output_path


def ensure_ffmpeg() -> bool:
    """Return True if ffmpeg is available on PATH."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
