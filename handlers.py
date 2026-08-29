"""
Telegram command and callback handlers.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from telegram import Update, InputFile
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import (
    ADMIN_ID,
    MAX_FILE_SIZE_BYTES,
    MAX_CONCURRENT_DOWNLOADS,
    QUALITY_LABELS,
    QUALITY_MAP,
)
from database import db
from downloader import (
    DownloadError,
    download_audio,
    download_video,
    extract_info,
    parse_media_info,
)
from keyboards import (
    admin_keyboard,
    cancel_only_keyboard,
    fallback_quality_keyboard,
    large_file_keyboard,
    quality_keyboard,
    start_keyboard,
)
from utils import (
    RateLimiter,
    UserSessionManager,
    cleanup_file,
    cleanup_files,
    format_duration,
    format_eta,
    format_size,
    format_speed,
    is_valid_tiktok_url,
    progress_bar,
)

logger = logging.getLogger(__name__)

sessions = UserSessionManager()
rate_limiter = RateLimiter()
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# Active cancel events per user
cancel_events: dict[int, asyncio.Event] = {}


def _get_cancel_event(user_id: int) -> asyncio.Event:
    if user_id not in cancel_events:
        cancel_events[user_id] = asyncio.Event()
    return cancel_events[user_id]


def _clear_cancel_event(user_id: int) -> None:
    cancel_events.pop(user_id, None)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return

    await db.upsert_user(user.id, user.username)

    text = (
        "🎬 *TikTok Downloader Owner by @doyoumissmefr pozz smos*\n\n"
        "Download TikTok វីដេអូ ឬ តន្ត្រីបានយ៉ាងងាយស្រួល.\n\n"
        "📥 Send me a TikTok URL to get started.\n\n"
        "*Supported:*\n"
        "• Video Download\n"
        "• Multiple video qualities\n"
        "• Music / Audio Download\n"
        "• Fast processing"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=start_keyboard(),
    )


# ---------------------------------------------------------------------------
# /help (and help callback)
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "📖 *Help*\n\n"
    "1. Send a TikTok video link (full or short URL).\n"
    "2. Choose a video quality or *Download Music*.\n"
    "3. Wait for the bot to process and send the file.\n\n"
    "*Tips:*\n"
    "• Only public videos are supported.\n"
    "• Large files may require a lower quality.\n"
    "• Use ❌ Cancel to stop an active download.\n\n"
    "The bot respects TikTok terms and copyright. "
    "Only download content you are allowed to use."
)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    await query.edit_message_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# URL message handler
# ---------------------------------------------------------------------------

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    if not user or not message or not message.text:
        return

    url = message.text.strip()
    if not is_valid_tiktok_url(url):
        await message.reply_text(
            "❌ *មិនត្រឹមត្រូវ TikTok URL.*\n\nPlease send a valid TikTok video link.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Rate limit
    if not await rate_limiter.is_allowed(user.id):
        await message.reply_text(
            "⏳ *សំណើច្រើនពេក.*\n\n សូមរង់ចាំមួយភ្លែតមុនពេលព្យាយាមម្តងទៀត.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Session busy check
    if await sessions.is_busy(user.id):
        await message.reply_text(
            "⏳ You already have an active download.\n"
            "Please finish or cancel it first.",
        )
        return

    await db.upsert_user(user.id, user.username)

    status_msg = await message.reply_text(
        "🔎 *កំពុងដំណើរការ TikTok URL...*\n\n សូមរង់ចាំ.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_only_keyboard(),
    )

    await sessions.set(
        user.id,
        {
            "url": url,
            "status": "extracting",
            "status_message_id": status_msg.message_id,
            "chat_id": message.chat_id,
        },
    )
    cancel_event = _get_cancel_event(user.id)
    cancel_event.clear()

    try:
        if cancel_event.is_set():
            raise DownloadError("Cancelled", user_message="Download cancelled.")

        info = await extract_info(url)
        if cancel_event.is_set():
            raise DownloadError("Cancelled", user_message="Download cancelled.")

        media = parse_media_info(info)
        await sessions.update(
            user.id,
            info=media,
            status="selecting_quality",
        )

        heights = media["heights"]
        max_h = media["max_height"]
        available_str = f"{max_h}p" if max_h else "unknown"

        preview = (
            "🎬 *TikTok Video*\n\n"
            f"👤 Creator: `{media['creator']}`\n"
            f"⏱ Duration: `{format_duration(media['duration'])}`\n"
            f"📦 Available quality: `{available_str}`\n\n"
            "Choose your download option:"
        )
        await status_msg.edit_text(
            preview,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=quality_keyboard(heights, max_h),
        )

    except DownloadError as exc:
        logger.warning("Extract failed for user %s: %s", user.id, exc)
        await sessions.clear(user.id)
        _clear_cancel_event(user.id)
        try:
            await status_msg.edit_text(
                f"❌ *Sorry, I couldn't download this video.*\n\n"
                f"{exc.user_message}\n\n"
                "Please check that:\n"
                "• The URL is correct\n"
                "• The content is publicly accessible\n"
                "• The video is still available",
                parse_mode=ParseMode.MARKDOWN,
            )
        except TelegramError:
            pass
    except Exception as exc:
        logger.exception("Unexpected error handling URL for user %s", user.id)
        await sessions.clear(user.id)
        _clear_cancel_event(user.id)
        try:
            await status_msg.edit_text(
                "❌ *Sorry, I couldn't download this video.*\n\n"
                "Please check that:\n"
                "• The URL is correct\n"
                "• The content is publicly accessible\n"
                "• The video is still available",
                parse_mode=ParseMode.MARKDOWN,
            )
        except TelegramError:
            pass


# ---------------------------------------------------------------------------
# Callback query router
# ---------------------------------------------------------------------------

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.from_user:
        return

    user_id = query.from_user.id
    data = query.data

    # Ownership: only the session owner may act (except help / admin / noop)
    session = await sessions.get(user_id)

    if data == "help":
        await help_callback(update, context)
        return

    if data == "noop":
        await query.answer("This quality is not available.", show_alert=False)
        return

    if data.startswith("admin:"):
        await admin_callback(update, context)
        return

    # Session-required actions
    if data in ("cancel", "back", "music") or data.startswith("quality:"):
        if not session and data != "cancel":
            await query.answer("Session expired. Send a new URL.", show_alert=True)
            return
        # Verify chat ownership implicitly via session presence for this user

    if data == "cancel":
        await cancel_callback(update, context)
        return

    if data == "back":
        await back_callback(update, context)
        return

    if data == "music":
        await music_callback(update, context)
        return

    if data.startswith("quality:"):
        await quality_callback(update, context)
        return

    await query.answer()


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return
    user_id = query.from_user.id
    await query.answer("Cancelling…")

    event = _get_cancel_event(user_id)
    event.set()

    session = await sessions.get(user_id)
    if session:
        # Best-effort cleanup of any known temp paths
        for key in ("video_path", "audio_path"):
            await cleanup_file(session.get(key))
        await sessions.clear(user_id)

    _clear_cancel_event(user_id)

    try:
        await query.edit_message_text("❌ *Download cancelled.*", parse_mode=ParseMode.MARKDOWN)
    except TelegramError:
        pass


# ---------------------------------------------------------------------------
# Back to quality menu
# ---------------------------------------------------------------------------

async def back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return
    user_id = query.from_user.id
    session = await sessions.get(user_id)
    if not session or "info" not in session:
        await query.answer("Session expired.", show_alert=True)
        return

    await query.answer()
    media = session["info"]
    heights = media["heights"]
    max_h = media["max_height"]
    available_str = f"{max_h}p" if max_h else "unknown"

    preview = (
        "🎬 *TikTok Video*\n\n"
        f"👤 Creator: `{media['creator']}`\n"
        f"⏱ Duration: `{format_duration(media['duration'])}`\n"
        f"📦 Available quality: `{available_str}`\n\n"
        "Choose your download option:"
    )
    await sessions.update(user_id, status="selecting_quality")
    try:
        await query.edit_message_text(
            preview,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=quality_keyboard(heights, max_h),
        )
    except TelegramError:
        pass


# ---------------------------------------------------------------------------
# Quality selection
# ---------------------------------------------------------------------------

async def quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.from_user or not query.message:
        return

    user_id = query.from_user.id
    session = await sessions.get(user_id)
    if not session or "info" not in session:
        await query.answer("Session expired. Send a new URL.", show_alert=True)
        return

    quality_key = query.data.split(":", 1)[1]
    media = session["info"]
    heights: set[int] = media["heights"]
    url = session["url"]

    requested = QUALITY_MAP.get(quality_key)
    if requested is None:
        try:
            requested = int(quality_key)
        except ValueError:
            await query.answer("Invalid quality.", show_alert=True)
            return

    # Check availability (no upscaling)
    from utils import best_height_at_or_below

    best = best_height_at_or_below(heights, requested)
    if best is None:
        await query.answer()
        avail_list = ", ".join(f"{h}p" for h in sorted(heights, reverse=True)) or "none"
        text = (
            f"⚠️ *{requested}p is not available.*\n\n"
            f"Available quality:\n`{avail_list}`\n\n"
            "Would you like to download a lower quality?"
        )
        try:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=fallback_quality_keyboard(heights),
            )
        except TelegramError:
            pass
        return

    label = QUALITY_LABELS.get(quality_key, f"{requested}p")
    await query.answer(f"Downloading {label}…")

    await sessions.update(user_id, status="downloading", quality=quality_key)
    cancel_event = _get_cancel_event(user_id)
    cancel_event.clear()

    status_msg = query.message
    download_id = await db.log_download(
        user_id, url, "video", quality_key, "pending"
    )

    try:
        await status_msg.edit_text(
            f"📥 *Preparing your video...*\n\n"
            f"Quality: `{label}`\n"
            f"⏳ Downloading...",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=cancel_only_keyboard(),
        )
    except TelegramError:
        pass

    async def progress_cb(d: dict[str, Any]) -> None:
        if cancel_event.is_set():
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        downloaded = d.get("downloaded_bytes") or 0
        speed = d.get("speed")
        eta = d.get("eta")
        pct = (downloaded / total * 100) if total else 0
        bar = progress_bar(pct)
        text = (
            f"📥 *Downloading...*\n\n"
            f"`{bar}` {pct:.0f}%\n\n"
            f"Size: `{format_size(downloaded)}"
            + (f" / {format_size(total)}`" if total else "`")
            + f"\nSpeed: `{format_speed(speed)}`\n"
            f"ETA: `{format_eta(eta)}`"
        )
        try:
            await status_msg.edit_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=cancel_only_keyboard(),
            )
        except TelegramError:
            pass

    video_path: Path | None = None
    try:
        async with download_semaphore:
            if cancel_event.is_set():
                raise DownloadError("Cancelled", user_message="Download cancelled.")

            await context.bot.send_chat_action(
                chat_id=status_msg.chat_id, action=ChatAction.UPLOAD_VIDEO
            )
            video_path = await download_video(
                url,
                quality_key,
                heights,
                progress_cb=progress_cb,
                cancel_event=cancel_event,
            )
            await sessions.update(user_id, video_path=str(video_path))

        if cancel_event.is_set():
            raise DownloadError("Cancelled", user_message="Download cancelled.")

        file_size = video_path.stat().st_size
        if file_size > MAX_FILE_SIZE_BYTES:
            await db.update_download_status(download_id, "failed")
            await cleanup_file(video_path)
            text = (
                "⚠️ *File is too large to send through this bot.*\n\n"
                "Please choose a lower quality."
            )
            await status_msg.edit_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=large_file_keyboard(heights),
            )
            await sessions.update(user_id, status="selecting_quality", video_path=None)
            return

        # Send video
        await status_msg.edit_text(
            "✅ *Upload ready!*\n\nSending video…",
            parse_mode=ParseMode.MARKDOWN,
        )
        await context.bot.send_chat_action(
            chat_id=status_msg.chat_id, action=ChatAction.UPLOAD_VIDEO
        )

        caption = (
            f"🎬 *TikTok Video*\n"
            f"👤 `{media['creator']}`\n"
            f"📦 `{label}`"
        )
        with open(video_path, "rb") as f:
            await context.bot.send_video(
                chat_id=status_msg.chat_id,
                video=InputFile(f, filename=video_path.name),
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                supports_streaming=True,
                read_timeout=120,
                write_timeout=120,
            )

        await db.update_download_status(download_id, "success")
        try:
            await status_msg.delete()
        except TelegramError:
            pass

    except DownloadError as exc:
        logger.warning("Video download failed user=%s: %s", user_id, exc)
        await db.update_download_status(download_id, "failed")
        if "cancel" in str(exc).lower():
            try:
                await status_msg.edit_text(
                    "❌ *Download cancelled.*", parse_mode=ParseMode.MARKDOWN
                )
            except TelegramError:
                pass
        else:
            try:
                await status_msg.edit_text(
                    f"❌ *Sorry, I couldn't download this video.*\n\n"
                    f"{exc.user_message}",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except TelegramError:
                pass
    except TelegramError as exc:
        logger.exception("Telegram send error user=%s", user_id)
        await db.update_download_status(download_id, "failed")
        try:
            await status_msg.edit_text(
                "❌ *Failed to send the video.*\n\n"
                "The file may be too large or Telegram is temporarily unavailable.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except TelegramError:
            pass
    except Exception as exc:
        logger.exception("Unexpected video error user=%s", user_id)
        await db.update_download_status(download_id, "failed")
        try:
            await status_msg.edit_text(
                "❌ *Sorry, I couldn't download this video.*\n\n"
                "Please try again later.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except TelegramError:
            pass
    finally:
        await cleanup_file(video_path)
        await sessions.clear(user_id)
        _clear_cancel_event(user_id)


# ---------------------------------------------------------------------------
# Music download
# ---------------------------------------------------------------------------

async def music_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not query.message:
        return

    user_id = query.from_user.id
    session = await sessions.get(user_id)
    if not session or "info" not in session:
        await query.answer("Session expired. Send a new URL.", show_alert=True)
        return

    await query.answer("Downloading music…")
    url = session["url"]
    media = session["info"]

    await sessions.update(user_id, status="downloading", quality="music")
    cancel_event = _get_cancel_event(user_id)
    cancel_event.clear()

    status_msg = query.message
    download_id = await db.log_download(user_id, url, "music", None, "pending")

    try:
        await status_msg.edit_text(
            "🎵 *Preparing music...*\n\n⏳ Downloading audio...",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=cancel_only_keyboard(),
        )
    except TelegramError:
        pass

    async def progress_cb(d: dict[str, Any]) -> None:
        if cancel_event.is_set():
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        downloaded = d.get("downloaded_bytes") or 0
        speed = d.get("speed")
        eta = d.get("eta")
        pct = (downloaded / total * 100) if total else 0
        bar = progress_bar(pct)
        text = (
            f"🎵 *Downloading audio...*\n\n"
            f"`{bar}` {pct:.0f}%\n\n"
            f"Size: `{format_size(downloaded)}"
            + (f" / {format_size(total)}`" if total else "`")
            + f"\nSpeed: `{format_speed(speed)}`\n"
            f"ETA: `{format_eta(eta)}`"
        )
        try:
            await status_msg.edit_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=cancel_only_keyboard(),
            )
        except TelegramError:
            pass

    audio_path: Path | None = None
    try:
        async with download_semaphore:
            if cancel_event.is_set():
                raise DownloadError("Cancelled", user_message="Download cancelled.")

            await context.bot.send_chat_action(
                chat_id=status_msg.chat_id, action=ChatAction.UPLOAD_VOICE
            )
            audio_path, meta = await download_audio(
                url,
                progress_cb=progress_cb,
                cancel_event=cancel_event,
            )
            await sessions.update(user_id, audio_path=str(audio_path))

        if cancel_event.is_set():
            raise DownloadError("Cancelled", user_message="Download cancelled.")

        file_size = audio_path.stat().st_size
        if file_size > MAX_FILE_SIZE_BYTES:
            await db.update_download_status(download_id, "failed")
            await cleanup_file(audio_path)
            await status_msg.edit_text(
                "⚠️ *Audio file is too large to send.*",
                parse_mode=ParseMode.MARKDOWN,
            )
            await sessions.clear(user_id)
            _clear_cancel_event(user_id)
            return

        await status_msg.edit_text(
            "✅ *Music ready!*\n\nSending…",
            parse_mode=ParseMode.MARKDOWN,
        )

        title = meta.get("title") or "TikTok Music"
        artist = meta.get("artist")
        caption_parts = ["🎵 *TikTok Music*", f"Title: `{title}`"]
        if artist:
            caption_parts.append(f"Artist: `{artist}`")
        caption = "\n".join(caption_parts)

        with open(audio_path, "rb") as f:
            await context.bot.send_audio(
                chat_id=status_msg.chat_id,
                audio=InputFile(f, filename=audio_path.name),
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                title=title[:64] if title else None,
                performer=str(artist)[:64] if artist else None,
                duration=int(meta["duration"]) if meta.get("duration") else None,
                read_timeout=120,
                write_timeout=120,
            )

        await db.update_download_status(download_id, "success")
        try:
            await status_msg.delete()
        except TelegramError:
            pass

    except DownloadError as exc:
        logger.warning("Music download failed user=%s: %s", user_id, exc)
        await db.update_download_status(download_id, "failed")
        msg = (
            "❌ *Download cancelled.*"
            if "cancel" in str(exc).lower()
            else f"❌ *Sorry, I couldn't download the audio.*\n\n{exc.user_message}"
        )
        try:
            await status_msg.edit_text(msg, parse_mode=ParseMode.MARKDOWN)
        except TelegramError:
            pass
    except Exception as exc:
        logger.exception("Unexpected music error user=%s", user_id)
        await db.update_download_status(download_id, "failed")
        try:
            await status_msg.edit_text(
                "❌ *Sorry, I couldn't download the audio.*\n\nPlease try again later.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except TelegramError:
            pass
    finally:
        await cleanup_file(audio_path)
        await sessions.clear(user_id)
        _clear_cancel_event(user_id)


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return

    if user.id != ADMIN_ID:
        await update.message.reply_text("❌ You are not authorized.")
        return

    stats = await db.get_stats()
    text = (
        "👑 *Admin Panel*\n\n"
        f"👥 Users: `{stats['users']:,}`\n"
        f"📥 Downloads: `{stats['downloads']:,}`\n"
        f"✅ Success: `{stats['success']:,}`\n"
        f"❌ Failed: `{stats['failed']:,}`\n\n"
        f"🎬 Videos: `{stats['videos']:,}`\n"
        f"🎵 Music: `{stats['music']:,}`"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_keyboard(),
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not query.data:
        return

    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorized.", show_alert=True)
        return

    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "stats":
        stats = await db.get_stats()
        text = (
            "📊 *Statistics*\n\n"
            f"👥 Users: `{stats['users']:,}`\n"
            f"📥 Downloads: `{stats['downloads']:,}`\n"
            f"✅ Success: `{stats['success']:,}`\n"
            f"❌ Failed: `{stats['failed']:,}`\n\n"
            f"🎬 Videos: `{stats['videos']:,}`\n"
            f"🎵 Music: `{stats['music']:,}`"
        )
        try:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=admin_keyboard(),
            )
        except TelegramError:
            pass

    elif action == "users":
        users = await db.get_recent_users(15)
        if not users:
            body = "No users yet."
        else:
            lines = []
            for u in users:
                uname = f"@{u['username']}" if u.get("username") else "—"
                lines.append(f"• `{u['telegram_user_id']}` {uname}")
            body = "\n".join(lines)
        text = f"👥 *Recent Users*\n\n{body}"
        try:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=admin_keyboard(),
            )
        except TelegramError:
            pass

    elif action == "cleanup":
        from config import VIDEOS_DIR, MUSIC_DIR

        removed = 0
        for folder in (VIDEOS_DIR, MUSIC_DIR):
            for f in folder.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
        text = f"🧹 *Cleanup*\n\nRemoved `{removed}` temporary file(s)."
        try:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=admin_keyboard(),
            )
        except TelegramError:
            pass
