"""
Spectrogram generation in the spek 0.8.5 / SoX style (dark background, SoX
heat palette, kHz + time axes, file name and track info).

  * audio files  -> one spectrogram of the FULL audio
  * video files  -> one spectrogram for EVERY audio track (full audio)
  * direct links -> whole file when it is small/medium, otherwise the first
                    part that fits the download budget

Audio files are downloaded in full; videos too (the spectrogram is only useful
when it covers the whole track). Nothing is analysed partially except very
large remote links.
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
    spek_track_description, SPEK_VERSION,
    send_spectrogram_album, cleanup_paths, safe_edit)
from TelegramBot.logging import LOGGER

logger = LOGGER(__name__)

MAX_TRACKS = 10                # Telegram albums cap at 10 photos anyway
LINK_FULL_BELOW_MB = 300       # links below this size are downloaded whole
LINK_STEPS = (300, 900)        # links above it: analyse this much audio


async def _render_all_tracks(
    message, reply_msg, path, mi_json, fp_json, display_name,
    max_seconds=None, note="",
):
    """
    Extract every audio track as mono PCM and render a spek-style spectrogram
    of the whole track for each.
    """
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
        track_desc = spek_track_description(
            track, stream_number=position, stream_total=total)

        # the extracted mono WAV may be shorter than the track when the file
        # was only partially downloaded; its own length is the honest axis end
        axis_seconds = _wav_duration(wav_path)

        if await build_spectrogram(wav_path, png_path, title=title,
                                   desc=track_desc, version=SPEK_VERSION,
                                   duration=axis_seconds):
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


def _wav_duration(wav_path):
    """Length of a (mono PCM) WAV file in seconds, or None."""
    import wave

    try:
        with wave.open(wav_path, "rb") as handle:
            rate = handle.getframerate()
            if rate:
                return handle.getnframes() / float(rate)
    except (wave.Error, OSError, ValueError):
        pass
    return None


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
        # audio and video files are downloaded in full: the spectrogram is
        # only useful when it covers the whole audio track
        path, mi_json, fp_json, downloaded = await telegram_head_download(
            client, media_message, reply_msg, display_name, full=True)

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
    """Spectrograms from a direct link (whole file when it is not huge)."""
    reply_msg = await message.reply_text(
        "🎵 **Generating spectrogram(s)**, please wait ...", quote=True)
    path = None
    png_paths = []
    try:
        filename = unquote(url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]) or "media"
        display_name = filename[:100]

        path, mi_json, fp_json, total_size = await http_head_download(
            url, reply_msg, display_name, steps=LINK_STEPS,
            full_below_mb=LINK_FULL_BELOW_MB)

        duration = get_duration_seconds(mi_json, fp_json)
        covered = covered_duration(duration, os.path.getsize(path), total_size)

        # the time axis always shows the real track duration, so when only part
        # of a large remote file was fetched, which part is covered is noted
        note = ""
        if covered and duration and covered < duration:
            note = (f"⚡ *Partial download — the first "
                    f"{get_readable_time(covered).strip()} of "
                    f"{get_readable_time(duration).strip()} are shown.*")

        items = await _render_all_tracks(
            message, reply_msg, path, mi_json, fp_json, display_name, note=note)
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
        "**Generates audio spectrograms (spek 0.8.5 / SoX style).**\n\n"
        "• Reply to an **audio** or **video** file → spectrogram of the "
        "**whole** audio track, one per audio track\n"
        "• Or pass a link: `/spek https://example.com/video.mkv` "
        "(downloaded fully unless the file is huge)\n\n"
        "The time axis is marked every 5 minutes and ends at the real track "
        "length (a 2 h 43 min track ends at `163:00`)."
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
