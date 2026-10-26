"""
Automatic action menu:

  * any incoming media (video / audio / document / voice) or any message
    containing a link instantly gets an inline menu — no slash commands needed
  * the buttons route into the exact same generation functions as the commands
"""

import re

from pyrogram import Client, filters
from pyrogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton)

from TelegramBot.config import SUDO_USERID
from TelegramBot.helpers.filters import authorized
from TelegramBot.helpers.media_utils import GDRIVE_REGEX, URL_REGEX, get_media
from TelegramBot.plugins.mediainfo import (
    telegram_mediainfo, ddl_mediainfo, gdrive_mediainfo)
from TelegramBot.plugins.screenshot import (
    telegram_screenshot, ddl_screenshot, gdrive_screenshot)
from TelegramBot.plugins.videosample import (
    telegram_videosample, ddl_videosample, gdrive_videosample)
from TelegramBot.plugins.audiospek import spek_telegram, spek_link
from TelegramBot.logging import LOGGER

logger = LOGGER(__name__)

MENU_TEXT = (
    "⚡ **What should I do with this?**\n\n"
    "All actions work on partial downloads — the whole file is never fetched."
)

MENU_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("🎬 Mediainfo", callback_data="menu|mi"),
        InlineKeyboardButton("📄 Raw info", callback_data="menu|mir"),
    ],
    [
        InlineKeyboardButton("📸 Screenshots", callback_data="menu|ss"),
        InlineKeyboardButton("🎵 Spectrogram", callback_data="menu|spek"),
    ],
    [InlineKeyboardButton("✂️ Video sample", callback_data="menu|sample")],
])

SAMPLE_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("1 min", callback_data="menu|sample_1"),
        InlineKeyboardButton("3 min", callback_data="menu|sample_3"),
        InlineKeyboardButton("5 min", callback_data="menu|sample_5"),
    ],
    [InlineKeyboardButton("⬅️ Back", callback_data="menu|back")],
])


def media_or_link(_, __, message: Message) -> bool:
    """Match plain media files and messages containing links (never commands)."""
    if not authorized(_, __, message):
        return False

    text = message.text or message.caption or ""
    if text.strip()[:1] in ("/", "!", "."):
        return False  # leave slash commands to their own handlers

    if message.text:
        return bool(re.search(URL_REGEX, text))

    if message.video or message.audio or message.voice:
        return True
    if message.animation:
        return False
    if message.document:
        mime = (message.document.mime_type or "")
        file_name = (message.document.file_name or "").lower()
        if mime.startswith(("video/", "audio/")):
            return True
        if file_name.endswith((
            ".mkv", ".mp4", ".avi", ".mov", ".flv", ".wmv", ".webm", ".m4v",
            ".ts", ".m2ts", ".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg",
            ".opus", ".wma", ".mka",
        )):
            return True
    return False


auto_trigger = filters.create(media_or_link)


@Client.on_message(filters.incoming & auto_trigger, group=1)
async def send_action_menu(client: Client, message: Message):
    """Instantly show the action menu for any media file or link."""
    try:
        await message.reply_text(
            MENU_TEXT,
            quote=True,
            disable_web_page_preview=True,
            reply_markup=MENU_KEYBOARD,
        )
    except Exception as error:
        logger.error(f"could not send the action menu: {error}")


def _extract_url(text: str):
    """Return (url, is_gdrive) from a text message, or (None, False)."""
    if match := re.search(GDRIVE_REGEX, text or ""):
        return match.group(0), True
    if match := re.search(URL_REGEX, text or ""):
        return match.group(0), False
    return None, False


@Client.on_callback_query(filters.regex(r"^menu\|"))
async def menu_callbacks(client: Client, callback_query: CallbackQuery):
    original = callback_query.message.reply_to_message
    if original is None:
        return await callback_query.answer(
            "The original message is gone, please send the file again.", show_alert=True)

    # only the sender (or a sudo user) may use this menu
    if (
        original.from_user
        and callback_query.from_user.id != original.from_user.id
        and callback_query.from_user.id not in SUDO_USERID
    ):
        return await callback_query.answer(
            "This menu was not initiated by you.", show_alert=True)

    action = callback_query.data.split("|", 1)[1]
    text = original.text or original.caption or ""
    url, is_gdrive = _extract_url(text)
    has_media = get_media(original) is not None

    await callback_query.answer()

    async def close_menu():
        try:
            await callback_query.message.delete()
        except Exception:
            pass

    try:
        if action == "back":
            return await callback_query.message.edit(
                MENU_TEXT, reply_markup=MENU_KEYBOARD, disable_web_page_preview=True)

        if action == "sample":
            if not (has_media or url):
                return await callback_query.answer(
                    "No media or link found in the original message.", show_alert=True)
            return await callback_query.message.edit(
                "⏱ Choose the duration of the sample video:",
                reply_markup=SAMPLE_KEYBOARD,
                disable_web_page_preview=True)

        if action.startswith("sample_"):
            if not (has_media or url):
                return await callback_query.answer(
                    "No media or link found in the original message.", show_alert=True)
            duration = int(action.split("_")[-1])
            await close_menu()
            if has_media:
                return await telegram_videosample(client, original, duration)
            if is_gdrive:
                return await gdrive_videosample(original, url, duration)
            return await ddl_videosample(original, url, duration)

        if not (has_media or url):
            return await callback_query.answer(
                "No media or link found in the original message.", show_alert=True)

        if action in ("mi", "mir"):
            is_raw = action == "mir"
            await close_menu()
            if has_media:
                return await telegram_mediainfo(client, original, isRaw=is_raw)
            if is_gdrive:
                return await gdrive_mediainfo(original, url, isRaw=is_raw)
            return await ddl_mediainfo(original, url, isRaw=is_raw)

        if action == "ss":
            await close_menu()
            if has_media:
                return await telegram_screenshot(client, original, frame_count=10)
            if is_gdrive:
                return await gdrive_screenshot(original, url, None, 10, 5, False, False)
            return await ddl_screenshot(original, url, None, 10, 5, False, False)

        if action == "spek":
            await close_menu()
            if has_media:
                return await spek_telegram(client, original)
            return await spek_link(original, url)

        await callback_query.answer("Unknown action.", show_alert=True)

    except Exception as error:
        logger.error(f"menu action '{action}' failed: {error}")
        try:
            await callback_query.message.reply_text(
                "❌ Something went wrong while processing this action.", quote=True)
        except Exception:
            pass
