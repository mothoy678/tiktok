"""
Inline keyboard builders for the TikTok Downloader Bot.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import QUALITY_LABELS, QUALITY_MAP


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📖 Help", callback_data="help")]]
    )


def quality_keyboard(
    available_heights: set[int],
    max_available: int | None = None,
) -> InlineKeyboardMarkup:
    """
    Build quality selection keyboard.
    Only show qualities that are available or can be served from a lower/equal source.
    """
    rows: list[list[InlineKeyboardButton]] = []
    # Ordered from highest to lowest
    ordered = sorted(QUALITY_MAP.keys(), key=lambda k: QUALITY_MAP[k], reverse=True)

    for key in ordered:
        height = QUALITY_MAP[key]
        label = QUALITY_LABELS[key]
        # A quality is selectable if there is at least one stream <= height
        # (we never upscale). Prefer exact or lower.
        can_offer = any(h <= height for h in available_heights) if available_heights else False
        # Prefer exact match display
        exact = height in available_heights
        if exact:
            rows.append(
                [InlineKeyboardButton(f"✅ {label}", callback_data=f"quality:{key}")]
            )
        elif can_offer:
            # Available via lower source – still offer, will pick best <= height
            rows.append(
                [InlineKeyboardButton(label, callback_data=f"quality:{key}")]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{label} — ❌ Not available",
                        callback_data="noop",
                    )
                ]
            )

    rows.append(
        [InlineKeyboardButton("🎵 Download ជាសម្លេង🎵", callback_data="music")]
    )
    rows.append(
        [InlineKeyboardButton("❌ ចេញ", callback_data="cancel")]
    )
    return InlineKeyboardMarkup(rows)


def fallback_quality_keyboard(available_heights: set[int]) -> InlineKeyboardMarkup:
    """Shown when requested quality is unavailable – offer actual available ones."""
    rows: list[list[InlineKeyboardButton]] = []
    ordered = sorted(available_heights, reverse=True)
    for h in ordered:
        # Map to closest known key
        key = None
        for k, v in QUALITY_MAP.items():
            if v == h:
                key = k
                break
        if key is None:
            # Use height as key
            key = str(h)
        label = QUALITY_LABELS.get(key, f"{h}p")
        rows.append(
            [InlineKeyboardButton(f"Download {label}", callback_data=f"quality:{key}")]
        )
    rows.append([InlineKeyboardButton("◀️ ត្រឡប់", callback_data="back")])
    rows.append([InlineKeyboardButton("❌ បោះបង់", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def large_file_keyboard(available_heights: set[int]) -> InlineKeyboardMarkup:
    """Offer lower qualities when file exceeds Telegram limit."""
    lower = sorted(
        [h for h in available_heights if h <= 720],
        reverse=True,
    )
    rows: list[list[InlineKeyboardButton]] = []
    for h in lower:
        key = next((k for k, v in QUALITY_MAP.items() if v == h), str(h))
        label = QUALITY_LABELS.get(key, f"{h}p")
        rows.append(
            [InlineKeyboardButton(label, callback_data=f"quality:{key}")]
        )
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 ស្ថិតិ", callback_data="admin:stats")],
            [InlineKeyboardButton("👥 Users", callback_data="admin:users")],
            [InlineKeyboardButton("🧹 ការសម្អាត", callback_data="admin:cleanup")],
        ]
    )


def cancel_only_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ បោះបង់", callback_data="cancel")]]
    )
