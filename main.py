from multiprocessing import context
import os
from turtle import update
import yt_dlp
import asyncio
import time

from dotenv import load_dotenv
from typing import Final
from telegram import InputMediaPhoto
from concurrent.futures import ThreadPoolExecutor
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

load_dotenv()


TOKEN: Final = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_USERNAME: Final = "@Muse_Down_bot"

CHOOSING = 1


# Store search results temporarily
search_results = {}        # video info
search_result_msgs = {}    # message IDs for deletion
executor = ThreadPoolExecutor(max_workers=2)  # background downloads

# - - - functions that make life easier - - - #

# Converts seconds duration to min:sec
def format_duration(seconds):
    if seconds is None:
        return "N/A"
    seconds = int(seconds)
    minutes = seconds // 60
    sec = seconds % 60
    return f"{minutes}:{sec:02d}"

# Searches what the user types on youtube for first 5 entries
def search_youtube(query: str):
    ydl_opts = {
        "quiet": True,
        "extract_flat": True,   # only metadata
        "skip_download": True,
        "noplaylist": True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch5:{query}", download=False)
        return info["entries"]

# Downloads the audio of the files
def download_audio(url: str):
    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": "%(title)s.%(ext)s",
        "quiet": True,
        "noplaylist": True,
        "max_filesize": 20 * 1024 * 1024,  # 20MB limit
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return filename, info.get("title", "Unknown Title")
    
# Downloads the files asynchronously
async def async_download(url: str):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, download_audio, url)

# bot commands

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎶 Hello! Send me a song name or YouTube link and I will get the audio for you."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📌 How to use:\n"
        "- Send a YouTube link → I’ll download it.\n"
        "- Send a song name → I’ll show you top 5 results with thumbnails and buttons."
    )

# handling messages

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text: str = update.message.text.strip()

    # youtube links
    if "youtube.com" in text or "youtu.be" in text:
        status_msg = await update.message.reply_text("🎵 Downloading, please wait...")

        file_path = None
        try:
            file_path, title = await async_download(text)

            with open(file_path, "rb") as f:
                await update.message.reply_audio(audio=f, title=title)

        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

        finally:
            # delete status message
            try:
                await status_msg.delete()
            except:
                pass

            # delete file safely
            if file_path and os.path.exists(file_path):
                for _ in range(3):
                    try:
                        os.remove(file_path)
                        break
                    except PermissionError:
                        import time
                        time.sleep(1)

        return

    # Search YouTube
    results = search_youtube(text)
    if not results:
        await update.message.reply_text("❌ No results found.")
        return

    search_results[update.message.chat_id] = results
    messages = []

    for i, video in enumerate(results):
        duration_sec = video.get("duration")
        duration = format_duration(duration_sec)

        thumbnail_url = None
        if video.get("thumbnails"):
            for t in video["thumbnails"]:
                if t.get("url", "").endswith(".jpg"):
                    thumbnail_url = t["url"]
                    break

        caption = f"{i+1}. {video['title']} ({duration})"
        keyboard = [[InlineKeyboardButton("Select", callback_data=str(i))]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            if thumbnail_url:
                msg = await update.message.reply_photo(
                    photo=thumbnail_url,
                    caption=caption,
                    reply_markup=reply_markup
                )
            else:
                raise Exception("No valid thumbnail")
        except:
            msg = await update.message.reply_text(
                text=caption,
                reply_markup=reply_markup
            )

        messages.append(msg.message_id)

    search_result_msgs[update.message.chat_id] = messages

#button presses

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat.id
    choice = int(query.data)

    if chat_id not in search_results:
        try:
            await query.edit_message_caption("❌ Sorry, no active search found.")
        except:
            pass
        return

    video = search_results[chat_id][choice]
    url = video["url"]

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"🎵 Downloading: {video['title']}"
    )

    file_path = None
    try:
        file_path, title = await async_download(url)

        with open(file_path, "rb") as f:
            await context.bot.send_audio(chat_id=chat_id, audio=f, title=title)

    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Error: {e}")

    finally:
        # delete "Downloading..." message
        try:
            await status_msg.delete()
        except:
            pass

        # delete search result messages
        for msg_id in search_result_msgs.get(chat_id, []):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except:
                pass

        # delete file safely
        if file_path and os.path.exists(file_path):
            for _ in range(3):
                try:
                    os.remove(file_path)
                    break
                except PermissionError:
                    import time
                    time.sleep(1)

    # Clean up memory
    search_results.pop(chat_id, None)
    search_result_msgs.pop(chat_id, None)

# error handler
async def error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"Update: {update} caused error {context.error}")

#running
if __name__ == "__main__":
    print("Starting Bot...")
    app = (
        Application.builder()
        .token(TOKEN)
        .connect_timeout(60)
        .read_timeout(60)
        .write_timeout(60)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))

    # Message handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Inline button handler
    app.add_handler(CallbackQueryHandler(button_handler))

    # Error handler
    app.add_error_handler(error)

    print("Polling...")
    app.run_polling(poll_interval=3)