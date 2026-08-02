# Torrent Download Telegram Bot

A simple repo that runs a Telegram bot: the user sends a **magnet** link or a **.torrent** file, the bot downloads the file with `aria2` and sends it back to the user. The bot runs inside **GitHub Actions**.

## Features

- Accepts magnet links and .torrent files
- Downloads and sends files **one by one** (e.g. a series is sent episode by episode)
- Reports the received link/source and file info (name, size) to the user
- Shows download progress and speed
- Cancel download with `/cancel`
- Restrict access with `ALLOWED_USER_IDS`
- Auto-stops after a configurable runtime (default 300 minutes)

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and get the token.
2. Push this project to GitHub.
3. Add these secrets under `Settings > Secrets and variables > Actions`:

| Name | Required | Description |
|---|---|---|
| `BOT_TOKEN` | Yes | Bot token from BotFather |
| `ALLOWED_USER_IDS` | No | Comma-separated numeric Telegram user IDs. Empty = everyone is allowed |
| `OWNER_CHAT_ID` | No | Chat ID that receives a shutdown notification |
| `MAX_RUNTIME_MINUTES` | No | Bot runtime in minutes (default 300) |

4. Open the `Actions` tab, select the **Telegram Torrent Bot** workflow and press **Run workflow**. The bot stays active for up to 5 hours.

## Usage

- `/start` — help
- Send a text message containing a magnet link
- Send a `.torrent` file
- `/status` — current download status
- `/cancel` — cancel the current download

## Limitations and notes

- GitHub Actions allows max 6 hours per run (the bot runs 5 hours, then stops).
- The workflow also runs automatically via cron every 6 hours. You can remove the `schedule` block from `bot.yml` if you don't want this.
- Telegram Bot API upload limit is **50 MB**; larger files only get an error message. A [Local Bot API server](https://core.telegram.org/bots/api#using-a-local-bot-api-server) raises this to 2 GB.
- Only one download at a time.
- Speed and peer availability depend on the GitHub runner network and may be limited.
- Only download content you are allowed to download.
