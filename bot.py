#!/usr/bin/env python3
"""Telegram bot: receives magnet / .torrent links, downloads the file with aria2,
and sends it back to the user. Designed to run on a GitHub Actions workflow."""

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
import zipfile

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("torrent-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID")
ALLOWED_USER_IDS = [
    int(x.strip())
    for x in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if x.strip()
]
MAX_RUNTIME_MINUTES = int(os.environ.get("MAX_RUNTIME_MINUTES") or "300")
MAX_SEND_SIZE = int(os.environ.get("MAX_SEND_SIZE") or str(50 * 1024 * 1024))
ARIA2_PORT = int(os.environ.get("ARIA2_PORT", "6800"))

MAGNET_RE = re.compile(
    r"(magnet:\?xt=urn:btih:[a-zA-Z0-9]{32,40}(?:&[^\s]+)?)", re.IGNORECASE
)

HELP_TEXT = """\
Hi! I am a torrent download bot.

Send me a magnet link or a .torrent file and I will download
the file and send it back to you.

Commands:
/start or /help - this help
/status - current download status
/cancel - cancel current download
"""

_lock = asyncio.Lock()
state = {"gid": None}
base_dir = None
aria2_proc = None


def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS


def rpc(method: str, params=None) -> dict:
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": "1", "method": method, "params": params or []}
    ).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{ARIA2_PORT}/jsonrpc",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    if "error" in data:
        raise RuntimeError(data["error"].get("message", "aria2 RPC error"))
    return data["result"]


def start_aria2() -> None:
    global aria2_proc
    aria2_proc = subprocess.Popen(
        [
            "aria2c",
            "--enable-rpc",
            f"--rpc-listen-port={ARIA2_PORT}",
            "--seed-time=0",
            "--console-log-level=warn",
            "--file-allocation=none",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(30):
        try:
            rpc("aria2.getVersion")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("aria2 did not start")


def cleanup_dir(path: str) -> None:
    for entry in os.listdir(path):
        full = os.path.join(path, entry)
        try:
            if os.path.isdir(full):
                shutil.rmtree(full)
            else:
                os.remove(full)
        except OSError:
            pass


async def send_results(bot, chat_id: int, files: list) -> None:
    paths = [
        f["path"]
        for f in files
        if f.get("path") and os.path.isfile(f["path"])
    ]
    if not paths:
        await bot.send_message(chat_id, "No files found to send.")
        return

    sendable = []
    for p in paths:
        size = os.path.getsize(p)
        if size > MAX_SEND_SIZE:
            await bot.send_message(
                chat_id,
                f"File \"{os.path.basename(p)}\" is {size / 1024 / 1024:.1f} MB, "
                f"which exceeds the bot send limit "
                f"({MAX_SEND_SIZE // 1024 // 1024} MB).",
            )
        else:
            sendable.append(p)

    if not sendable:
        return

    if len(sendable) == 1:
        await bot.send_document(chat_id, document=sendable[0])
    else:
        zip_path = os.path.join(base_dir, "files.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sendable:
                zf.write(p, arcname=os.path.basename(p))
        await bot.send_document(chat_id, document=zip_path)

    await bot.send_message(chat_id, "Done. File(s) sent.")


async def run_download(update: Update, magnet, torrent_path, status_msg) -> None:
    bot = update.get_bot()
    chat_id = update.effective_chat.id
    gid = None
    try:
        if magnet:
            gid = rpc("aria2.addUri", [[magnet], {"dir": base_dir, "seed-time": "0"}])
        else:
            with open(torrent_path, "rb") as f:
                torrent_b64 = base64.b64encode(f.read()).decode()
            gid = rpc(
                "aria2.addTorrent",
                [torrent_b64, {"dir": base_dir, "seed-time": "0"}],
            )
        state["gid"] = gid

        last_report = 0.0
        while True:
            status = rpc(
                "aria2.tellStatus",
                [
                    gid,
                    [
                        "status",
                        "totalLength",
                        "completedLength",
                        "downloadSpeed",
                        "errorMessage",
                        "files",
                    ],
                ],
            )
            st = status.get("status")
            if st == "complete":
                await status_msg.edit_text("Download complete. Sending file(s)...")
                await send_results(bot, chat_id, status.get("files") or [])
                return
            if st in ("error", "removed"):
                await status_msg.edit_text(
                    f"Download failed: {status.get('errorMessage') or st}"
                )
                return

            now = time.monotonic()
            total = int(status.get("totalLength") or 0)
            done = int(status.get("completedLength") or 0)
            if now - last_report >= 10 and total > 0:
                pct = done / total * 100
                speed = int(status.get("downloadSpeed") or 0) / 1024 / 1024
                try:
                    await status_msg.edit_text(
                        f"Downloading: {pct:.1f}% | Speed: {speed:.2f} MB/s"
                    )
                except Exception:
                    pass
                last_report = now
            await asyncio.sleep(3)
    except Exception as e:
        logger.exception("download failed")
        try:
            await status_msg.edit_text(f"Error: {e}")
        except Exception:
            pass
    finally:
        state["gid"] = None
        if gid:
            try:
                rpc("aria2.forceRemove", [gid])
            except Exception:
                pass
        if torrent_path and os.path.exists(torrent_path):
            try:
                os.remove(torrent_path)
            except OSError:
                pass
        cleanup_dir(base_dir)


async def start_download(update: Update, magnet, torrent_path) -> None:
    async with _lock:
        if state["gid"]:
            await update.message.reply_text(
                "Another download is already in progress. "
                "Send /cancel or wait for it to finish."
            )
            return
    status_msg = await update.message.reply_text("Starting download...")
    asyncio.create_task(run_download(update, magnet, torrent_path, status_msg))


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("You are not allowed to use this bot.")
        return
    await update.message.reply_text(HELP_TEXT)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    gid = state["gid"]
    if not gid:
        await update.message.reply_text("No download in progress.")
        return
    try:
        status = rpc(
            "aria2.tellStatus",
            [gid, ["status", "totalLength", "completedLength", "downloadSpeed", "errorMessage"]],
        )
        st = status.get("status")
        if st == "error":
            await update.message.reply_text(f"Error: {status.get('errorMessage')}")
            return
        total = int(status.get("totalLength") or 0)
        done = int(status.get("completedLength") or 0)
        speed = int(status.get("downloadSpeed") or 0) / 1024 / 1024
        text = f"Status: {st}\n"
        if total > 0:
            text += f"Progress: {done / total * 100:.1f}%\n"
        text += f"Speed: {speed:.2f} MB/s"
        await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text(f"Failed to get status: {e}")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    gid = state["gid"]
    if not gid:
        await update.message.reply_text("Nothing to cancel.")
        return
    try:
        rpc("aria2.forceRemove", [gid])
    except Exception as e:
        logger.warning("cancel failed: %s", e)
    state["gid"] = None
    await update.message.reply_text("Download cancelled.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    text = update.message.text or ""
    match = MAGNET_RE.search(text)
    if not match:
        await update.message.reply_text(
            "No valid magnet link found. It should start with magnet:?xt=urn:btih:"
        )
        return
    await start_download(update, match.group(1), None)


async def handle_torrent_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    doc = update.message.document
    if not (doc.file_name or "").lower().endswith(".torrent"):
        await update.message.reply_text(
            "Only .torrent files are supported. "
            "For magnet links, just send the link as text."
        )
        return
    torrent_path = os.path.join(tempfile.gettempdir(), f"upload_{int(time.time())}.torrent")
    file = await doc.get_file()
    await file.download_to_drive(torrent_path)
    await start_download(update, None, torrent_path)


async def watchdog(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("max runtime reached (%s minutes), shutting down", MAX_RUNTIME_MINUTES)
    if OWNER_CHAT_ID:
        try:
            await context.bot.send_message(
                OWNER_CHAT_ID,
                "Workflow max runtime reached, bot is stopping.",
            )
        except Exception:
            pass
    await context.application.stop()


def main() -> None:
    global base_dir
    if not BOT_TOKEN:
        raise SystemExit("env BOT_TOKEN is required")
    base_dir = tempfile.mkdtemp(prefix="torrent_bot_")
    start_aria2()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(
        MessageHandler(filters.Document.FileExtension("torrent"), handle_torrent_file)
    )
    app.job_queue.run_once(watchdog, when=60 * MAX_RUNTIME_MINUTES)

    logger.info("bot started, running for up to %s minutes", MAX_RUNTIME_MINUTES)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
