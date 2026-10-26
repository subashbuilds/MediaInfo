"""
Video sample (trim) generation from Google Drive links, direct download
links and Telegram files.
"""

import os
import re
import random
import datetime
import traceback
from urllib.parse import unquote

import requests

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified

from TelegramBot.helpers.functions import randstr, check_and_convert_time
from TelegramBot.helpers.media_utils import (
    GDRIVE_REGEX, URL_REGEX, run_process, get_media, get_duration_seconds,
    telegram_head_download, probe_url_duration, cleanup_paths, safe_edit)
from TelegramBot.helpers.gdrivehelper import GoogleDriveHelper
from TelegramBot.helpers.filters import check_auth
from TelegramBot.logging import LOGGER

logger = LOGGER(__name__)

TELEGRAM_UPLOAD_LIMIT = 1_900_000_000  # stay below the 2 GB bot upload limit

# thumbnail for uploaded samples (fetched once at startup, non fatal on failure)
thumb_path = "thumb.jpg"
try:
    _thumb = requests.get(
        "https://te.legra.ph/file/508f1cd599bb3d9352e88.jpg", timeout=15)
    if _thumb.status_code == 200 and _thumb.content:
        with open(thumb_path, "wb") as _file:
            _file.write(_thumb.content)
    else:
        thumb_path = None
except Exception as error:
    logger.warning(f"could not fetch sample thumbnail: {error}")
    thumb_path = None


def _thumb_or_none():
    return thumb_path if thumb_path and os.path.exists(thumb_path) else None


async def generate_sample_from_link(
    message, replymsg, file_url, duration, headers, filename, timestamp
):
    """Cut a sample straight from a remote URL with ffmpeg (streaming copy)."""
    await safe_edit(
        replymsg,
        f"✂️ Generating **{duration} min** sample from "
        f"`{unquote(filename)}`, please wait ...")

    rand_str = randstr()
    output_path = f"download/{rand_str}_{filename.replace(' ', '.')}"

    _, output = await run_process(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-user_agent", "Mozilla/5.0",
         "-headers", headers + "\r\n",
         "-y",
         "-ss", str(timestamp),
         "-i", file_url,
         "-t", str(datetime.timedelta(minutes=duration)),
         "-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-map", "0:d?",
         "-c", "copy",
         output_path],
        timeout_seconds=1800)
    if output:
        logger.info(f"sample ffmpeg output: {output[:500]}")

    if not os.path.exists(output_path) or os.path.getsize(output_path) < 1024:
        cleanup_paths(output_path)
        return await safe_edit(replymsg, "❌ FFmpeg failed to create the sample video.")

    if os.path.getsize(output_path) > TELEGRAM_UPLOAD_LIMIT:
        cleanup_paths(output_path)
        return await safe_edit(
            replymsg,
            "❌ Sample file is larger than 2 GB — it exceeds Telegram's upload limit.")

    await safe_edit(replymsg, "📤 Uploading the video file, please wait ...")
    await message.reply_video(
        video=output_path,
        caption=f"[**{duration}min Sample**] {filename}",
        thumb=_thumb_or_none(),
        quote=True)

    try:
        await replymsg.delete()
    except Exception:
        pass
    cleanup_paths(output_path)


async def gdrive_videosample(message, url, duration):
    """Generate a video sample from a Google Drive link."""
    replymsg = await message.reply_text(
        "🔎 Checking your Google Drive link ...", quote=True)
    try:
        drive = GoogleDriveHelper()
        metadata = drive.get_metadata(url)
        filename = metadata.get("name") or "gdrive_video"

        if "video" not in (metadata.get("mimeType") or ""):
            return await safe_edit(
                replymsg, "❌ Can only generate samples from a video file.")

        file_url = (
            "https://www.googleapis.com/drive/v3/files/"
            f"{drive.get_id(url)}?supportsAllDrives=true&alt=media"
        )
        headers = f"Authorization: Bearer {drive.get_bearer_token()}"

        total_duration = await probe_url_duration(file_url, headers)
        if not total_duration:
            return await safe_edit(
                replymsg,
                "❌ Could not read the video duration. Make sure the link is "
                "public, is a proper video and is not rate limited.")

        timestamp = str(datetime.timedelta(
            seconds=int(total_duration * (random.uniform(15, 20) / 100))))

        await generate_sample_from_link(
            message, replymsg, file_url, duration, headers, filename, timestamp)

    except MessageNotModified:
        pass
    except Exception as error:
        logger.error(f"gdrive videosample failed: {error}")
        await safe_edit(
            replymsg,
            "❌ Something went wrong while processing the Google Drive link. "
            "Make sure the link is public and is a proper video file.")


async def ddl_videosample(message, url, duration):
    """Generate a video sample from a direct download link."""
    replymsg = await message.reply_text(
        "🔎 Checking direct download url ...", quote=True)
    try:
        filename = unquote(url.split("?")[0].rstrip("/").rsplit("/", 1)[-1])
        if not filename:
            filename = "sample.mkv"
        if "." not in filename:
            filename += ".mkv"
        if len(filename) > 60:
            filename = filename[-60:]

        headers = (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        total_duration = await probe_url_duration(url, headers)
        if not total_duration:
            return await safe_edit(
                replymsg,
                "❌ Could not read the video duration from this link. "
                "Make sure it points to a proper video file.")

        timestamp = str(datetime.timedelta(
            seconds=int(total_duration * (random.uniform(15, 20) / 100))))

        await generate_sample_from_link(
            message, replymsg, url, duration, headers, filename, timestamp)

    except MessageNotModified:
        pass
    except Exception as error:
        logger.error(f"ddl videosample failed: {error}")
        await safe_edit(
            replymsg, "❌ Something went wrong while generating the sample "
            "from the given link.")


async def telegram_videosample(client, media_message, duration):
    """Generate a video sample from a Telegram file (partial download)."""
    media = get_media(media_message)
    if media is None:
        return await media_message.reply_text(
            "Reply to a video file to generate a sample.", quote=True)

    mime = (getattr(media, "mime_type", "") or "")
    if media_message.document and "video" not in mime:
        return await media_message.reply_text(
            "Can only generate samples from a video file.", quote=True)

    filename = (getattr(media, "file_name", None)
                or f"telegram_video_{media_message.id}")[:100]
    file_size = getattr(media, "file_size", 0) or 0

    replymsg = await media_message.reply_text(
        f"✂️ Generating **{duration} min** sample from `{filename}`, "
        "please wait ...", quote=True)
    download_path = output_path = None
    try:
        # 1) download a small head to measure duration / bitrate
        probe_path, mi_json, fp_json, _ = await telegram_head_download(
            client, media_message, replymsg, filename, steps=(25, 75))
        cleanup_paths(probe_path)

        total_duration = get_duration_seconds(mi_json, fp_json)
        if not total_duration:
            return await safe_edit(
                replymsg,
                "❌ Could not read the video duration from the downloaded part.")

        # 2) download only the bytes the sample needs (never the whole file)
        bytes_per_second = (file_size / total_duration) if file_size else 0
        needed_mb = int((bytes_per_second * duration * 60) / (1024 * 1024)) + 5
        download_mb = max(min(needed_mb, 2000), 25)

        rand_str = randstr()
        download_path = f"download/{rand_str}_{filename}"
        output_path = f"download/{rand_str}_sample_{filename}"

        await safe_edit(
            replymsg, f"⬇️ Downloading the sample part (~{download_mb} MB) ...")
        with open(download_path, "wb") as file:
            async for chunk in client.stream_media(media_message, limit=download_mb):
                file.write(chunk)

        # 3) remux the requested duration without re-encoding
        await run_process(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-i", download_path,
             "-t", str(datetime.timedelta(minutes=duration)),
             "-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-map", "0:d?",
             "-c", "copy",
             output_path],
            timeout_seconds=1800)

        if not os.path.exists(output_path) or os.path.getsize(output_path) < 1024:
            return await safe_edit(
                replymsg, "❌ FFmpeg failed to create the sample video.")

        if os.path.getsize(output_path) > TELEGRAM_UPLOAD_LIMIT:
            return await safe_edit(
                replymsg,
                "❌ Sample file is larger than 2 GB — it exceeds Telegram's upload limit.")

        await safe_edit(replymsg, "📤 Uploading the video file, please wait ...")
        await media_message.reply_video(
            video=output_path,
            caption=f"[**{duration}min Sample**] {filename}",
            thumb=_thumb_or_none(),
            quote=True)

        try:
            await replymsg.delete()
        except Exception:
            pass

    except Exception as error:
        traceback.print_exc()
        logger.error(f"telegram videosample failed: {error}")
        await safe_edit(
            replymsg,
            "❌ Something went wrong while generating the sample from this Telegram file.")
    finally:
        cleanup_paths(download_path, output_path)


sample_duration_buttons = [
    [
        InlineKeyboardButton("1 min", callback_data="videosample_1"),
        InlineKeyboardButton("3 min", callback_data="videosample_3"),
        InlineKeyboardButton("5 min", callback_data="videosample_5"),
    ]
]

info_dictionary: dict = {}


@Client.on_callback_query(filters.regex(r"^videosample_\d+$"))
async def videosample_duration(client, callback_query):
    duration = int(callback_query.data.split("_")[-1])

    original = callback_query.message.reply_to_message
    info = info_dictionary.pop(original.id, None) if original else None
    if not info:
        return await callback_query.answer(
            "This menu is too old, please run /sample again.", show_alert=True)

    try:
        await client.delete_messages(
            callback_query.message.chat.id, callback_query.message.id)
    except Exception:
        pass
    await callback_query.answer()

    if info["type"] == "gdrive":
        return await gdrive_videosample(info["message"], info["url"], duration)

    if info["type"] == "ddl":
        return await ddl_videosample(info["message"], info["url"], duration)

    return await telegram_videosample(
        info["client"], info["message"].reply_to_message, duration)


@Client.on_message(filters.command(["sample", "trim"]) & check_auth)
async def video_sample(client: Client, message: Message):
    """Generate a video sample from GDrive links, Telegram files or direct links."""

    picker = (
        "⏱ Choose the duration of the sample video:",
        InlineKeyboardMarkup(sample_duration_buttons),
    )

    if message.reply_to_message:
        if get_media(message.reply_to_message) is None:
            return await message.reply_text(
                "Reply to a video file to generate a sample.", quote=True)
        info_dictionary[message.id] = {
            "message": message, "client": client, "type": "telegram"}
        return await message.reply_text(
            picker[0], reply_markup=picker[1], quote=True)

    if len(message.command) < 2:
        return await message.reply_text(
            "**Generates a video sample from Google Drive links, Telegram files "
            "or direct download links.**\n\n"
            "Reply to a video with `/sample`, or pass a link: "
            "`/sample https://example.com/video.mkv`",
            quote=True)

    user_input = message.text.split(None, 1)[1]

    if url_match := re.search(GDRIVE_REGEX, user_input):
        info_dictionary[message.id] = {
            "message": message, "client": client,
            "type": "gdrive", "url": url_match.group(0)}
        return await message.reply_text(
            picker[0], reply_markup=picker[1], quote=True)

    if url_match := re.search(URL_REGEX, user_input):
        info_dictionary[message.id] = {
            "message": message, "client": client,
            "type": "ddl", "url": url_match.group(0)}
        return await message.reply_text(
            picker[0], reply_markup=picker[1], quote=True)

    return await message.reply_text("This type of link is not supported.", quote=True)
