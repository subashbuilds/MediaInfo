"""
Spectrogram generation in the classic SoX style (dark background, vivid
green→yellow→red heat colours, kHz + time axes, title and track info).

  * audio files  -> one spectrogram of the full audio
  * video files  -> one spectrogram for EVERY audio track
  * direct links -> partial download, first minutes of audio analysed

Nothing is downloaded in full unless the file is a small audio file.
"""

import os
import re
import traceback
from urllib.parse import unquote

from pyrogram import Client, filters
from pyrogram.types import Message

from TelegramBot.helpers.filters import check_auth
from TelegramBot.helpers.functions import randstr, get_readable_time
from TelegramBot.helpers.media_utils import (
    URL_REGEX, get_media, get_duration_seconds, covered_duration,
    telegram_head_download, http_head_download, list_audio_tracks,
    describe_audio_track, extract_audio_track, build_spectrogram,
    send_spectrogram_album, cleanup_paths, safe_edit)
from TelegramBot.logging import LOGGER

logger = LOGGER(__name__)

MAX_TRACKS = 10                # Telegram albums cap at 10 photos anyway
LINK_ANALYSIS_SECONDS = 600    # for links only analyse the first 10 minutes


async def _render_all_tracks(
    message, reply_msg, path, mi_json, fp_json, display_name,
    max_seconds=None, note="",
):
    """Extract every audio track and render a SoX spectrogram for each."""
    tracks = list_audio_tracks(fp_json, mi_json)
    if not tracks:
        await safe_edit(reply_msg, "❌ No audio track found in this file.")
        return []

    tracks = tracks[:MAX_TRACKS]
    total = len(tracks)
    items = []

    for track in tracks:
        position = track["position"]
        description = describe_audio_track(track)

        await safe_edit(
            reply_msg, f"🎵 **Extracting audio track {position}/{total}** ...")

        wav_path = os.path.join("download", f"{randstr()}_track{position}.wav")
        if not await extract_audio_track(path, track["stream_index"], wav_path,
                                         max_seconds=max_seconds):
            logger.error(f"could not extract audio track {position} of {display_name}")
            continue

        await safe_edit(
            reply_msg, f"📊 **Rendering spectrogram {position}/{total}** ...")

        png_path = os.path.join("download", f"{randstr()}_track{position}.png")
        title = f"{display_name[:60]} — Audio #{position}"

        if await build_spectrogram(wav_path, png_path, title=title, comment=description):
            caption = (
                f"🎵 **Audio #{position}/{total}** — {description}\n"
                f"**File:** `{display_name}`"
            )
            if note:
                caption += f"\n{note}"
            items.append((png_path, caption))

        try:
            os.remove(wav_path)
        except OSError:
            pass

    return items


async def spek_telegram(client, media_message):
    """Spectrograms from a Telegram audio/video file."""
    media = get_media(media_message)
    if media is None:
        return await media_message.reply_text(
            "Reply to an audio or video file to generate spectrograms.", quote=True)

    display_name = (getattr(media, "file_name", None)
                    or f"telegram_audio_{media_message.id}")[:100]

    reply_msg = await media_message.reply_text(
        "🎵 **Generating spectrogram(s)**, please wait ...", quote=True)
    path = None
    png_paths = []
    try:
        # audio files are small -> downloaded fully; videos are streamed
        # partially (head only) and every audio track inside is rendered
        path, mi_json, fp_json, downloaded = await telegram_head_download(
            client, media_message, reply_msg, display_name, full_below_mb=100)

        items = await _render_all_tracks(
            media_message, reply_msg, path, mi_json, fp_json, display_name)
        if not items:
            return  # status message already explains the failure

        png_paths = [png for png, _ in items]
        try:
            await reply_msg.delete()
        except Exception:
            pass
        await send_spectrogram_album(media_message, items)

    except Exception as error:
        traceback.print_exc()
        logger.error(f"spek (telegram) failed: {error}")
        await safe_edit(reply_msg,
                        "❌ Something went wrong while generating the spectrogram.")
    finally:
        cleanup_paths(path, *png_paths)


async def spek_link(message, url):
    """Spectrograms from a direct link (partial download, first minutes)."""
    reply_msg = await message.reply_text(
        "🎵 **Generating spectrogram(s)**, please wait ...", quote=True)
    path = None
    png_paths = []
    try:
        filename = unquote(url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]) or "media"
        display_name = filename[:100]

        path, mi_json, fp_json, total_size = await http_head_download(
            url, reply_msg, display_name, steps=(30, 120))

        duration = get_duration_seconds(mi_json, fp_json)
        covered = covered_duration(duration, os.path.getsize(path), total_size)

        max_seconds = LINK_ANALYSIS_SECONDS
        note = "⚡ *Analysed the first 10 minutes (partial download).*"
        if covered and covered < LINK_ANALYSIS_SECONDS:
            note = (f"⚡ *Partial download — covers roughly the first "
                    f"{get_readable_time(covered).strip()}.*")

        items = await _render_all_tracks(
            message, reply_msg, path, mi_json, fp_json, display_name,
            max_seconds=max_seconds, note=note)
        if not items:
            return

        png_paths = [png for png, _ in items]
        try:
            await reply_msg.delete()
        except Exception:
            pass
        await send_spectrogram_album(message, items)

    except Exception as error:
        traceback.print_exc()
        logger.error(f"spek (link) failed: {error}")
        await safe_edit(reply_msg,
                        "❌ Something went wrong while generating the spectrogram "
                        "from the given link.")
    finally:
        cleanup_paths(path, *png_paths)


@Client.on_message(filters.command(["spek", "sox"]) & check_auth)
async def generate_spek(client, message: Message):
    usage = (
        "**Generates audio spectrograms (classic SoX style).**\n\n"
        "• Reply to an **audio** file → spectrogram of the full audio\n"
        "• Reply to a **video** file → one spectrogram for **each** audio track\n"
        "• Or pass a link: `/spek https://example.com/video.mkv` "
        "(first 10 minutes analysed, whole file is never downloaded)"
    )

    if message.reply_to_message:
        if get_media(message.reply_to_message) is None:
            return await message.reply_text(
                "Reply to an audio or video file to generate spectrograms.", quote=True)
        return await spek_telegram(client, message.reply_to_message)

    user_input = message.text.split(None, 1)[1] if len(message.command) > 1 else ""
    if user_input:
        if url_match := re.search(URL_REGEX, user_input):
            return await spek_link(message, url_match.group(0))

    return await message.reply_text(usage, quote=True)
