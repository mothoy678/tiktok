"""
Configuration module for TikTok Downloader Bot.
Loads settings from environment variables.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Telegram
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0") or "0")

# Paths
DOWNLOAD_DIR: Path = BASE_DIR / os.getenv("DOWNLOAD_DIR", "downloads")
VIDEOS_DIR: Path = DOWNLOAD_DIR / "videos"
MUSIC_DIR: Path = DOWNLOAD_DIR / "music"
LOGS_DIR: Path = BASE_DIR / "logs"
DATABASE_PATH: Path = BASE_DIR / "bot.db"

# Limits
MAX_FILE_SIZE_MB: int = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_FILE_SIZE_BYTES: int = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_CONCURRENT_DOWNLOADS: int = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))
MAX_DOWNLOADS_PER_MINUTE: int = int(os.getenv("MAX_DOWNLOADS_PER_MINUTE", "5"))
DOWNLOAD_TIMEOUT: int = int(os.getenv("DOWNLOAD_TIMEOUT", "300"))  # seconds

# Ensure directories exist
for path in (VIDEOS_DIR, MUSIC_DIR, LOGS_DIR):
    path.mkdir(parents=True, exist_ok=True)

# Quality mapping (label -> max height)
QUALITY_MAP: dict[str, int] = {
    "2160": 2160,
    "1440": 1440,
    "1080": 1080,
    "720": 720,
    "420": 420,
    "360": 360,
    "144": 144,
}

QUALITY_LABELS: dict[str, str] = {
    "2160": "4K / 2160p",
    "1440": "2K / 1440p",
    "1080": "1080p",
    "720": "720p",
    "420": "420p",
    "360": "360p",
    "144": "144p",
}

# yt-dlp options base
YTDLP_OPTS_BASE: dict = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "extract_flat": False,
    "socket_timeout": 30,
    "retries": 3,
    "fragment_retries": 3,
}
