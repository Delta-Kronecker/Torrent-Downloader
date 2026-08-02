import os
import asyncio
import logging
from pathlib import Path
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import libtorrent as lt

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads"))
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "2000"))

DOWNLOAD_DIR.mkdir(exist_ok=True)


def is_magnet_link(text: str) -> bool:
    return text.strip().startswith("magnet:?")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! Send me a Magnet link and I will download the torrent and send you the file."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send a Magnet link to download a torrent.\n\n"
        "Max file size: {} MB".format(MAX_FILE_SIZE_MB)
    )


async def download_torrent(magnet_link: str) -> Path | None:
    ses = lt.session()
    ses.listen_on(6881, 6891)

    params = {
        "save_path": str(DOWNLOAD_DIR),
        "storage_mode": lt.storage_mode_t.storage_mode_sparse,
    }

    handle = lt.add_magnet_uri(ses, magnet_link, params)
    logger.info("Downloading metadata: %s", magnet_link[:60])

    timeout = 120
    waited = 0
    while not handle.has_metadata() and waited < timeout:
        await asyncio.sleep(1)
        waited += 1

    if not handle.has_metadata():
        logger.error("Metadata timed out")
        ses.remove_torrent(handle)
        return None

    torrent_info = handle.get_torrent_info()
    file_name = torrent_info.name()
    total_size = torrent_info.total_size()

    max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
    if total_size > max_bytes:
        logger.error("File too large: %.2f MB", total_size / (1024 * 1024))
        ses.remove_torrent(handle)
        return None

    logger.info("Downloading: %s (%.2f MB)", file_name, total_size / (1024 * 1024))

    status_interval = 5
    last_status_time = 0

    while handle.status().state != lt.torrent_status.seeding:
        status = handle.status()
        now = asyncio.get_event_loop().time()

        if now - last_status_time >= status_interval:
            progress = status.progress * 100
            download_rate = status.download_rate / 1000
            logger.info(
                "Progress: %.1f%% | Download: %.1f kB/s",
                progress,
                download_rate,
            )
            last_status_time = now

        await asyncio.sleep(1)

    downloaded_file = DOWNLOAD_DIR / file_name
    logger.info("Download complete: %s", downloaded_file)

    ses.remove_torrent(handle)
    return downloaded_file


async def handle_magnet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    magnet_link = update.message.text.strip()

    if not is_magnet_link(magnet_link):
        await update.message.reply_text(
            "Please send a valid Magnet link.\n"
            "Example: magnet:?xt=urn:btih:..."
        )
        return

    status_msg = await update.message.reply_text(
        "Fetching metadata and downloading..."
    )

    try:
        downloaded_file = await asyncio.wait_for(
            download_torrent(magnet_link), timeout=600
        )
    except asyncio.TimeoutError:
        await status_msg.edit_text("Download failed: timeout.")
        return
    except Exception as e:
        logger.error("Download error: %s", str(e))
        await status_msg.edit_text("Download error: {}".format(str(e)))
        return

    if downloaded_file is None or not downloaded_file.exists():
        await status_msg.edit_text(
            "File not found or download failed. The torrent may be inactive or too large."
        )
        return

    file_size = downloaded_file.stat().st_size
    if file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await status_msg.edit_text(
            "File exceeds the {} MB limit.".format(MAX_FILE_SIZE_MB)
        )
        downloaded_file.unlink()
        return

    await status_msg.edit_text(
        "Uploading: {} ({:.1f} MB)...".format(
            downloaded_file.name, file_size / (1024 * 1024)
        )
    )

    try:
        with open(downloaded_file, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=downloaded_file.name,
                caption=downloaded_file.name,
            )
    except Exception as e:
        logger.error("Send error: %s", str(e))
        await status_msg.edit_text("Send error: {}".format(str(e)))
        return
    finally:
        await status_msg.delete()

    try:
        downloaded_file.unlink()
        logger.info("Cleaned up: %s", downloaded_file)
    except Exception as e:
        logger.warning("Cleanup failed: %s", str(e))


def main():
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_magnet)
    )

    logger.info("Bot started")
    application.run_polling()


if __name__ == "__main__":
    main()
