# Postiz Telegram Bot

Telegram bot that creates and schedules posts on a self-hosted [Postiz](https://postiz.com) instance via its public API.

## Flow

`/start` → choose source (**Upload Media** or **URL** — Reels/TikTok/YouTube etc., downloaded with yt-dlp) → send media → caption → pick channel(s) (multi-select, from Postiz integrations) → **Post now** or **Schedule** (date + time, WIB) → confirm → bot uploads media to Postiz and creates the post.

## Setup (Ubuntu VM)

```bash
sudo mkdir -p /opt/postiz-bot && sudo chown ubuntu:ubuntu /opt/postiz-bot
cp bot.py requirements.txt /opt/postiz-bot/
cd /opt/postiz-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp postiz-bot.env .env   # then edit: real token, API key, ALLOWED_USER_IDS
sudo cp postiz-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now postiz-bot
journalctl -u postiz-bot -f
```

## Config (`.env`)

| Var | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `POSTIZ_URL` | `<your-postiz>/api/public/v1` |
| `POSTIZ_API_KEY` | Postiz → Settings → API key |
| `ALLOWED_USER_IDS` | Comma-separated Telegram user IDs; empty = anyone can use the bot |

Timezone for scheduling input is WIB (UTC+7); the API receives UTC.

Note: Instagram `post_type` is set to `video` when any video is in the media, `post` otherwise. Additional channels added to Postiz later work automatically — channels are listed live from the API.
