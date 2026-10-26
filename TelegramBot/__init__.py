import os
import sys
import json
import time
import shutil
from pathlib import Path
from asyncio import get_event_loop, new_event_loop, set_event_loop

# --------------------------------------------------
# Initialize secret JSON files from environment variables
# --------------------------------------------------

# Repository root: parent directory of TelegramBot/
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def create_secret_file(env_name: str, filename: str) -> None:
    """Create a JSON file from a environment variable."""

    value = os.environ.get(env_name)

    if not value:
        raise RuntimeError(
            f"Missing required Railway environment variable: {env_name}"
        )

    try:
        parsed_json = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Railway variable {env_name} does not contain valid JSON."
        ) from exc

    file_path = PROJECT_ROOT / filename

    file_path.write_text(
        json.dumps(parsed_json, indent=2),
        encoding="utf-8",
    )

    try:
        file_path.chmod(0o600)
    except OSError:
        # Permissions may behave differently on some filesystems.
        pass


create_secret_file("TOKEN_JSON", "token.json")
create_secret_file("CREDENTIALS_JSON", "credentials.json")

# --------------------------------------------------
# Existing application imports
# --------------------------------------------------

import uvloop
import requests

from pyrogram import Client
from TelegramBot.config import *
from TelegramBot.logging import LOGGER
from apscheduler.schedulers.asyncio import AsyncIOScheduler

uvloop.install()
LOGGER(__name__).info("Starting TelegramBot....")
BotStartTime = time.time()


VERSION_ASCII = """
  =============================================================
  You MUST need to be on python 3.8 or above, shutting down the bot...
  =============================================================
  """

if sys.version_info[0] < 3 or sys.version_info[1] < 8:
    LOGGER(__name__).critical(VERSION_ASCII)
    sys.exit(1)

if not os.path.exists("token.json"):
    LOGGER(__name__).critical("token.json not found. quitting...")
    sys.exit(1)

if not os.path.exists("credentials.json"):
    LOGGER(__name__).info("credentials.json not found. quitting....")
    sys.exit(1)

with open(f"{os.getcwd()}/token.json", "r") as file:
    access_token = file.read()
access_token = json.loads(access_token)


BANNER = """
____________________________________________________________________
|  _______   _                                ____        _        |
| |__   __| | |                              |  _ \      | |       |
|    | | ___| | ___  __ _ _ __ __ _ _ __ ___ | |_) | ___ | |_      |
|    | |/ _ \ |/ _ \/ _` | '__/ _` | '_ ` _ \|  _ < / _ \| __|     |
|    | |  __/ |  __/ (_| | | | (_| | | | | | | |_) | (_) | |_      |
|    |_|\___|_|\___|\__, |_|  \__,_|_| |_| |_|____/ \___/ \__|     |
|                    __/ |                                         |
|__________________________________________________________________|   
"""
LOGGER(__name__).info(BANNER)
LOGGER(__name__).info("setting up event loop....")


try:
    loop = get_event_loop()
except RuntimeError:
    set_event_loop(new_event_loop())
    loop = get_event_loop()

# creating download directory
if not os.path.exists("download"):
    os.mkdir("download")


async def clear_download():
    """Clear download directory after every 24 hour."""

    shutil.rmtree("download")
    os.mkdir("download")


from zoneinfo import ZoneInfo

scheduler = AsyncIOScheduler(
    event_loop=loop,
    timezone=ZoneInfo("Asia/Kolkata"),
)
scheduler.add_job(clear_download, "interval", minutes=1440)
scheduler.start()


LOGGER(__name__).info("initiating the client....")
plugins = dict(root="TelegramBot/plugins")
bot = Client(
    "test",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    plugins=plugins,
)
