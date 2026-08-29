"""
TikTok Downloader Telegram Bot — entry point.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from config import ADMIN_ID, BOT_TOKEN, LOGS_DIR
from database import db
from downloader import ensure_ffmpeg
from handlers import (
    admin_command,
    callback_router,
    handle_url,
    help_command,
    start_command,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOGS_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOGS_DIR / "bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("bot")

# Reduce noisy loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("yt_dlp").setLevel(logging.WARNING)


async def post_init(application: Application) -> None:
    await db.connect()
    logger.info("Database connected.")
    if not ensure_ffmpeg():
        logger.warning(
            "FFmpeg not found on PATH. Audio conversion may fail. "
            "Install FFmpeg and ensure it is available."
        )
    else:
        logger.info("FFmpeg is available.")


async def post_shutdown(application: Application) -> None:
    await db.close()
    logger.info("Database closed.")


def main() -> None:
    if not BOT_TOKEN:
        logger.error(
            "BOT_TOKEN is not set. Copy .env.example to .env and add your token."
        )
        sys.exit(1)

    if not ADMIN_ID:
        logger.warning("ADMIN_ID is not set. Admin panel will be inaccessible.")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .concurrent_updates(True)
        .build()
    )

    # Commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_command))

    # Callbacks (single router)
    application.add_handler(CallbackQueryHandler(callback_router))

    # TikTok URLs (any text that looks like a URL)
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_url,
        )
    )

    logger.info("ចាប់ផ្តើម TikTok Downloader Bot…🔥")
    application.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
