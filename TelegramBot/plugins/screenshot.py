"""
Screenshot generation from Google Drive links, direct download links and
Telegram files. Telegram files are partially downloaded (head only) and
screenshots are taken from the downloaded range.
"""

import os
import re
import shutil
import random
import datetime
import traceback
from urllib.parse import unquote

from pyrogram.types import InputMediaPhoto, Message
from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified

from TelegramBot.helpers.functions import randstr, makedir, check_and_convert_time
from TelegramBot.helpers.media_utils import (
    GDRIVE_REGEX, URL_REGEX, run_process, get_media, get_duration_seconds,
    covered_duration, telegram_head_download, cleanup_paths, safe_edit)
from TelegramBot.helpers.gdrivehelper import GoogleDriveHelper
from TelegramBot.helpers.filters import check_auth
from TelegramBot.logging import LOGGER

logger = LOGGER(__name__)

MAX_FRAME_RETRIES = 3


async def slowpics_collection(message, file_name, path):
    """Send the generated screenshots to Telegram (albums of 10)."""
    msg = await message.reply_text(
        "📤 Uploading generated screenshots to Telegram ...", quote=True)

    media = []
    for img in sorted(os.listdir(path)):
        img_path = os.path.join(path, img)
        if os.path.isfile(img_path) and os.path.getsize(img_path) > 0:
            media.append(InputMediaPhoto(media=img_path))

    if not media:
        return await safe_edit(msg, "❌ No screenshots were generated.")

    for start in range(0, len(media), 10):
        group = media[start:start + 10]
        if len(group) == 1:
            await message.reply_photo(photo=group[0].media, quote=True)
        else:
            await message.reply_media_group(media=group, quote=True)

    await safe_edit(
        msg,
        f"✅ Successfully generated **{len(media)}** screenshots for `{unquote(file_name)}`")


async def generate_ss_from_file(
    message, replymsg, file_path, file_name, frame_count, file_duration, hdr=False
):
    """Generate screenshots from a (partially) downloaded file with ffmpeg."""
    await safe_edit(
        replymsg,
        f"📸 Generating **{frame_count}** screenshots from "
        f"`{unquote(file_name)}`, please wait ...")

    rand_str = randstr()
    download_path = f"download/{rand_str}"
    makedir(download_path)

    # Telegram files are SDR; tonemapping is only applied when --hdr is passed
    vf_flags = (
        "zscale=transfer=linear,tonemap=tonemap=hable:param=1.0:desat=0:"
        "peak=10,zscale=transfer=bt709"
        if hdr else "null"
    )

    # keep every random timestamp safely inside the downloaded range
    safe_duration = max(file_duration - 2, 1)

    for frame in range(1, frame_count + 1):
        timestamp = str(datetime.timedelta(
            seconds=int(random.uniform(1, safe_duration))))
        output_path = f"{download_path}/{str(frame).zfill(2)}.png"

        for attempt in range(MAX_FRAME_RETRIES + 1):
            _, output = await run_process(
                ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                 "-ss", timestamp, "-i", file_path,
                 "-vf", f"{vf_flags},format=rgb24",
                 "-frames:v", "1", output_path],
                timeout_seconds=120)
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                break
            logger.warning(f"frame {frame} attempt {attempt} failed: {output[:200]}")

    generated = [f for f in sorted(os.listdir(download_path)) if f.endswith(".png")]
    if not generated:
        shutil.rmtree(download_path, ignore_errors=True)
        return await safe_edit(
            replymsg, "❌ Could not generate screenshots from this file.")

    try:
        await replymsg.delete()
    except Exception:
        pass

    await slowpics_collection(message, file_name, path=f"{os.getcwd()}/{download_path}")
    shutil.rmtree(download_path, ignore_errors=True)


async def generate_ss_from_link(
    message, replymsg, file_url, headers, file_name, frame_count, fps, hdr, timestamp
):
    """Generate screenshots straight from a remote URL with ffmpeg."""
    await safe_edit(
        replymsg,
        f"📸 Generating **{frame_count}** screenshots from "
        f"`{unquote(file_name)}`, please wait ...")

    rand_str = randstr()
    download_path = f"download/{rand_str}"
    makedir(download_path)

    vf_flags = (
        "zscale=transfer=linear,tonemap=tonemap=hable:param=1.0:desat=0:"
        "peak=10,zscale=transfer=bt709"
        if hdr else "null"
    )

    _, output = await run_process(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-user_agent", "Mozilla/5.0",
         "-headers", headers + "\r\n",
         "-y",
         "-ss", str(timestamp),
         "-i", file_url,
         "-vf", f"{vf_flags},format=rgb24",
         "-frames:v", str(frame_count),
         f"{download_path}/%02d.png"],
        timeout_seconds=1800)
    if output:
        logger.info(f"ffmpeg screenshot output: {output[:500]}")

    generated = [f for f in sorted(os.listdir(download_path)) if f.endswith(".png")]
    if not generated:
        shutil.rmtree(download_path, ignore_errors=True)
        return await safe_edit(
            replymsg, "❌ Could not generate screenshots from this link.")

    try:
        await replymsg.delete()
    except Exception:
        pass

    await slowpics_collection(message, file_name, path=f"{os.getcwd()}/{download_path}")
    shutil.rmtree(download_path, ignore_errors=True)


async def gdrive_screenshot(message, url, time, frame_count, fps, hdr, dv):
    """Generate screenshots from a Google Drive link."""
    replymsg = await message.reply_text(
        "🔎 Checking your Google Drive link ...", quote=True)
    try:
        drive = GoogleDriveHelper()
        metadata = drive.get_metadata(url)
        file_name = metadata.get("name") or "gdrive_video"

        if "video" not in (metadata.get("mimeType") or ""):
            return await safe_edit(
                replymsg, "❌ Can only generate screenshots from a video file.")

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

        # random timestamp between 15-20% of the movie, custom --time wins
        timestamp = total_duration * (random.uniform(15, 20) / 100)
        custom_timestamp = check_and_convert_time(time)
        if custom_timestamp and custom_timestamp < total_duration:
            timestamp = custom_timestamp
        timestamp = str(datetime.timedelta(seconds=int(timestamp)))

        await generate_ss_from_link(
            message, replymsg, file_url, headers, file_name,
            frame_count, fps, hdr, timestamp)

    except MessageNotModified:
        pass
    except Exception as error:
        logger.error(f"gdrive screenshot failed: {error} ({url})")
        await safe_edit(
            replymsg,
            "❌ Something went wrong while processing the Google Drive link. "
            "Make sure it is public and not rate limited.")


async def ddl_screenshot(message, url, time, frame_count, fps, hdr, dv):
    """Generate screenshots from a direct download link."""
    replymsg = await message.reply_text(
        "🔎 Checking direct download url ...", quote=True)
    try:
        file_name = unquote(url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]) or "video"
        if len(file_name) > 60:
            file_name = file_name[-60:]

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

        timestamp = total_duration * (random.uniform(15, 20) / 100)
        custom_timestamp = check_and_convert_time(time)
        if custom_timestamp and custom_timestamp < total_duration:
            timestamp = custom_timestamp
        timestamp = str(datetime.timedelta(seconds=int(timestamp)))

        await generate_ss_from_link(
            message, replymsg, url, headers, file_name,
            frame_count, fps, hdr, timestamp)

    except MessageNotModified:
        pass
    except Exception as error:
        logger.error(f"ddl screenshot failed: {error} ({url})")
        await safe_edit(
            replymsg, "❌ Something went wrong while generating screenshots "
            "from the given link.")


async def telegram_screenshot(client, media_message, frame_count=10):
    """Generate screenshots from a Telegram video (partial download)."""
    media = get_media(media_message)
    if media is None:
        return await media_message.reply_text(
            "Reply to a video file to generate screenshots.", quote=True)

    mime = (getattr(media, "mime_type", "") or "")
    if media_message.document and "video" not in mime:
        return await media_message.reply_text(
            "Can only generate screenshots from a video file.", quote=True)

    file_name = (getattr(media, "file_name", None)
                 or f"telegram_video_{media_message.id}")[:100]

    replymsg = await media_message.reply_text(
        "📸 Preparing screenshots, please wait ...", quote=True)
    path = None
    try:
        path, mi_json, fp_json, downloaded = await telegram_head_download(
            client, media_message, replymsg, file_name,
            steps=(75, 150), full_below_mb=200)

        total_duration = get_duration_seconds(mi_json, fp_json)
        if not total_duration:
            return await safe_edit(
                replymsg,
                "❌ Could not read the video duration from the downloaded part.")

        file_size = getattr(media, "file_size", 0) or 0
        partial_duration = (
            covered_duration(total_duration, downloaded, file_size) or total_duration)

        await generate_ss_from_file(
            media_message, replymsg, path, file_name,
            frame_count, partial_duration)

    except MessageNotModified:
        pass
    except Exception as error:
        traceback.print_exc()
        logger.error(f"telegram screenshot failed: {error}")
        await safe_edit(
            replymsg,
            "❌ Something went wrong while generating screenshots from this Telegram file.")
    finally:
        cleanup_paths(path)


screenshot_help = """Generates screenshots from Google Drive links, Telegram files, or direct download links.

**➜ Command**

`/ss` or `/screenshot` [GDrive Link] or [DDL Link] or [Reply to Telegram file]

**➜ Additional flags**

`--count=10` — number of screenshots (default 10, max 20)
`--fps=10` — seconds between two screenshots (default 5, max 15)
`--time=01:20:10` — timestamp to start from (HH:MM:SS)
`--hdr` — for HDR videos

Only `--count` works for Telegram files.

**Example:** `/ss https://videolink.mkv --count=10 --fps=1 --hdr --time=00:20:00`"""


@Client.on_message(filters.command(["screenshot", "ss"]) & check_auth)
async def screenshot(client: Client, message: Message):
    """Generate screenshots from ddl, gdrive or Telegram files."""

    if message.reply_to_message:
        frame_count = 10
        if len(message.command) > 1:
            user_input = message.text.split(None, 1)[1]
            match = re.search(r"(?:--|-)count=(\d+)", user_input)
            if match:
                frame_count = min(int(match[1]), 20)
        return await telegram_screenshot(client, message.reply_to_message, frame_count)

    if len(message.command) < 2:
        return await message.reply_text(screenshot_help, quote=True)

    user_input = message.text.split(None, 1)[1]

    match = re.search(r"(?:--|-)fps=(\d+)", user_input)
    fps = min(int(match[1]), 15) if match else 5

    match = re.search(r"(?:--|-)count=(\d+)", user_input)
    frame_count = min(int(match[1]), 20) if match else 10

    match = re.search(r"--time=(\d{2}:\d{2}:\d{2})", user_input)
    time = match.group(1) if match else None

    hdr = bool(re.search(r"(?:--|-)hdr\b", user_input, re.IGNORECASE))
    dv = bool(re.search(r"(?:--|-)dv\b", user_input, re.IGNORECASE))

    if url_match := re.search(GDRIVE_REGEX, user_input):
        return await gdrive_screenshot(
            message, url_match.group(0), time, frame_count, fps, hdr, dv)

    if url_match := re.search(URL_REGEX, user_input):
        return await ddl_screenshot(
            message, url_match.group(0), time, frame_count, fps, hdr, dv)

    return await message.reply_text("This type of link is not supported.", quote=True)
