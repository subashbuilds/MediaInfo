import asyncio

from pyrogram import idle
from pyrogram.types import BotCommand

from TelegramBot import bot
from TelegramBot.logging import LOGGER

logger = LOGGER(__name__)

BOT_COMMANDS = [
    BotCommand("start", "check if the bot is alive"),
    BotCommand("mediainfo", "generate detailed mediainfo"),
    BotCommand("ss", "generate screenshots"),
    BotCommand("sample", "generate a video sample"),
    BotCommand("spek", "generate audio spectrograms"),
    BotCommand("paste", "paste text to katb.in"),
]


async def main():
    await bot.start()
    logger.info("client successfully initiated, press Ctrl+C to stop")

    # publish the command list so the "/" menu shows up automatically in Telegram
    try:
        await bot.set_bot_commands(BOT_COMMANDS)
    except Exception as error:
        logger.warning(f"could not set bot commands: {error}")

    await idle()
    await bot.stop()


if __name__ == "__main__":
    bot.run(main())
