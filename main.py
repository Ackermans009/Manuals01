import os
import logging
import asyncio
import time
import re

from flask import Flask, request
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from pymongo import MongoClient
from dotenv import load_dotenv

from telegram import Bot, Update
from telegram.ext import CommandHandler, MessageHandler, Filters, Dispatcher, CallbackContext

# Load environment variables
load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMINS = [int(uid) for uid in os.getenv("ADMINS").split(",")]
PORT = int(os.getenv("PORT", 8000))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Ensure downloads directory exists
os.makedirs("downloads", exist_ok=True)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask app
app = Flask(__name__)
bot = Bot(token=BOT_TOKEN)
dispatcher = Dispatcher(bot, None, workers=0, use_context=True)

# MongoDB client
mongo_client = MongoClient(DATABASE_URL)
db = mongo_client["telegram_bot"]
sessions = db["sessions"]

# User states
user_states = {}

# Start command
def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "👋 Hi Ackerman, I am Save Content Bot. I can send you content by its post link.\n"
        "For downloading restricted content use /login first."
    )

# Login command
def login(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        update.message.reply_text("You are not authorized to use this bot.")
        return

    user_states[user_id] = {"stage": "awaiting_phone"}
    update.message.reply_text("Please send your phone number with country code (e.g., +1234567890):")

# Handle all messages
def handle_message(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    text = update.message.text

    if user_id not in user_states:
        update.message.reply_text("Use /start or /login to begin.")
        return

    state = user_states[user_id]
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Awaiting phone number
    if state["stage"] == "awaiting_phone":
        state["phone"] = text
        state["stage"] = "awaiting_code"
        session = StringSession()
        client = TelegramClient(session, API_ID, API_HASH)
        state["client"] = client

        async def send_code():
            await client.connect()
            await client.send_code_request(text)

        loop.run_until_complete(send_code())
        update.message.reply_text("OTP sent to your Telegram account. Please enter the code:")

    # Awaiting OTP
    elif state["stage"] == "awaiting_code":
        client = state["client"]
        phone = state["phone"]

        async def do_login():
            await client.connect()
            try:
                await client.sign_in(phone, text)
                session_str = client.session.save()
                sessions.update_one(
                    {"user_id": user_id},
                    {"$set": {"session": session_str}},
                    upsert=True
                )
                update.message.reply_text("You are logged in now. Send me a post link to download the content.")
                user_states[user_id] = {"stage": "awaiting_link", "client": client}
            except SessionPasswordNeededError:
                state["stage"] = "awaiting_password"
                update.message.reply_text("Two-step verification is enabled. Please enter your password:")

        loop.run_until_complete(do_login())

    # Awaiting 2FA Password
    elif state["stage"] == "awaiting_password":
        client = state["client"]
        password = text

        async def send_password():
            await client.sign_in(password=password)
            session_str = client.session.save()
            sessions.update_one(
                {"user_id": user_id},
                {"$set": {"session": session_str}},
                upsert=True
            )
            update.message.reply_text("You are logged in now. Send me a post link to download the content.")
            user_states[user_id] = {"stage": "awaiting_link", "client": client}

        loop.run_until_complete(send_password())

    # Awaiting link
    elif state["stage"] == "awaiting_link":
        state["link"] = text
        state["stage"] = "awaiting_count"
        update.message.reply_text("How many files do you want to download?")

    # Awaiting file count
    elif state["stage"] == "awaiting_count":
        try:
            count = int(text)
        except ValueError:
            update.message.reply_text("Please enter a valid number.")
            return

        state["count"] = count
        link = state["link"]
        client = state["client"]

        match = re.search(r"t\.me/([^/]+)/(\d+)", link)
        if not match:
            update.message.reply_text("Invalid message link format.")
            return

        entity_username = match.group(1)
        msg_id = int(match.group(2))

        async def download():
            await client.connect()
            entity = await client.get_entity(entity_username)
            messages = await client.get_messages(entity, ids=range(msg_id, msg_id + count))

            for msg in messages:
                if msg.media:
                    file_path = f"downloads/{msg.id}.mp4"
                    start = time.time()
                    await client.download_media(msg, file=file_path)
                    end = time.time()
                    size_kb = os.path.getsize(file_path) // 1024
                    speed = size_kb / (end - start)
                    update.message.reply_text(
                        f"Downloaded: {file_path}\nSize: {size_kb} KB\nSpeed: {int(speed)} KB/s"
                    )

        loop.run_until_complete(download())
        update.message.reply_text("Download complete!")
        user_states[user_id] = {"stage": "awaiting_link", "client": client}

# Handlers
dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(CommandHandler("login", login))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

# Flask routes
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dispatcher.process_update(update)
    return "ok"

@app.route("/")
def home():
    return "Telegram Save Content Bot is live!"

if __name__ == "__main__":
    if WEBHOOK_URL:
        bot.set_webhook(url=WEBHOOK_URL + BOT_TOKEN)
    app.run(host="0.0.0.0", port=PORT)
