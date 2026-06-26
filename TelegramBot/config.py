import json
from os import getenv
from dotenv import load_dotenv

load_dotenv("config.env")

API_ID = int(getenv("API_ID"))
API_HASH = getenv("API_HASH")
BOT_TOKEN = getenv("BOT_TOKEN")

SUDO_USERID = json.loads(getenv("SUDO_USERID"))
AUTHORIZED_CHATS = json.loads(getenv("AUTHORIZED_CHATS"))
TELEGRAPH_TOKEN = "017cfe207a9dcfdb10294633593f9d8d3710f37afe554ddf8696a461450b"
