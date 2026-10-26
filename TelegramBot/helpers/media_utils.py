"""
Shared media helpers used by every generation plugin:

  * progressive partial downloads (Telegram files and HTTP links) — the whole
    file is never fetched, the head is downloaded in growing stages and
    re-parsed after each stage
  * robust media probing through ffprobe / MediaInfo with guarded JSON parsing
  * per-audio-track extraction and classic SoX spectrogram rendering
"""

import os
import re
import json
import math
import shutil
import shlex
import httpx
import asyncio

from async_timeout import timeout

from pyrogram.types import InputMediaPhoto
from pyrogram.errors import RPCError, MessageNotModified

from TelegramBot.helpers.functions import randstr
from TelegramBot.logging import LOGGER

logger = LOGGER(__name__)

DOWNLOAD_DIR = "download"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

GDRIVE_REGEX = r"https://drive\.google\.com/\S+"
URL_REGEX = (
    r"https?://([\w_-]+(?:(?:\.[\w_-]+)+))"
    r"([\w.,@?^=%&:\/~+#-]*[\w@?^=%&\/~+#-])"
)

# progressive head-download sizes (in MB). The file is re-parsed after every
# stage, so in most cases only the first stage is ever downloaded.
TELEGRAM_PROBE_STEPS = (25, 75, 200)
HTTP_PROBE_STEPS = (10, 50, 150)

MB = 1024 * 1024


# --------------------------------------------------------------------------- #
#  Small generic helpers
# --------------------------------------------------------------------------- #

def get_media(message):
    """Return the media object of a message (video / audio / voice / document)."""
    if message is None:
        return None
    for attribute in ("video", "audio", "voice", "document", "animation"):
        media = getattr(message, attribute, None)
        if media:
            return media
    return None


def safe_filename(name, fallback="media"):
    """Sanitise a file name for use inside the download directory."""
    name = os.path.basename(str(name or "")).strip().replace(" ", "_")
    name = re.sub(r"[^\w.\-()\[\]]", "", name)
    return (name or fallback)[:80]


async def safe_edit(message, text, **kwargs):
    """edit_message_text that never raises (not modified, unchanged text, ...)."""
    try:
        return await message.edit(text, disable_web_page_preview=True, **kwargs)
    except MessageNotModified:
        return message
    except RPCError as error:
        logger.warning(f"could not edit the status message: {error}")
        return message
    except Exception as error:
        logger.warning(f"could not edit the status message: {error}")
        return message


def cleanup_paths(*paths):
    """Delete files / directories, ignoring every error (accepts None entries)."""
    for path in paths:
        if not path:
            continue
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


async def run_process(args, timeout_seconds=900):
    """
    Run a process from an argument LIST (no shell -> no quoting bugs with
    spaces / unicode in file names). Returns (returncode, combined_output).
    """
    if isinstance(args, str):
        args = shlex.split(args)

    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout_seconds)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise

    output = (stdout or b"").decode(errors="ignore")
    errors = (stderr or b"").decode(errors="ignore")
    return process.returncode, (output + errors).strip()


# --------------------------------------------------------------------------- #
#  Probing (ffprobe / MediaInfo with guarded JSON parsing)
# --------------------------------------------------------------------------- #

def parse_mediainfo_json(raw):
    """Parse `mediainfo --Output=JSON` output, return the dict or None."""
    raw = (raw or "").strip()
    if not raw.startswith("{"):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    tracks = data.get("media", {}).get("track", []) or []
    # a usable parse needs a General track plus at least one stream track
    if len(tracks) < 2:
        return None
    return data


def parse_ffprobe_json(raw):
    """Parse ffprobe JSON output, return the dict or None."""
    raw = (raw or "").strip()
    if not raw.startswith("{"):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def media_probe_ok(mi_json, fp_json):
    """True when at least one prober actually recognised the file."""
    if fp_json and fp_json.get("streams"):
        return True
    if mi_json and len(mi_json.get("media", {}).get("track", []) or []) >= 2:
        return True
    return False


async def probe_file(path):
    """Probe a local (partial) file. Returns (mi_json, fp_json)."""
    mi_json = fp_json = None

    try:
        code, output = await run_process(
            ["ffprobe", "-v", "quiet", "-show_format", "-show_streams",
             "-print_format", "json", path],
            timeout_seconds=120)
        if code == 0:
            fp_json = parse_ffprobe_json(output)
    except Exception as error:
        logger.warning(f"ffprobe failed on {path}: {error}")

    try:
        code, output = await run_process(
            ["mediainfo", "--Output=JSON", path], timeout_seconds=120)
        if code == 0:
            mi_json = parse_mediainfo_json(output)
    except Exception as error:
        logger.warning(f"mediainfo failed on {path}: {error}")

    return mi_json, fp_json


async def probe_url_duration(url, headers=None):
    """ffprobe a remote URL directly, return its duration in seconds or None."""
    header_args = []
    if headers:
        if isinstance(headers, dict):
            header_string = "".join(
                f"{key}: {value}\r\n" for key, value in headers.items())
        else:
            header_string = str(headers).strip() + "\r\n"
        header_args = ["-headers", header_string]

    try:
        code, output = await run_process(
            ["ffprobe", "-v", "quiet", *header_args,
             "-show_format", "-print_format", "json", url],
            timeout_seconds=120)
    except Exception as error:
        logger.warning(f"ffprobe failed on the remote url: {error}")
        return None

    data = parse_ffprobe_json(output) if code == 0 else None
    if data:
        try:
            return float(data["format"]["duration"])
        except (KeyError, TypeError, ValueError):
            return None
    return None


def get_duration_seconds(mi_json, fp_json):
    """Best-effort duration in seconds from either probe result (or None)."""
    if fp_json:
        try:
            return float(fp_json["format"]["duration"])
        except (KeyError, TypeError, ValueError):
            pass
        durations = []
        for stream in fp_json.get("streams", []) or []:
            try:
                durations.append(float(stream["duration"]))
            except (KeyError, TypeError, ValueError):
                continue
        if durations:
            return max(durations)

    if mi_json:
        for track in mi_json.get("media", {}).get("track", []) or []:
            try:
                return float(track["Duration"])
            except (KeyError, TypeError, ValueError):
                continue
    return None


def covered_duration(duration, downloaded, total_size):
    """Estimate how many seconds of media the downloaded part contains."""
    try:
        if not duration or not total_size or downloaded >= total_size:
            return None
        return duration * (float(downloaded) / float(total_size))
    except (TypeError, ZeroDivisionError):
        return None


# --------------------------------------------------------------------------- #
#  Progressive partial downloads
# --------------------------------------------------------------------------- #

async def telegram_head_download(
    client, media_message, reply_msg, display_name="media",
    steps=None, full_below_mb=100, probe=True,
):
    """
    Download the head of a Telegram file in growing stages, re-parsing after
    every stage. Files smaller than `full_below_mb` are fetched in full.
    Returns (path, mi_json, fp_json, downloaded_bytes).
    """
    media = get_media(media_message)
    file_size = getattr(media, "file_size", 0) or 0
    path = os.path.join(DOWNLOAD_DIR, f"{randstr()}_{safe_filename(display_name)}")

    if file_size and file_size <= full_below_mb * MB:
        stages = [math.ceil(file_size / MB) + 2]
    else:
        stages = list(steps or TELEGRAM_PROBE_STEPS)

    mi_json = fp_json = None
    downloaded = 0

    for stage, limit_mb in enumerate(stages, 1):
        await safe_edit(
            reply_msg,
            f"⬇️ **Downloading** `{display_name}` — stage {stage}/{len(stages)} "
            f"(~{limit_mb} MB) ...")

        chunks = 0
        with open(path, "wb") as file:
            async for chunk in client.stream_media(media_message, limit=limit_mb):
                file.write(chunk)
                chunks += 1
                if chunks % 25 == 0:
                    await safe_edit(
                        reply_msg,
                        f"⬇️ **Downloading** `{display_name}` — {chunks} MB ...")

        downloaded = os.path.getsize(path) if os.path.exists(path) else 0
        if not probe:
            break

        mi_json, fp_json = await probe_file(path)
        if media_probe_ok(mi_json, fp_json):
            break
        if file_size and downloaded >= file_size:
            break  # the whole file is here, a bigger stage cannot help

    return path, mi_json, fp_json, downloaded


async def http_head_download(
    url, reply_msg, display_name="media", steps=None, headers=None,
):
    """
    Download the head of a remote file with HTTP Range requests, re-parsing
    after every stage. Returns (path, mi_json, fp_json, total_size_or_None).
    """
    path = os.path.join(DOWNLOAD_DIR, f"{randstr()}_{safe_filename(display_name)}")
    request_headers = {"user-agent": DEFAULT_UA, "accept-encoding": "identity"}
    if headers:
        request_headers.update(headers)

    total_size = None
    mi_json = fp_json = None
    stages = list(steps or HTTP_PROBE_STEPS)

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, read=120.0),
        headers=request_headers,
    ) as client:
        for stage, limit_mb in enumerate(stages, 1):
            await safe_edit(
                reply_msg,
                f"⬇️ **Downloading** `{display_name}` — stage {stage}/{len(stages)} "
                f"(~{limit_mb} MB) ...")

            max_bytes = limit_mb * MB
            try:
                async with timeout(600):
                    async with client.stream(
                        "GET", url, headers={"range": f"bytes=0-{max_bytes - 1}"},
                    ) as response:
                        if response.status_code not in (200, 206):
                            raise RuntimeError(
                                f"HTTP status {response.status_code}")

                        content_range = response.headers.get("content-range") or ""
                        if "/" in content_range:
                            try:
                                total_size = int(content_range.rsplit("/", 1)[-1])
                            except ValueError:
                                pass
                        elif response.status_code == 200:
                            content_length = response.headers.get("content-length")
                            if content_length and content_length.isdigit():
                                total_size = int(content_length)

                        # hard cap: some servers ignore Range and return 200
                        received = 0
                        with open(path, "wb") as file:
                            async for chunk in response.aiter_bytes(MB):
                                file.write(chunk)
                                received += len(chunk)
                                if received >= max_bytes:
                                    break
            except (httpx.HTTPError, asyncio.TimeoutError, RuntimeError) as error:
                logger.error(f"download stage {limit_mb} MB failed: {error}")
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    break  # work with whatever arrived
                continue

            downloaded = os.path.getsize(path) if os.path.exists(path) else 0
            if total_size and downloaded >= total_size:
                break  # the whole file fit inside this stage

            mi_json, fp_json = await probe_file(path)
            if media_probe_ok(mi_json, fp_json):
                break

    if os.path.exists(path) and not media_probe_ok(mi_json, fp_json):
        mi_json, fp_json = await probe_file(path)

    return path, mi_json, fp_json, total_size


# --------------------------------------------------------------------------- #
#  Audio tracks & spectrograms
# --------------------------------------------------------------------------- #

def list_audio_tracks(fp_json, mi_json=None):
    """
    Collect every audio track from the probe results. Each entry carries the
    audio-relative index (`-map 0:a:N`) and the absolute ffprobe stream index.
    """
    tracks = []

    if fp_json:
        for stream in fp_json.get("streams", []) or []:
            if stream.get("codec_type") != "audio":
                continue
            tags = stream.get("tags") or {}
            tracks.append({
                # `-map 0:a:N` needs the audio-relative index
                "stream_index": len(tracks),
                "absolute_index": stream.get("index", 0),
                "position": len(tracks) + 1,
                "codec": (stream.get("codec_name") or "?").upper(),
                "channels": stream.get("channels"),
                "channel_layout": stream.get("channel_layout") or "",
                "sample_rate": stream.get("sample_rate"),
                "language": (tags.get("language") or tags.get("LANGUAGE")
                             or tags.get("lang") or ""),
                "title": tags.get("title") or tags.get("TITLE") or "",
            })

    if not tracks and mi_json:
        for track in mi_json.get("media", {}).get("track", []) or []:
            if track.get("@type") != "Audio":
                continue
            tracks.append({
                "stream_index": len(tracks),
                "absolute_index": None,
                "position": len(tracks) + 1,
                "codec": track.get("Format") or "?",
                "channels": track.get("Channel(s)"),
                "channel_layout": track.get("ChannelLayout") or "",
                "sample_rate": track.get("SamplingRate"),
                "language": track.get("Language") or "",
                "title": track.get("Title") or "",
            })

    return tracks


def describe_audio_track(track):
    """Human readable one-liner: `E-AC-3 · 6 ch (5.1) · 48 kHz · [ENG] · Title`."""
    parts = [str(track.get("codec") or "Audio")]

    channels = track.get("channels")
    layout = track.get("channel_layout") or ""
    if channels:
        channel_text = f"{channels} ch"
        if layout and str(channels) not in layout:
            channel_text += f" ({layout})"
        parts.append(channel_text)
    elif layout:
        parts.append(layout)

    if track.get("sample_rate"):
        try:
            parts.append(f"{int(track['sample_rate']) // 1000} kHz")
        except (TypeError, ValueError):
            pass

    language = track.get("language") or ""
    if language and language.lower() not in ("und", "unknown"):
        parts.append(f"[{language.upper()}]")

    title = track.get("title") or ""
    if title:
        parts.append(title[:60])

    return " · ".join(parts)


async def extract_audio_track(src_path, stream_index, out_wav, max_seconds=None):
    """
    Extract one audio track (by audio-relative index) as 48 kHz stereo PCM.
    `max_seconds` limits how much of the input is decoded.
    """
    if not os.path.exists(src_path):
        return False

    args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if max_seconds:
        args += ["-t", str(int(max_seconds))]
    args += [
        "-i", src_path,
        "-map", f"0:a:{stream_index}",
        "-vn", "-sn", "-dn",
        "-ac", "2", "-ar", "48000",
        "-c:a", "pcm_s16le",
        out_wav,
    ]

    try:
        code, output = await run_process(args, timeout_seconds=900)
    except asyncio.TimeoutError:
        logger.error(f"audio extraction timed out for track {stream_index}")
        cleanup_paths(out_wav)
        return False

    if code != 0:
        logger.error(
            f"audio extraction failed for track {stream_index}: {output[:300]}")
    return code == 0 and os.path.exists(out_wav) and os.path.getsize(out_wav) > 1024


async def build_spectrogram(wav_path, png_path, title="", comment=""):
    """
    Render a wide SoX spectrogram in the classic style: dark background,
    vivid green→yellow→red heat colours, kHz and time axes, title on top and
    the track description at the bottom.
    """
    if not os.path.exists(wav_path) or os.path.getsize(wav_path) < 1024:
        return False

    args = [
        "sox", wav_path, "-n", "spectrogram",
        "-x", "2400", "-y", "900",   # large, sharp output image
        "-z", "120",                 # dynamic range in dB
        "-w", "Kaiser",              # window function
        "-o", png_path,
    ]
    if title:
        args += ["-t", title[:100]]
    if comment:
        args += ["-c", comment[:150]]

    try:
        code, output = await run_process(args, timeout_seconds=900)
    except asyncio.TimeoutError:
        logger.error("sox spectrogram timed out")
        return False

    if code != 0 or not os.path.exists(png_path) or os.path.getsize(png_path) == 0:
        logger.error(f"sox spectrogram failed: {output[:300]}")
        return False
    return True


async def send_spectrogram_album(message, items):
    """Send (png_path, caption) pairs as Telegram albums (10 per album max)."""
    for start in range(0, len(items), 10):
        group = items[start:start + 10]
        try:
            if len(group) == 1:
                await message.reply_photo(
                    photo=group[0][0], caption=group[0][1], quote=True)
            else:
                await message.reply_media_group(
                    media=[
                        InputMediaPhoto(media=png_path, caption=caption)
                        for png_path, caption in group
                    ],
                    quote=True,
                )
        except Exception as error:
            logger.error(f"album upload failed, sending single photos: {error}")
            for png_path, caption in group:
                try:
                    await message.reply_photo(
                        photo=png_path, caption=caption, quote=True)
                except Exception as inner_error:
                    logger.error(f"photo upload failed: {inner_error}")
        await asyncio.sleep(1)
