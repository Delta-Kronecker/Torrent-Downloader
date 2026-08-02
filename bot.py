#!/usr/bin/env python3
"""Telegram bot: receives magnet / .torrent links, downloads the files one by one
with aria2, sends each file back to the user, and reports the source link."""

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

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
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
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
ARIA2_PORT = int(os.environ.get("ARIA2_PORT") or "6800")

MAGNET_RE = re.compile(
    r"(magnet:\?xt=urn:btih:[a-zA-Z0-9]{32,40}(?:&[^\s]+)?)", re.IGNORECASE
)

HELP_TEXT = """\
Hi! I am a torrent download bot.

Send me a magnet link or a .torrent file and I will download
the files and send them back to you one by one.

Commands:
/start or /help - this help
/status - current download status
/cancel - cancel current download
"""

_lock = asyncio.Lock()
state = {"gid": None}
base_dir = None
aria2_proc = None
aria2_log_path = os.path.join(tempfile.gettempdir(), "aria2_console.log")


class DownloadError(Exception):
    pass


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
    log_file = open(aria2_log_path, "wb")
    aria2_proc = subprocess.Popen(
        [
            "aria2c",
            "--enable-rpc",
            f"--rpc-listen-port={ARIA2_PORT}",
            "--seed-time=0",
            "--console-log-level=notice",
            "--file-allocation=none",
            "--enable-dht=true",
            "--dht-listen-port=17000-17999",
            "--enable-peer-exchange=true",
            "--bt-tracker="
            "udp://tracker.opentrackr.org:1337/announce,"
            "http://tracker.opentrackr.org:1337/announce,"
            "udp://tracker.openbittorrent.com:80/announce,"
            "http://tracker.openbittorrent.com:80/announce,"
            "udp://open.demonii.com:1337/announce,"
            "udp://exodus.desync.com:6969/announce,"
            "udp://open.stealth.si:80/announce,"
            "udp://tracker.torrent.eu.org:451/announce,"
            "http://tracker.torrent.eu.org:451/announce",
        ],
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    for _ in range(30):
        try:
            rpc("aria2.getVersion")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("aria2 did not start")


def log_aria2_tail(lines: int = 40) -> None:
    try:
        with open(aria2_log_path, "rb") as f:
            tail = f.read().decode(errors="replace").splitlines()[-lines:]
        for line in tail:
            logger.warning("aria2: %s", line)
    except OSError:
        pass


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


def remove_file(path: str) -> None:
    try:
        if os.path.isfile(path):
            os.remove(path)
        control = path + ".aria2"
        if os.path.isfile(control):
            os.remove(control)
    except OSError:
        pass


async def wait_for_metadata(gid: str, status_msg) -> list:
    last_log = 0.0
    last_tail = 0.0
    for _ in range(400):
        status = rpc(
            "aria2.tellStatus", [gid, ["status", "files", "errorMessage"]]
        )
        st = status.get("status")
        if st in ("error", "removed"):
            raise DownloadError(status.get("errorMessage") or st)
        files = status.get("files") or []
        if files and any(int(f.get("length") or 0) > 0 for f in files):
            return files
        now = time.monotonic()
        if now - last_log >= 15:
            logger.info(
                "gid=%s: waiting for torrent metadata (status=%s, %d file entry/entries)",
                gid,
                st,
                len(files),
            )
            last_log = now
        if now - last_tail >= 90:
            log_aria2_tail(20)
            last_tail = now
        await asyncio.sleep(3)
    raise DownloadError("timed out while waiting for torrent metadata")


async def wait_for_complete(gid: str, status_msg, label: str) -> dict:
    last_report = 0.0
    last_log = 0.0
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
                ],
            ],
        )
        st = status.get("status")
        if st == "complete":
            logger.info("%s: download complete", label)
            return status
        if st in ("error", "removed"):
            logger.info("%s: download ended with status %s", label, st)
            raise DownloadError(status.get("errorMessage") or st)
        now = time.monotonic()
        total = int(status.get("totalLength") or 0)
        done = int(status.get("completedLength") or 0)
        if now - last_log >= 60 and total > 0:
            pct = done / total * 100
            speed = int(status.get("downloadSpeed") or 0) / 1024 / 1024
            logger.info(
                "%s: %s (%.1f%%) | %.1f / %.1f MB | %.2f MB/s",
                label,
                st,
                pct,
                done / 1024 / 1024,
                total / 1024 / 1024,
                speed,
            )
            last_log = now
        if now - last_report >= 10 and total > 0:
            pct = done / total * 100
            speed = int(status.get("downloadSpeed") or 0) / 1024 / 1024
            try:
                await status_msg.edit_text(
                    f"{label}: {pct:.1f}% | Speed: {speed:.2f} MB/s"
                )
            except Exception:
                pass
            last_report = now
        await asyncio.sleep(3)


async def send_file(bot, chat_id: int, path: str, size: int, source: str, label: str) -> None:
    name = os.path.basename(path)
    if not os.path.isfile(path):
        await bot.send_message(chat_id, f"{label}: file not found on disk.")
        return
    if size > MAX_SEND_SIZE:
        await bot.send_message(
            chat_id,
            f"{label}: {size / 1024 / 1024:.1f} MB exceeds the "
            f"{MAX_SEND_SIZE // 1024 // 1024} MB send limit.",
        )
        return
    logger.info("uploading to user %s: %s (%.1f MB)", chat_id, name, size / 1024 / 1024)
    start = time.monotonic()
    await bot.send_document(chat_id, document=path, filename=name)
    elapsed = time.monotonic() - start
    logger.info(
        "upload finished for user %s: %s (%.1f MB, %.1fs)",
        chat_id,
        name,
        size / 1024 / 1024,
        elapsed,
    )
    await bot.send_message(
        chat_id,
        f"Sent: {name}\nSize: {size / 1024 / 1024:.1f} MB\nSource: {source}",
    )


def add_download(magnet, torrent_path):
    if magnet:
        gid = rpc("aria2.addUri", [[magnet], {"dir": base_dir, "seed-time": "0"}])
        source = magnet
    else:
        with open(torrent_path, "rb") as f:
            torrent_b64 = base64.b64encode(f.read()).decode()
        gid = rpc(
            "aria2.addTorrent",
            [torrent_b64, {"dir": base_dir, "seed-time": "0"}],
        )
        source = f"torrent file: {os.path.basename(torrent_path)}"
    return gid, source


async def run_download(update: Update, magnet, torrent_path, status_msg) -> None:
    bot = update.get_bot()
    chat_id = update.effective_chat.id
    gid = None
    try:
        gid, source = add_download(magnet, torrent_path)
        state["gid"] = gid
        logger.info("gid=%s: added to aria2 (source: %.80s)", gid, source)

        files = None
        for attempt in range(1, 4):
            try:
                files = await wait_for_metadata(gid, status_msg)
                break
            except DownloadError as e:
                if attempt == 3 or "timed out" not in str(e):
                    raise
                logger.warning(
                    "metadata timeout on attempt %d, waiting 30s and retrying", attempt
                )
                try:
                    await status_msg.edit_text(
                        f"Metadata timeout (attempt {attempt}); retrying..."
                    )
                except Exception:
                    pass
                await asyncio.sleep(30)
                gid, source = add_download(magnet, torrent_path)
                state["gid"] = gid
                logger.info("gid=%s: re-added to aria2 (source: %.80s)", gid, source)
        total = len(files)
        logger.info("gid=%s: metadata ready, %d file(s) to download", gid, total)
        await status_msg.edit_text(
            f"Source: {source}\nFiles: {total}\nSending files one by one..."
        )

        for i, finfo in enumerate(files, start=1):
            index = finfo["index"]
            path = finfo["path"]
            size = int(finfo.get("length") or 0)
            name = os.path.basename(path)
            label = f"[{i}/{total}] {name}"
            logger.info(
                "file %d/%d: downloading %s (%.1f MB)", i, total, name, size / 1024 / 1024
            )
            rpc("aria2.changeOption", [gid, {"select-file": str(index)}])
            await wait_for_complete(gid, status_msg, label)
            await send_file(bot, chat_id, path, size, source, label)
            remove_file(path)

        try:
            await status_msg.delete()
        except Exception:
            pass
        await bot.send_message(
            chat_id, f"All {total} file(s) downloaded and sent."
        )
        logger.info("gid=%s: all %d file(s) sent", gid, total)
    except DownloadError as e:
        logger.warning("download failed: %s", e)
        log_aria2_tail()
        try:
            await status_msg.edit_text(f"Download failed: {e}")
        except Exception:
            pass
    except Exception as e:
        logger.exception("download failed")
        log_aria2_tail()
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
    if magnet:
        logger.info("magnet request from user %s: %s", update.effective_user.id, magnet)
        await update.message.reply_text(f"Received: {magnet}")
    else:
        logger.info(
            "torrent file request from user %s: %s",
            update.effective_user.id,
            os.path.basename(torrent_path),
        )
        await update.message.reply_text(
            f"Received: {os.path.basename(torrent_path)}"
        )
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
    logger.info("/status requested by user %s", update.effective_user.id)
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
    logger.info("/cancel requested by user %s", update.effective_user.id)
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
    torrent_path = os.path.join(
        tempfile.gettempdir(), f"upload_{int(time.time())}.torrent"
    )
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
