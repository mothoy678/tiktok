"""
Utility helpers: URL validation, rate limiting, safe filenames, cleanup.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from config import MAX_DOWNLOADS_PER_MINUTE

logger = logging.getLogger(__name__)

# TikTok URL patterns (including short links that yt-dlp can resolve)
TIKTOK_PATTERNS = [
    re.compile(
        r"^https?://(?:www\.|vm\.|vt\.)?tiktok\.com/@[\w.-]+/video/\d+",
        re.IGNORECASE,
    ),
    re.compile(
        r"^https?://(?:www\.|vm\.|vt\.)?tiktok\.com/t/[\w-]+/?",
        re.IGNORECASE,
    ),
    re.compile(
        r"^https?://(?:vm|vt)\.tiktok\.com/[\w-]+/?",
        re.IGNORECASE,
    ),
    re.compile(
        r"^https?://(?:www\.)?tiktok\.com/@[\w.-]+/video/\d+",
        re.IGNORECASE,
    ),
]


def is_valid_tiktok_url(url: str) -> bool:
    """Return True if the URL looks like a supported TikTok video link."""
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return False
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if "tiktok.com" not in host:
        return False
    for pattern in TIKTOK_PATTERNS:
        if pattern.search(url):
            return True
    # Fallback: accept common TikTok path shapes that yt-dlp understands
    path = parsed.path or ""
    if "/video/" in path or path.startswith("/t/") or host.startswith(("vm.", "vt.")):
        return True
    return False


def sanitize_filename(name: str, max_length: int = 80) -> str:
    """Produce a safe filename without path separators or control chars."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(" .")
    if not name:
        name = "tiktok_media"
    return name[:max_length]


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "—"
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def format_size(num_bytes: int | float | None) -> str:
    if num_bytes is None or num_bytes < 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def format_speed(bytes_per_sec: float | None) -> str:
    if not bytes_per_sec or bytes_per_sec <= 0:
        return "—"
    return f"{format_size(bytes_per_sec)}/s"


def format_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "—"
    m, sec = divmod(s, 60)
    return f"{m:02d}:{sec:02d}"


def progress_bar(percentage: float, length: int = 12) -> str:
    """Return a simple text progress bar."""
    filled = int(length * max(0.0, min(100.0, percentage)) / 100)
    return "█" * filled + "░" * (length - filled)


class RateLimiter:
    """Simple in-memory per-user rate limiter."""

    def __init__(self, max_calls: int = MAX_DOWNLOADS_PER_MINUTE, period: float = 60.0) -> None:
        self.max_calls = max_calls
        self.period = period
        self._hits: dict[int, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def is_allowed(self, user_id: int) -> bool:
        async with self._lock:
            now = time.monotonic()
            window = self._hits[user_id]
            # Drop old entries
            self._hits[user_id] = [t for t in window if now - t < self.period]
            if len(self._hits[user_id]) >= self.max_calls:
                return False
            self._hits[user_id].append(now)
            return True

    async def remaining(self, user_id: int) -> int:
        async with self._lock:
            now = time.monotonic()
            window = [t for t in self._hits[user_id] if now - t < self.period]
            return max(0, self.max_calls - len(window))


class UserSessionManager:
    """Lightweight per-user download session store."""

    def __init__(self) -> None:
        self._sessions: dict[int, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, user_id: int) -> dict[str, Any] | None:
        async with self._lock:
            return self._sessions.get(user_id)

    async def set(self, user_id: int, data: dict[str, Any]) -> None:
        async with self._lock:
            self._sessions[user_id] = data

    async def update(self, user_id: int, **kwargs: Any) -> None:
        async with self._lock:
            if user_id in self._sessions:
                self._sessions[user_id].update(kwargs)

    async def clear(self, user_id: int) -> None:
        async with self._lock:
            self._sessions.pop(user_id, None)

    async def is_busy(self, user_id: int) -> bool:
        async with self._lock:
            session = self._sessions.get(user_id)
            if not session:
                return False
            return session.get("status") in (
                "extracting",
                "selecting_quality",
                "downloading",
                "processing",
            )


async def cleanup_file(path: str | Path | None) -> None:
    """Safely delete a single file if it exists."""
    if not path:
        return
    try:
        p = Path(path)
        if p.is_file():
            p.unlink(missing_ok=True)
            logger.debug("Cleaned up file: %s", p)
    except OSError as exc:
        logger.warning("Failed to delete %s: %s", path, exc)


async def cleanup_files(*paths: str | Path | None) -> None:
    for path in paths:
        await cleanup_file(path)


def extract_available_heights(formats: list[dict]) -> set[int]:
    """Extract unique video heights from yt-dlp format list."""
    heights: set[int] = set()
    for f in formats or []:
        h = f.get("height")
        if h and isinstance(h, (int, float)) and h > 0:
            heights.add(int(h))
        # Some TikTok formats only have resolution string
        res = f.get("resolution") or ""
        if isinstance(res, str) and "x" in res:
            try:
                parts = res.lower().split("x")
                if len(parts) == 2:
                    heights.add(int(parts[1]))
            except ValueError:
                pass
    return heights


def best_height_at_or_below(available: set[int], requested: int) -> int | None:
    """Return the highest available height that is <= requested."""
    candidates = [h for h in available if h <= requested]
    return max(candidates) if candidates else None
