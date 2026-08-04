import os
import shutil
import tempfile
import asyncio
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Final

import yt_dlp
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TOKEN: Final = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in your .env file")

# Restrict the bot to only you. Anyone else who messages it gets ignored.
# Get your numeric Telegram user ID from @userinfobot, then set it in .env:
#   ALLOWED_USER_ID=123456789
_allowed_id_raw = os.getenv("ALLOWED_USER_ID")
if not _allowed_id_raw:
    raise RuntimeError(
        "ALLOWED_USER_ID is not set in your .env file. "
        "Message @userinfobot on Telegram to get your numeric user ID, "
        "then add ALLOWED_USER_ID=<your id> to .env."
    )
ALLOWED_USER_ID: Final = int(_allowed_id_raw)

MAX_FILESIZE_BYTES: Final = 20 * 1024 * 1024  # Telegram bot API cap for audio
MAX_DURATION_SECONDS: Final = 20 * 60  # skip anything over 20 min (podcasts, mixes, etc.)
SEARCH_RESULTS_COUNT: Final = 5
DOWNLOAD_WORKERS: Final = 1  # single user, no need to parallelize downloads

# All downloads live under a single temp dir that's wiped on startup and on exit,
# so nothing ever lingers on the SD card between runs.
DOWNLOAD_ROOT: Final = Path(tempfile.gettempdir()) / "musicbot_downloads"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("musicbot")

executor = ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS)

# Per-chat in-memory state (fine for a single user — one entry, never grows)
search_results: dict[int, list] = {}
search_result_msgs: dict[int, list[int]] = {}


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

def _is_owner(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == ALLOWED_USER_ID)


async def _reject_if_not_owner(update: Update) -> bool:
    """Returns True if the update was rejected (caller should stop processing)."""
    if _is_owner(update):
        return False
    user = update.effective_user
    logger.warning("Rejected message from unauthorized user id=%s", user.id if user else "unknown")
    # Deliberately vague reply — don't confirm this is a music bot to strangers.
    if update.message:
        await update.message.reply_text("This bot is private.")
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_duration(seconds) -> str:
    if seconds is None:
        return "N/A"
    seconds = int(seconds)
    minutes = seconds // 60
    sec = seconds % 60
    return f"{minutes}:{sec:02d}"


def search_youtube(query: str) -> list:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch{SEARCH_RESULTS_COUNT}:{query}", download=False)
        return info["entries"]


def download_audio(url: str) -> tuple[str, str]:
    """
    Downloads directly to m4a/opus/weba (whatever the source natively is) with
    NO ffmpeg re-encoding step — this is the single biggest speed win, since
    transcoding is by far the most CPU/time-expensive part of the process on
    a Pi. Telegram plays these formats natively as audio, so no conversion
    is needed.

    Each call gets its own throwaway subdirectory so concurrent/back-to-back
    requests can never collide on filenames, and cleanup is a single
    shutil.rmtree of that one directory — nothing can be left half-deleted.
    """
    request_dir = Path(tempfile.mkdtemp(dir=DOWNLOAD_ROOT))
    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(request_dir / "%(title).100s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": MAX_FILESIZE_BYTES,
        "postprocessors": [],  # explicitly no ffmpeg postprocessing
        "match_filter": yt_dlp.utils.match_filter_func(
            f"duration <? {MAX_DURATION_SECONDS}"
        ),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return filename, info.get("title", "Unknown Title")


async def async_download(url: str) -> tuple[str, str]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, download_audio, url)


def cleanup_download(file_path: str) -> None:
    """Delete the entire per-request temp directory, not just the file."""
    if not file_path:
        return
    try:
        parent = Path(file_path).parent
        if parent.exists() and parent.is_relative_to(DOWNLOAD_ROOT):
            shutil.rmtree(parent, ignore_errors=True)
    except Exception:
        logger.exception("Failed to clean up %s", file_path)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_owner(update):
        return
    await update.message.reply_text(
        "🎶 Hello! Send me a song name or YouTube link and I will get the audio for you."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_owner(update):
        return
    await update.message.reply_text(
        "📌 How to use:\n"
        "- Send a YouTube link → I'll download it.\n"
        "- Send a song name → I'll show you top 5 results with thumbnails and buttons.\n"
        f"- Max length: {MAX_DURATION_SECONDS // 60} min, max size: {MAX_FILESIZE_BYTES // (1024*1024)}MB."
    )


# ---------------------------------------------------------------------------
# Message handling
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_owner(update):
        return

    text: str = update.message.text.strip()

    if "youtube.com" in text or "youtu.be" in text:
        await _download_and_send(update.message.chat_id, text, update.message)
        return

    try:
        results = search_youtube(text)
    except Exception as e:
        logger.exception("Search failed")
        await update.message.reply_text(f"❌ Search error: {e}")
        return

    if not results:
        await update.message.reply_text("❌ No results found.")
        return

    # Overwrite any previous search for this chat — no unbounded growth.
    search_results[update.message.chat_id] = results
    messages = []

    for i, video in enumerate(results):
        duration = format_duration(video.get("duration"))
        thumbnail_url = next(
            (t["url"] for t in (video.get("thumbnails") or []) if t.get("url", "").endswith(".jpg")),
            None,
        )
        caption = f"{i+1}. {video['title']} ({duration})"
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("Select", callback_data=str(i))]])

        try:
            if not thumbnail_url:
                raise ValueError("no thumbnail")
            msg = await update.message.reply_photo(
                photo=thumbnail_url, caption=caption, reply_markup=reply_markup
            )
        except Exception:
            msg = await update.message.reply_text(text=caption, reply_markup=reply_markup)

        messages.append(msg.message_id)

    search_result_msgs[update.message.chat_id] = messages


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _is_owner(update):
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    chat_id = query.message.chat.id
    choice = int(query.data)

    if chat_id not in search_results or choice >= len(search_results[chat_id]):
        try:
            await query.edit_message_caption("❌ Sorry, no active search found.")
        except Exception:
            pass
        return

    video = search_results[chat_id][choice]
    url = video["url"]

    status_msg = await context.bot.send_message(chat_id=chat_id, text=f"🎵 Downloading: {video['title']}")

    await _download_and_send(chat_id, url, status_msg, context.bot, delete_search_ui=True)


async def _download_and_send(chat_id, url, status_or_message, bot=None, delete_search_ui=False):
    """
    Shared download+send+cleanup path used by both direct-link messages and
    button selections, so there's exactly one place that has to get file
    cleanup right.
    """
    file_path = None
    try:
        file_path, title = await async_download(url)
        with open(file_path, "rb") as f:
            if bot:
                await bot.send_audio(chat_id=chat_id, audio=f, title=title)
            else:
                await status_or_message.reply_audio(audio=f, title=title)
    except yt_dlp.utils.DownloadError as e:
        msg = f"❌ Couldn't download that (too long, too large, or unavailable): {e}"
        if bot:
            await bot.send_message(chat_id=chat_id, text=msg)
        else:
            await status_or_message.reply_text(msg)
    except Exception as e:
        logger.exception("Download/send failed")
        msg = f"❌ Error: {e}"
        if bot:
            await bot.send_message(chat_id=chat_id, text=msg)
        else:
            await status_or_message.reply_text(msg)
    finally:
        try:
            if bot:
                await status_or_message.delete()
        except Exception:
            pass

        if delete_search_ui:
            for msg_id in search_result_msgs.get(chat_id, []):
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except Exception:
                    pass
            search_results.pop(chat_id, None)
            search_result_msgs.pop(chat_id, None)

        cleanup_download(file_path)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Update caused error: %s", context.error, exc_info=context.error)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _reset_download_root():
    """Wipe any leftovers from a previous run/crash, then recreate fresh."""
    if DOWNLOAD_ROOT.exists():
        shutil.rmtree(DOWNLOAD_ROOT, ignore_errors=True)
    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    _reset_download_root()
    logger.info("Starting bot, restricted to user id %s", ALLOWED_USER_ID)

    app = (
        Application.builder()
        .token(TOKEN)
        .connect_timeout(60)
        .read_timeout(60)
        .write_timeout(60)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)

    try:
        logger.info("Polling...")
        app.run_polling(poll_interval=3)
    finally:
        shutil.rmtree(DOWNLOAD_ROOT, ignore_errors=True)