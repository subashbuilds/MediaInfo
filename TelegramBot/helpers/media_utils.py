"""
Shared media helpers used by every generation plugin:

  * progressive partial downloads (Telegram files and HTTP links) — the whole
    file is never fetched, the head is downloaded in growing stages and
    re-parsed after each stage
  * robust media probing through ffprobe / MediaInfo with guarded JSON parsing
  * per-audio-track extraction and spek-style spectrogram rendering (SoX DSP
    with a redrawn spek 0.8.5 header/axes/dB legend)
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
    steps=None, full_below_mb=100, probe=True, full=False,
):
    """
    Download the head of a Telegram file in growing stages, re-parsing after
    every stage. Files smaller than `full_below_mb` are fetched in full.
    `full=True` forces the complete file (used by /spek, which needs the whole
    audio track and not just the first minutes).
    Returns (path, mi_json, fp_json, downloaded_bytes).
    """
    media = get_media(media_message)
    file_size = getattr(media, "file_size", 0) or 0
    path = os.path.join(DOWNLOAD_DIR, f"{randstr()}_{safe_filename(display_name)}")

    if file_size and (full or file_size <= full_below_mb * MB):
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
    full_below_mb=None,
):
    """
    Download the head of a remote file with HTTP Range requests, re-parsing
    after every stage. Returns (path, mi_json, fp_json, total_size_or_None).

    `full_below_mb` raises the first stage to the server's whole file size when
    that size is below the given limit (used by /spek, which needs the entire
    audio track), so small files are fetched in one go.
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
            if stage == 1 and total_size and full_below_mb:
                if total_size <= full_below_mb * MB:
                    limit_mb = math.ceil(total_size / MB) + 1

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
                "bit_rate": stream.get("bit_rate"),
                "bitrate_mode": None,
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
                "bit_rate": track.get("BitRate"),
                "bitrate_mode": track.get("BitRate_Mode"),
                "language": track.get("Language") or "",
                "title": track.get("Title") or "",
            })

    # ffprobe often omits `bit_rate` for audio streams — fill it in from the
    # MediaInfo JSON (both list audio tracks in the same container order)
    if tracks and mi_json:
        mi_audio = [t for t in (mi_json.get("media", {}).get("track") or [])
                    if t.get("@type") == "Audio"]
        for index, track in enumerate(tracks[:len(mi_audio)]):
            if not track.get("bit_rate"):
                track["bit_rate"] = mi_audio[index].get("BitRate")
            if not track.get("bitrate_mode"):
                track["bitrate_mode"] = mi_audio[index].get("BitRate_Mode")

    return tracks


def describe_audio_track(track):
    """Human readable one-liner: `E-AC-3 · 640 kbps · 6 ch (5.1) · 48 kHz · [ENG]`."""
    parts = [str(track.get("codec") or "Audio")]

    bit_rate = track.get("bit_rate")
    if bit_rate:
        try:
            kbps = int(float(str(bit_rate).replace(",", "")) / 1000)
            if kbps > 0:
                mode = (track.get("bitrate_mode") or "").strip().upper()
                parts.append(f"{kbps} kbps" + (" VBR" if mode == "VBR" else ""))
        except (TypeError, ValueError):
            pass

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
    Extract one audio track (by audio-relative index) as 48 kHz MONO PCM.
    Mono matters for two reasons: every channel is downmixed into one graph
    (SoX draws one pane PER CHANNEL, so a stereo/5.1 wav would show a double
    graph for a single audio track), and the output stays small enough to
    process and upload. `max_seconds` limits how much of the input is decoded.
    """
    if not os.path.exists(src_path):
        return False

    base_args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if max_seconds:
        base_args += ["-t", str(int(max_seconds))]
    base_args += ["-i", src_path, "-map", f"0:a:{stream_index}",
                  "-vn", "-sn", "-dn"]
    tail_args = ["-ar", "48000", "-c:a", "pcm_s16le", out_wav]

    # `-ac 1` uses ffmpeg's default downmix; `-af pan` names the channels
    # explicitly as a fallback for layouts ffmpeg refuses to downmix on its own.
    downmixes = (
        ["-ac", "1"],
        ["-af", "pan=mono|c0=0.5*c0+0.5*c1"],
        ["-ac", "1", "-af", "aresample=48000"],
    )

    for downmix in downmixes:
        try:
            code, output = await run_process(
                base_args + downmix + tail_args, timeout_seconds=900)
        except asyncio.TimeoutError:
            logger.error(f"audio extraction timed out for track {stream_index}")
            cleanup_paths(out_wav)
            return False

        if code == 0 and os.path.exists(out_wav) and os.path.getsize(out_wav) > 1024:
            return True
        logger.warning(
            f"mono extraction of track {stream_index} failed with "
            f"{downmix}: {output[:200]}")

    logger.error(f"audio extraction failed for track {stream_index}")
    return False


# --------------------------------------------------------------------------- #
#  Spectrogram rendering (spek 0.8.5 / classic SoX audio-viewer style)
# --------------------------------------------------------------------------- #
#
# Layout reproduced (identical to the reference image):
#
#   file name (bold)                                   spek 0.8.5
#   codec, bitrate, sample rate, channel, W:…, F:…           ┌────┐ 0 dB
#    24 MHz ┤            spectrogram (SoX palette)       ╷   │    │
#    22 MHz ┤                                            ╵   │    │
#     0 MHz └──┬────┬────┬────┬────┬────┬────┬────┬──────────┘ −120 dB
#             0:00 0:30 1:00 1:30 2:00 2:30 3:00 3:30
#
# SoX does the actual DSP (DFT, block averaging, dB conversion and colouring);
# this module only redraws the image with the spek-style header, kHz/mm:ss axes
# and dB colour legend around the raw spectrogram, which is why the palette
# matches the reference exactly.

SPEK_VERSION = "0.8.5"          # version shown at the top-right of the image
SPEK_WIDTH = 1568               # output image width in pixels
SPEK_HEIGHT = 780               # output image height in pixels
SPEK_PLOT_LEFT = 72             # left margin (frequency labels)
SPEK_PLOT_RIGHT = 84            # right margin (palette legend + dB labels)
SPEK_PLOT_TOP = 42              # top margin (file name + stream info)
SPEK_PLOT_BOTTOM = 36           # bottom margin (time labels)
SPEK_LEGEND_WIDTH = 14          # width of the colour legend strip
SPEK_DB_RANGE = 120             # dynamic range of the legend, in dB
SPEK_TIME_STEP = 300            # seconds between time-axis ticks (5 minutes)
SPEK_RENDER_TIMEOUT = 600       # seconds SoX may spend on the spek-style pass
SPEK_PLAIN_TIMEOUT = 300        # seconds for the plain fallback (no Pillow)
SPEK_FREQ_MIRROR = False        # flip vertically if the graph is upside down
# SoX is much faster when the plot height is 1 + 2**n (power-of-two DFT);
# 1025 gives a 2048-point DFT and is scaled to the canvas height afterwards.
SPEK_SOX_Y = 1025

# Minimum horizontal room for one 5-minute time tick, and a sanity cap so a
# very long recording cannot ask for an enormous image.
SPEK_MIN_TICK_PX = 58
SPEK_MAX_PLOT_PX = 6000


def _spek_plot_size(duration):
    """
    Plot area (width, height) in pixels. The height is fixed; the width grows
    with the duration so the 5-minute time ticks stay readable on long tracks.
    """
    plot_h = SPEK_HEIGHT - SPEK_PLOT_TOP - SPEK_PLOT_BOTTOM
    plot_w = SPEK_WIDTH - SPEK_PLOT_LEFT - SPEK_PLOT_RIGHT
    if duration and duration > 0:
        ticks = int(duration // SPEK_TIME_STEP) + 2
        plot_w = min(SPEK_MAX_PLOT_PX, max(plot_w, ticks * SPEK_MIN_TICK_PX))
    return plot_w, plot_h


def _spek_sox_rgb(level):
    """
    SoX's default heat palette (identical to spek's ``sox`` palette):
    black → blue → purple → red → orange → yellow → white.
    """
    level = min(1.0, max(0.0, float(level)))

    if level < 0.13:
        red = 0.0
    elif level < 0.73:
        red = math.sin((level - 0.13) / 0.60 * math.pi / 2.0)
    else:
        red = 1.0

    if level < 0.60:
        green = 0.0
    elif level < 0.91:
        green = math.sin((level - 0.60) / 0.31 * math.pi / 2.0)
    else:
        green = 1.0

    if level < 0.60:
        blue = 0.5 * math.sin(level / 0.60 * math.pi)
    elif level < 0.78:
        blue = 0.0
    else:
        blue = (level - 0.78) / 0.22

    return int(red * 255 + 0.5), int(green * 255 + 0.5), int(blue * 255 + 0.5)


def _nice_step(span, target=12):
    """A 1/2/2.5/5/10 times a power of ten axis step giving ~`target` ticks."""
    if span <= 0 or target <= 0:
        return 1.0
    raw = span / float(target)
    magnitude = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 2.5, 5, 10):
        if magnitude * factor >= raw - 1e-12:
            return magnitude * factor
    return magnitude * 10


def _format_freq(hertz):
    if hertz >= 1e6:
        return f"{hertz / 1e6:.6g} MHz"
    if hertz >= 1e3:
        return f"{hertz / 1e3:.6g} kHz"
    return f"{hertz:.6g} Hz"


def _format_time(seconds):
    total = max(0, int(round(seconds)))
    return f"{total // 60}:{total % 60:02d}"


def spek_track_description(track, sox_y=SPEK_SOX_Y, window="Hann",
                           stream_number=None, stream_total=None):
    """`Stream 1 / 1: E-AC-3, 640 kbps, 48000 Hz, mono (6 ch downmix), W:2048, F:Hann`."""
    parts = [str(track.get("codec") or "Audio")]

    bit_rate = track.get("bit_rate")
    if bit_rate:
        try:
            kbps = int(float(str(bit_rate).replace(",", "")) / 1000)
            if kbps > 0:
                parts.append(f"{kbps} kbps")
        except (TypeError, ValueError):
            pass

    sample_rate = track.get("sample_rate")
    if sample_rate:
        try:
            parts.append(f"{int(sample_rate)} Hz")
        except (TypeError, ValueError):
            pass

    # the spectrogram is always rendered from a mono downmix, so the header
    # says mono and only mentions the source channel count
    channels = track.get("channels")
    try:
        channels = int(channels) if channels else 0
    except (TypeError, ValueError):
        channels = 0
    if channels > 1:
        parts.append(f"mono ({channels} ch downmix)")
    else:
        parts.append("mono")

    parts.append(f"W:{2 * (sox_y - 1)}")
    parts.append(f"F:{window}")

    description = ", ".join(parts)
    if stream_number and stream_total:
        description = f"Stream {stream_number} / {stream_total}: {description}"
    return description


def _load_font(size, bold=False):
    """Best-effort font lookup (falls back to Pillow's built-in bitmap font)."""
    from PIL import ImageFont

    paths = []
    if bold:
        paths.append("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    paths += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ]
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _fit_text(draw, text, font, max_width):
    """Truncate `text` with an ellipsis so it fits inside `max_width` pixels."""
    if max_width <= 0:
        return ""
    if draw.textlength(text, font=font) <= max_width:
        return text

    ellipsis = "…"
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if draw.textlength(text[:mid] + ellipsis, font=font) <= max_width:
            low = mid
        else:
            high = mid - 1
    return text[:low] + ellipsis


def _draw_spectrogram_canvas(plot, title, desc, sample_rate, duration, version):
    """Compose the plot with the spek-style header, axes and dB legend."""
    from PIL import Image, ImageDraw

    plot_w, plot_h = _spek_plot_size(duration)
    left, top = SPEK_PLOT_LEFT, SPEK_PLOT_TOP
    width = plot_w + left + SPEK_PLOT_RIGHT
    height = plot_h + top + SPEK_PLOT_BOTTOM

    transpose = getattr(getattr(Image, "Transpose", Image), "FLIP_TOP_BOTTOM")
    if SPEK_FREQ_MIRROR:
        plot = plot.transpose(transpose)
    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    plot = plot.resize((plot_w, plot_h), resample)

    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    canvas.paste(plot, (left, top))
    draw = ImageDraw.Draw(canvas)

    font_small = _load_font(11)
    font_bold = _load_font(16, bold=True)

    draw.rectangle(
        [left - 1, top - 1, left + plot_w, top + plot_h],
        outline=(255, 255, 255))

    # Frequency axis (left, increasing upwards).
    nyquist = (sample_rate or 0) / 2.0
    if nyquist > 0:
        step = _nice_step(nyquist, target=12)
        value = 0.0
        while value <= nyquist + 1e-9:
            y = top + plot_h - (value / nyquist) * plot_h
            draw.line([left - 5, y, left - 1, y], fill=(255, 255, 255))
            label = _format_freq(value)
            label_width = draw.textlength(label, font=font_small)
            draw.text((left - 9 - label_width, y - 6), label,
                      font=font_small, fill=(255, 255, 255))
            value += step

    # Time axis (bottom): a tick every 5 minutes, with the true end of the
    # audio as the final label (a 2:43:00 track ends at "163:00").
    if duration and duration > 0:
        step = SPEK_TIME_STEP
        value = 0.0
        while value <= duration + 1e-9:
            x = left + (value / duration) * plot_w
            draw.line([x, top + plot_h + 1, x, top + plot_h + 5],
                      fill=(255, 255, 255))
            label = _format_time(value)
            label_width = draw.textlength(label, font=font_small)
            draw.text((x - label_width / 2, top + plot_h + 8), label,
                      font=font_small, fill=(255, 255, 255))
            value += step

        x = left + plot_w
        draw.line([x, top + plot_h + 1, x, top + plot_h + 5],
                  fill=(255, 255, 255))
        label = _format_time(duration)
        label_width = draw.textlength(label, font=font_small)
        draw.text((x - label_width / 2, top + plot_h + 8), label,
                  font=font_small, fill=(255, 255, 255))

    # Colour legend (right strip) with dB labels.
    legend_x = plot_w + left + 4
    strip = Image.new("RGB", (SPEK_LEGEND_WIDTH, plot_h), (0, 0, 0))
    pixels = strip.load()
    for y in range(plot_h):
        colour = _spek_sox_rgb(1.0 - y / max(1, plot_h - 1))
        for x in range(SPEK_LEGEND_WIDTH):
            pixels[x, y] = colour
    canvas.paste(strip, (legend_x, top))
    draw.rectangle(
        [legend_x - 1, top - 1, legend_x + SPEK_LEGEND_WIDTH, top + plot_h],
        outline=(255, 255, 255))

    step = _nice_step(SPEK_DB_RANGE, target=12)
    db = 0.0
    while db >= -SPEK_DB_RANGE - 1e-9:
        y = top + (-db / SPEK_DB_RANGE) * plot_h
        draw.text((legend_x + SPEK_LEGEND_WIDTH + 5, y - 6),
                  f"{int(round(db))} dB", font=font_small, fill=(255, 255, 255))
        db -= step

    # Header: file name (bold) + stream info on the left, version on the right.
    draw.text((left, 3), _fit_text(draw, title, font_bold, plot_w),
              font=font_bold, fill=(255, 255, 255))
    if desc:
        draw.text((left, top - 19), _fit_text(draw, desc, font_small, plot_w),
                  font=font_small, fill=(255, 255, 255))
    version_text = f"spek {version}"
    version_width = draw.textlength(version_text, font=font_bold)
    draw.text((width - SPEK_PLOT_RIGHT - version_width, 3), version_text,
              font=font_bold, fill=(255, 255, 255))

    return canvas


async def _sox_raw_spectrogram(wav_path, columns, rows_value, duration=None,
                               timeout_seconds=SPEK_RENDER_TIMEOUT):
    """
    Run SoX with `-r` (no axes/legends) and return the raw PNG path.

    SoX derives the time axis of the finished image from the decoded signal; a
    partially read / odd-length input can make it disagree with the WAV header,
    which would skew every time tick, so `-d` pins the duration when known.
    """
    png_path = f"{wav_path}.raw.png"
    args = [
        "sox", wav_path, "-n", "spectrogram",
        "-x", str(columns), "-y", str(rows_value),
        "-z", str(SPEK_DB_RANGE), "-w", "Kaiser",
        "-r", "-o", png_path,
    ]
    if duration and duration > 0:
        args += ["-d", f"{duration:.3f}"]

    try:
        code, output = await run_process(args, timeout_seconds=timeout_seconds)
    except asyncio.TimeoutError:
        logger.error("sox spectrogram (raw pass) timed out")
        cleanup_paths(png_path)
        return None

    if code != 0 or not os.path.exists(png_path) or os.path.getsize(png_path) == 0:
        logger.error(f"sox spectrogram (raw pass) failed: {output[:300]}")
        cleanup_paths(png_path)
        return None
    return png_path


async def _render_spek_spectrogram(wav_path, png_path, title, desc, version,
                                   duration=None):
    """Render the spek-style image; returns True on success."""
    try:
        from PIL import Image
    except ImportError:
        return False

    import wave

    sample_rate = None
    try:
        with wave.open(wav_path, "rb") as handle:
            sample_rate = handle.getframerate() or None
            if not duration:
                frames = handle.getnframes()
                if sample_rate:
                    duration = frames / float(sample_rate)
    except (wave.Error, OSError, ValueError):
        pass

    plot_w, _ = _spek_plot_size(duration)
    raw_path = await _sox_raw_spectrogram(wav_path, plot_w, SPEK_SOX_Y, duration)
    if raw_path is None:
        return False

    try:
        with Image.open(raw_path) as image:
            plot = image.convert("RGB")
        canvas = _draw_spectrogram_canvas(
            plot, title, desc, sample_rate, duration, version)
        canvas.save(png_path, "PNG", optimize=True)
    except Exception as error:
        logger.error(f"could not compose the spectrogram image: {error}")
        cleanup_paths(raw_path)
        return False

    cleanup_paths(raw_path)
    return os.path.exists(png_path) and os.path.getsize(png_path) > 0


async def _render_plain_sox_spectrogram(wav_path, png_path, title, comment):
    """Plain SoX spectrogram — fallback used when Pillow is unavailable."""
    # SoX sets its DFT size to 2*(y-1), so `-y 513` gives a 1024-point
    # (power-of-two) transform. A non-power-of-two height such as 900 forces
    # SoX's much slower generic transform, which times out on long tracks.
    args = [
        "sox", wav_path, "-n", "spectrogram",
        "-x", "1600", "-y", "513",
        "-z", str(SPEK_DB_RANGE), "-w", "Kaiser",
        "-o", png_path,
    ]
    if title:
        args += ["-t", title[:100]]
    if comment:
        args += ["-c", comment[:150]]

    try:
        code, output = await run_process(args, timeout_seconds=SPEK_PLAIN_TIMEOUT)
    except asyncio.TimeoutError:
        logger.error("sox spectrogram timed out")
        return False

    if code != 0 or not os.path.exists(png_path) or os.path.getsize(png_path) == 0:
        logger.error(f"sox spectrogram failed: {output[:300]}")
        return False
    return True


async def build_spectrogram(wav_path, png_path, title="", desc="",
                            version=SPEK_VERSION, duration=None):
    """
    Render a spek-style spectrogram: dark background, SoX heat palette, a
    header with the file name and stream info, kHz frequency axis, a 5-minute
    time axis ending at the real track length, and a dB colour legend. Falls
    back to a plain SoX image (its own legend) when Pillow is not installed.

    `duration` is the track length in seconds; it defaults to the length of the
    WAV file.
    """
    if not os.path.exists(wav_path) or os.path.getsize(wav_path) < 1024:
        return False

    if await _render_spek_spectrogram(wav_path, png_path, title, desc, version,
                                      duration=duration):
        return True

    logger.warning("spek-style rendering unavailable — using plain SoX output")
    return await _render_plain_sox_spectrogram(wav_path, png_path, title, desc)


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
