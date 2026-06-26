import os

from pyrogram.types import Message
from pyrogram import filters, Client

from TelegramBot.helpers.filters import check_auth
from TelegramBot.helpers.functions import async_subprocess
import time

_last_update = {}

async def progress(current, total, message):
    now = time.time()

    if (
        message.id not in _last_update
        or now - _last_update[message.id] > 2
        or current == total
    ):
        _last_update[message.id] = now

        percent = current * 100 / total
        done = int(percent / 5)
        bar = "█" * done + "░" * (20 - done)

        try:
            await message.edit_text(
                f"**Downloading audio...**\n\n"
                f"`[{bar}] {percent:.1f}%`"
            )
        except:
            pass

@Client.on_message(filters.command(["spek", "sox"]) & check_auth)
async def generate_spek(_, message: Message):
    """Generate spectrogram of music file using sox tool."""

    if not message.reply_to_message:
        return await message.reply_text(
            "Reply to a proper audio file to Generate audio spectrum.", quote=True)

    message = message.reply_to_message
    if message.text:
        return await message.reply_text(
            "Reply to a proper audio file to Generate audio spectrum.", quote=True)

    if message.media.value == "audio":
        media = message.audio

    elif message.media.value == "document":
        media = message.document

    else:
        return await message.reply_text(
            "Can only generate spectrum from audio file....", quote=True)

    file_name = str(media.file_name)
    mime = media.mime_type
    if message.media.value == "document" and "audio" not in mime:
        return await message.reply_text(
            "Can only generate spectrum from audio file....", quote=True)

    replymsg = await message.reply_text(
        "Generating Spectrogram of the audio. Please wait...", quote=True)
    await message.download(
        os.path.join(os.getcwd(), "download", file_name),
        progress=progress,
        progress_args=(replymsg,),
    )

    await replymsg.edit("🎵 Converting audio...")

    wav_file = f"download/{file_name}.wav"

    print("Starting FFmpeg")

    output = await async_subprocess(
        f"ffmpeg -nostdin -y -i 'download/{file_name}' -vn -ac 2 -ar 48000 '{wav_file}'"
    )

    print("FFmpeg finished")
    print(output)

    await replymsg.edit("📊 Generating spectrogram...")

    await async_subprocess(
        f"sox '{wav_file}' -n spectrogram -x 1000 -y 513 -z 120 -w Kaiser -o 'download/{file_name}.png'"
    )
    print("PNG:", os.path.exists(f"download/{file_name}.png"))

    os.remove(wav_file)

    if not os.path.exists(f"download/{file_name}.png"):
        return await replymsg.edit(
            "Can not able to generate spectograph of given audio.")

    await replymsg.edit("📤 Uploading spectrogram...")

    await message.reply_photo(
        photo=f"download/{file_name}.png",
        caption=f"**File:** `{file_name}`",
        quote=True,
    )

    await replymsg.delete()
    os.remove(f"download/{file_name}")
    os.remove(f"download/{file_name}.png")
