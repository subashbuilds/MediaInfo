"""
Detailed Mediainfo generation from Telegram files, direct download links
and Google Drive links.

Everything works on progressive partial downloads (the whole file is never
fetched): the head of the file is downloaded in growing stages and re-parsed
after each stage, which is what makes Mediainfo succeed on the FIRST attempt.
"""

import os
import re
import httpx
import traceback
from urllib.parse import unquote, urljoin

from pyrogram import Client, filters
from pyrogram.types import Message

from TelegramBot.helpers.filters import check_auth
from TelegramBot.helpers.functions import (
    get_readable_bytes, get_readable_bitrate, remove_N)
from TelegramBot.helpers.media_utils import (
    URL_REGEX, GDRIVE_REGEX, run_process, get_media, get_duration_seconds,
    telegram_head_download, http_head_download, cleanup_paths, safe_edit)
from TelegramBot.helpers.mediainfo_paste import mediainfo_paste
from TelegramBot.helpers.gdrivehelper import GoogleDriveHelper
from TelegramBot.logging import LOGGER

logger = LOGGER(__name__)


def _format_duration(seconds):
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


# `mediainfo -f` prints one field more than once (raw value, human value,
# timecode...) and repeats a block of bookkeeping fields for every stream.
# A file with 38 subtitle tracks turns that into ~67 KB of text, past
# Telegraph's 64 KiB page limit (CONTENT_TOO_BIG), so the repeated values are
# collapsed and the bookkeeping fields dropped.
_MI_NOISE_FIELDS = frozenset((
    "Count", "Count of stream of this kind", "Kind of stream",
    "Stream identifier", "StreamOrder", "Unique ID",
    "Format/Url", "Format/Info", "Codec ID/Info",
    "Format/Extensions usually used", "Internet media type",
))


def _mi_field_name(line):
    """Field name of a `Name : value` line, or None for headers / blank lines."""
    name, separator, _ = line.partition(" : ")
    return name.strip() if separator else None


def _is_noise_field(name):
    return name in _MI_NOISE_FIELDS or name.endswith("_Source")


def clean_mediainfo_text(text):
    """
    Drop truncation noise, repeated values and blank-line runs.

    Fields printed under the same name for one stream are collapsed to their
    single most readable value (the longest one, which is the human readable
    form rather than the raw number), and purely internal fields are removed.
    """
    lines = []
    for line in text.splitlines():
        if "IsTruncated" in line or "FileExtension_Invalid" in line:
            continue
        lines.append(line.rstrip())

    remove_N(lines)

    collapsed = []
    index = 0
    while index < len(lines):
        name = _mi_field_name(lines[index])
        if name is None:
            collapsed.append(lines[index])
            index += 1
            continue

        end, best = index, lines[index]
        while end + 1 < len(lines) and _mi_field_name(lines[end + 1]) == name:
            end += 1
            if len(lines[end]) >= len(best):
                best = lines[end]

        if not _is_noise_field(name):
            collapsed.append(best)
        index = end + 1

    cleaned, previous_blank = [], False
    for line in collapsed:
        if not line:
            if previous_blank:
                continue
            previous_blank = True
        else:
            previous_blank = False
        cleaned.append(line)

    return "\n".join(cleaned).strip()


def _mi_field_label(line):
    """(prefix-before-colon, field label) of a `Name : value` line, or None."""
    position = line.find(":")
    if position < 0:
        return None
    prefix, label = line[:position], line[:position].strip()
    return (prefix, label) if label else None


def _mi_value_column(lines):
    """Character column `mediainfo` aligns its values at (blank lines skipped)."""
    for line in lines:
        parsed = _mi_field_label(line)
        if parsed:
            return len(parsed[0])
    return 41


def _set_mi_value(line, value):
    """Replace the value of a `Name : value` line, keeping its alignment."""
    prefix = line[:line.find(":")]
    return f"{prefix}: {value}"


async def build_mediainfo(path, mi_json, display_name, real_size=None, partial=False):
    """
    Run mediainfo in full-detail mode (`-f`, every known field) on the
    (partial) file and post-process the text:

      * restore the original file name (partial files get random names)
      * restore the real file size and real overall bitrate
      * give every video / audio / text track a stream size (estimated from its
        bitrate and marked `~` when only part of the file was read, or when
        MediaInfo reports no stream size at all)

    Returns (text, mi_json).
    """
    _, full_text = await run_process(["mediainfo", "-f", path])
    if not full_text.strip():
        _, full_text = await run_process(["mediainfo", path])

    duration = get_duration_seconds(mi_json, None)

    lines = full_text.splitlines()
    value_column = _mi_value_column(lines)
    size_prefix = "Stream size".ljust(value_column)
    sections = []        # (section_type, section_index, start_line, has_size)
    section_type = None
    section_index = 0
    for index, line in enumerate(lines):
        stripped = line.strip()

        if stripped and " : " not in line:
            # section header (General / Video / Audio #n / Text #n ...)
            header = re.match(r"^(Video|Audio|Text)(?:\s+#(\d+))?$", stripped)
            if header:
                section_type = header.group(1)
                section_index = (int(header.group(2)) - 1) if header.group(2) else 0
                sections.append([section_type, section_index, index, False])
            else:
                section_type = None
            continue

        if "Complete name" in line:
            lines[index] = _set_mi_value(line, display_name)

        elif "File size" in line and real_size:
            try:
                readable = get_readable_bytes(int(real_size))
            except (TypeError, ValueError):
                continue
            lines[index] = _set_mi_value(line, readable)

        elif (
            "Overall bit rate" in line
            and all(
                flag not in line
                for flag in ("mode", "Mode", "maximum", "Maximum",
                             "nominal", "Nominal", "minimum", "Minimum")
            )
            and real_size and duration
        ):
            try:
                bitrate = get_readable_bitrate((int(real_size) * 8) / (duration * 1000))
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            lines[index] = _set_mi_value(line, bitrate)

        elif stripped.startswith("Stream size") and partial and section_type in (
                "Video", "Audio", "Text") and duration:
            # only part of the file was read — MediaInfo's own stream size
            # covers just the seen part, so replace it with the estimate
            track = _mi_track(mi_json, section_type, section_index) or {}
            estimate = _estimate_stream_size(track, duration)
            if estimate:
                lines[index] = _set_mi_value(
                    line, "~" + get_readable_bytes(estimate))
            if sections:
                sections[-1][3] = True

        elif stripped.startswith("Stream size") and sections:
            sections[-1][3] = True

    # MediaInfo only prints a stream size for some containers, so a track
    # without one gets an estimate from its bitrate (marked `~`), inserted as
    # the first line under its header. Done last-first so earlier positions
    # of the list stay valid.
    if duration:
        for track_type, track_index, start, has_size in reversed(sections):
            if has_size:
                continue
            track = _mi_track(mi_json, track_type, track_index) or {}
            estimate = _estimate_stream_size(track, duration)
            if estimate:
                lines.insert(
                    start + 1, f"{size_prefix}: ~{get_readable_bytes(estimate)}")

    return clean_mediainfo_text("\n".join(lines)), mi_json


def _to_int(value):
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0


def _track_bitrate(track):
    """First available bitrate field (measured, then nominal / maximum)."""
    for key in ("BitRate", "BitRate_Nominal", "BitRate_Maximum"):
        value = track.get(key)
        if value:
            try:
                rate = float(str(value).replace(",", ""))
            except (TypeError, ValueError):
                continue
            if rate > 0:
                return rate
    return None


def _estimate_stream_size(track, duration):
    """Estimate a stream's size in bytes from its bitrate, or return None."""
    bit_rate = _track_bitrate(track)
    if not bit_rate or not duration:
        return None
    size = bit_rate * float(duration) / 8
    return int(size) if size > 0 else None


def _stream_size_text(track, duration, partial):
    """
    Human readable size of one stream. Exact when the whole file was read,
    estimated from the track bitrate (marked `~`) when only a part was read.
    """
    raw = track.get("StreamSize")
    if raw and not partial:
        try:
            size = int(str(raw).replace(",", ""))
            if size > 0:
                return get_readable_bytes(size)
        except (TypeError, ValueError):
            pass
    estimate = _estimate_stream_size(track, duration)
    return f"~{get_readable_bytes(estimate)}" if estimate else None


def _mi_track(mi_json, type_name, index):
    """The `index`-th MediaInfo track of the given type (Video/Audio/Text)."""
    same = [t for t in (mi_json or {}).get("media", {}).get("track", [])
            if t.get("@type") == type_name]
    return same[index] if index < len(same) else None


def build_summary(mi_json, display_name, real_size=None, partial=False):
    """Compact, human readable summary of the parsed MediaInfo JSON."""
    tracks = (mi_json or {}).get("media", {}).get("track", [])
    if not tracks:
        return None

    general = tracks[0]

    try:
        total_duration = float(general.get("Duration"))
    except (TypeError, ValueError):
        total_duration = None

    parts = []
    container = general.get("Format") or general.get("CodecID") or "unknown"
    parts.append(f"📦 `{container}`")

    try:
        parts.append(f"⏱ `{_format_duration(float(general['Duration']))}`")
    except (KeyError, TypeError, ValueError):
        pass

    size = real_size
    if not size:
        size = general.get("FileSize")
    if size:
        try:
            parts.append(f"💾 `{get_readable_bytes(int(size))}`")
        except (TypeError, ValueError):
            pass

    try:
        parts.append(f"⚡ `{get_readable_bitrate(float(general['OverallBitRate']) / 1000)}`")
    except (KeyError, TypeError, ValueError):
        pass

    summary = f"🎬 **{display_name}**\n\n" + " · ".join(parts)

    video_tracks = [t for t in tracks if t.get("@type") == "Video"]
    if video_tracks:
        rendered = []
        for track in video_tracks:
            bits = [track.get("Format") or "?"]
            if track.get("Width") and track.get("Height"):
                bits.append(f"{track['Width']}×{track['Height']}")
            if track.get("FrameRate"):
                bits.append(f"{track['FrameRate']} fps")
            hdr = (track.get("HDR_Format") or "").split("(")[0].strip()
            if hdr:
                bits.append(hdr)
            elif "Dolby Vision" in (track.get("Format_Profile") or ""):
                bits.append("Dolby Vision")
            size_text = _stream_size_text(track, total_duration, partial)
            if size_text:
                bits.append(size_text)
            rendered.append(" · ".join(bits))
        summary += f"\n🎥 **Video [{len(video_tracks)}]:** " + " | ".join(rendered)

    audio_tracks = [t for t in tracks if t.get("@type") == "Audio"]
    if audio_tracks:
        rendered = []
        for track in audio_tracks:
            bits = [track.get("Format") or "?"]
            bit_rate = _track_bitrate(track)
            if bit_rate:
                kbps = int(bit_rate / 1000)
                if kbps > 0:
                    mode = (track.get("BitRate_Mode") or "").strip().upper()
                    bits.append(f"{kbps} kbps" + (" VBR" if mode == "VBR" else ""))
            layout = (track.get("ChannelLayout") or "").split("(")[0].strip()
            if layout:
                bits.append(layout)
            elif track.get("Channel(s)"):
                bits.append(f"{track['Channel(s)']} ch")
            language = (track.get("Language") or "").strip()
            if language and language.lower() != "und":
                bits.append(f"[{language.upper()}]")
            title = (track.get("Title") or "").strip()
            if title:
                bits.append(f"“{title}”")
            size_text = _stream_size_text(track, total_duration, partial)
            if size_text:
                bits.append(size_text)
            rendered.append(" ".join(bits))
        summary += f"\n🔊 **Audio [{len(audio_tracks)}]:** " + " | ".join(rendered)

    text_tracks = [t for t in tracks if t.get("@type") == "Text"]
    if text_tracks:
        formats = sorted({(t.get("Format") or "?") for t in text_tracks})
        languages = sorted({
            (t.get("Language") or "").upper() for t in text_tracks
        } - {"", "UND"})
        line = f"💬 **Subtitles [{len(text_tracks)}]:** {', '.join(formats)}"
        if languages:
            line += f" ({', '.join(languages)})"
        if not partial:
            total_text_size = 0
            for track in text_tracks:
                try:
                    total_text_size += int(str(track.get("StreamSize")).replace(",", ""))
                except (TypeError, ValueError):
                    total_text_size = 0
                    break
            if total_text_size > 0:
                line += f" · {get_readable_bytes(total_text_size)} total"
        summary += line

    image_tracks = [t for t in tracks if t.get("@type") == "Image"]
    if image_tracks:
        summary += f"\n🖼 **Images [{len(image_tracks)}]**"

    return summary


async def _resolve_paste_url(url, timeout=15.0):
    """
    Follow a Telegraph page URL to the page it actually redirects to.

    Some shared links redirect (e.g. a telegra.ph path that is served from
    graph.org, or vice versa); posting the redirect target avoids a "404 not
    found" when the user opens the link. On any error the original URL is
    returned unchanged.
    """
    if not url:
        return url
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout),
            headers={"user-agent": "Mozilla/5.0"},
        ) as client:
            response = await client.get(url)
        if response.status_code == 200 and str(response.url) != url:
            return str(response.url)
    except (httpx.HTTPError, Exception):
        pass
    return url


async def _send_result(
    reply_msg, target_message, path, text, mi_json, display_name, real_size,
    isRaw, partial=False
):
    if not text:
        return await safe_edit(
            reply_msg,
            "❌ Could not read any media information from this file — "
            "it may be corrupted or in an unsupported format.")

    if isRaw:
        txt_path = f"{path}.txt"
        with open(txt_path, "w", encoding="utf-8") as file:
            file.write(text)
        await target_message.reply_document(
            txt_path, caption=f"**File Name :** `{display_name}`", quote=True)
        try:
            await reply_msg.delete()
        except Exception:
            pass
        return

    summary = (build_summary(mi_json, display_name, real_size, partial)
               or f"🎬 **{display_name}**")

    paste_url = None
    try:
        paste_url = mediainfo_paste(text=text, title=display_name)
    except Exception as error:
        logger.error(f"telegraph paste failed: {error}")

    if paste_url:
        paste_url = await _resolve_paste_url(paste_url)
        await reply_msg.edit(
            f"{summary}\n\n📝 **Full detailed MediaInfo:** "
            f"[{display_name}]({paste_url})",
            disable_web_page_preview=True)
    else:
        # Telegraph unreachable -> fall back to a text document
        txt_path = f"{path}.txt"
        with open(txt_path, "w", encoding="utf-8") as file:
            file.write(text)
        await target_message.reply_document(
            txt_path, caption=f"**File Name :** `{display_name}`", quote=True)
        try:
            await reply_msg.delete()
        except Exception:
            pass


async def telegram_mediainfo(client, media_message, isRaw=False):
    """Generate Mediainfo from a Telegram file (partial download + retry)."""
    media = get_media(media_message)
    if media is None:
        return await media_message.reply_text(
            "Reply to a media file (video / audio / document) to generate Mediainfo.",
            quote=True)

    display_name = (getattr(media, "file_name", None)
                    or f"telegram_file_{media_message.id}")[:100]

    reply_msg = await media_message.reply_text(
        "🎬 **Generating Mediainfo**, please wait ...", quote=True)
    path = None
    try:
        path, mi_json, fp_json, downloaded = await telegram_head_download(
            client, media_message, reply_msg, display_name)

        real_size = getattr(media, "file_size", None)
        partial = 0 < downloaded < _to_int(real_size)
        text, mi_json = await build_mediainfo(
            path, mi_json, display_name, real_size, partial)

        await _send_result(
            reply_msg, media_message, path, text, mi_json, display_name,
            real_size, isRaw, partial)

    except Exception as error:
        traceback.print_exc()
        logger.error(f"telegram mediainfo failed: {error}")
        await safe_edit(
            reply_msg,
            "❌ Something went wrong while generating Mediainfo from this Telegram file.")
    finally:
        cleanup_paths(path, f"{path}.txt" if path else None)


async def ddl_mediainfo(message, url, isRaw=False):
    """Generate Mediainfo from a direct download link (Range requests)."""
    reply_msg = await message.reply_text(
        "🎬 **Generating Mediainfo**, please wait ...", quote=True)
    path = None
    try:
        filename = unquote(url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]) or "media"
        display_name = filename[:100]

        path, mi_json, fp_json, total_size = await http_head_download(
            url, reply_msg, display_name)

        partial = 0 < os.path.getsize(path) < _to_int(total_size)
        text, mi_json = await build_mediainfo(
            path, mi_json, display_name, total_size, partial)

        await _send_result(
            reply_msg, message, path, text, mi_json, display_name,
            total_size, isRaw, partial)

    except Exception as error:
        traceback.print_exc()
        logger.error(f"ddl mediainfo failed: {error}")
        await safe_edit(
            reply_msg,
            "❌ Something went wrong while generating Mediainfo from the given link.")
    finally:
        cleanup_paths(path, f"{path}.txt" if path else None)


async def gdrive_mediainfo(message, url, isRaw=False):
    """Generate Mediainfo from a Google Drive link (authenticated Range requests)."""
    reply_msg = await message.reply_text(
        "🎬 **Generating Mediainfo**, please wait ...", quote=True)
    path = None
    try:
        drive = GoogleDriveHelper()
        metadata = drive.get_metadata(url)
        display_name = (metadata.get("name") or "gdrive_file")[:100]
        real_size = metadata.get("size")

        ddl_url = (
            "https://www.googleapis.com/drive/v3/files/"
            f"{drive.get_id(url)}?supportsAllDrives=true&alt=media"
        )
        path, mi_json, fp_json, downloaded = await http_head_download(
            ddl_url, reply_msg, display_name,
            headers={"Authorization": f"Bearer {drive.get_bearer_token()}"})

        partial = 0 < downloaded < _to_int(real_size)
        text, mi_json = await build_mediainfo(
            path, mi_json, display_name, real_size, partial)

        await _send_result(
            reply_msg, message, path, text, mi_json, display_name,
            real_size, isRaw, partial)

    except Exception as error:
        logger.error(f"gdrive mediainfo failed: {error}")
        await safe_edit(
            reply_msg,
            "❌ Something went wrong while processing the Google Drive link.\n\n"
            "Make sure the link is public, is a file (not a folder) and is not rate limited.")
    finally:
        cleanup_paths(path, f"{path}.txt" if path else None)


@Client.on_message(filters.command(["mediainfo", "m"]) & check_auth)
async def mediainfo_command(client, message: Message):
    usage = (
        "**Generates detailed Mediainfo from Google Drive links, Telegram files "
        "or direct download links.**\n\n"
        "Reply to any Telegram file or pass a link after the command.\n"
        "Use the `--r` flag for raw Mediainfo as a document.\n\n"
        "**Example:** `/m https://example.com/video.mkv --r`"
    )

    user_input = message.text.split(None, 1)[1] if len(message.command) > 1 else ""
    isRaw = bool(re.search(r"(?:--|-)r\b", user_input))

    if message.reply_to_message:
        if get_media(message.reply_to_message) is None:
            return await message.reply_text(
                "Reply to a media file (video / audio / document) to generate Mediainfo.",
                quote=True)
        return await telegram_mediainfo(client, message.reply_to_message, isRaw)

    if not user_input:
        return await message.reply_text(usage, quote=True)

    if url_match := re.search(GDRIVE_REGEX, user_input):
        return await gdrive_mediainfo(message, url_match.group(0), isRaw)

    if url_match := re.search(URL_REGEX, user_input):
        return await ddl_mediainfo(message, url_match.group(0), isRaw)

    return await message.reply_text("This type of link is not supported.", quote=True)
