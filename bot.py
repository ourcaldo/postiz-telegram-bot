"""Postiz Telegram Bot - schedule posts to Postiz via chat.

Flow: /start -> source (upload/url) -> media -> caption -> channels (multi)
-> post time (now/schedule) -> date+time -> confirm -> submit to Postiz API.
"""

import asyncio
import logging
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yt_dlp
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s", level=logging.INFO
)
log = logging.getLogger("postiz-bot")

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
POSTIZ_URL = os.environ["POSTIZ_URL"].rstrip("/")  # https://host/api/public/v1
POSTIZ_API_KEY = os.environ["POSTIZ_API_KEY"]
ALLOWED_USERS = {
    int(u) for u in os.environ.get("ALLOWED_USER_IDS", "").split(",") if u.strip()
}
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/tmp/postiz-media"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# conversation states
SOURCE, MEDIA, URL, CAPTION, CHANNEL, MORE_CHANNEL, SCHEDULE, DATE, TIME, CONFIRM = range(
    10
)

MEDIA_GIF = "media.gif"


def kb(rows):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text, callback_data=data) for text, data in row] for row in rows]
    )


def postiz(method, path, payload=None, files=None):
    """Call Postiz public API. Returns (status, json)."""
    url = f"{POSTIZ_URL}{path}"
    headers = {"Authorization": POSTIZ_API_KEY}
    if files:
        r = requests.post(url, headers=headers, files=files, timeout=600)
    else:
        r = requests.request(
            method, url, headers=headers, json=payload, timeout=60
        )
    try:
        body = r.json()
    except ValueError:
        body = r.text
    return r.status_code, body


_settings_cache = {}


def fetch_post_type(integration_id):
    """Default post_type from the channel's settings schema, or None if not required.

    Each provider has a different enum (Instagram: post/story, others vary) —
    discovered live from /integration-settings so new channels work without code changes.
    """
    if integration_id in _settings_cache:
        return _settings_cache[integration_id]
    status, body = postiz("GET", f"/integration-settings/{integration_id}")
    pt = None
    if 200 <= status < 300 and isinstance(body, dict):
        try:
            out = body["output"]["settings"]
            props = out["properties"]
            if "post_type" in props and "post_type" in out.get("required", []):
                enum = props["post_type"].get("enum", [])
                pt = "post" if "post" in enum else (enum[0] if enum else None)
        except (KeyError, TypeError, IndexError):
            pt = None
    _settings_cache[integration_id] = pt
    return pt


def auto_date(integration_id):
    """Auto slot: +1h after the channel's latest published/scheduled post.

    Never in the past (min now+5min); now+1h if the channel has no history.
    """
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    end = (now + timedelta(days=90)).strftime("%Y-%m-%d")
    status, body = postiz("GET", f"/posts?startDate={start}&endDate={end}")
    latest = None
    if 200 <= status < 300 and isinstance(body, dict):
        for p in body.get("posts", []):
            if p.get("integration", {}).get("id") != integration_id:
                continue
            d = p.get("publishDate")
            if not d:
                continue
            try:
                dt = datetime.fromisoformat(d.replace("Z", "+00:00"))
            except ValueError:
                continue
            if latest is None or dt > latest:
                latest = dt
    base = latest or now
    return max(base + timedelta(hours=1), now + timedelta(minutes=5))


def is_allowed(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True  # no restriction configured
    u = update.effective_user
    return u is not None and u.id in ALLOWED_USERS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("Not authorized.")
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["media_files"] = []
    context.user_data["integrations"] = []
    await update.message.reply_text(
        "👋 Hi! Let's create a post.\n\n<b>Choose content source:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb([[("📷 Upload Media", "src:upload"), ("🔗 From URL (Reels/Video)", "src:url")]]),
    )
    return SOURCE


async def source_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    src = q.data.split(":")[1]
    context.user_data["source"] = src
    if src == "upload":
        await q.edit_message_text(
            "📷 <b>Send photo(s) or video(s)</b> — one at a time or as an album.\n"
            "Tap <b>Done</b> when finished.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb([[("✅ Done", "media:done"), ("❌ Cancel", "cancel")]]),
        )
        return MEDIA
    await q.edit_message_text(
        "🔗 <b>Send the URL</b> (Instagram Reels, TikTok, YouTube, etc.)\n"
        "I'll download the video on the backend.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb([[("❌ Cancel", "cancel")]]),
    )
    return URL


async def media_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    file = None
    kind = None
    if msg.photo:
        file = msg.photo[-1]  # largest size
        kind = "image"
    elif msg.video:
        file = msg.video
        kind = "video"
    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith(("image/", "video/")):
        file = msg.document
        kind = "video" if "video" in msg.document.mime_type else "image"
    if not file:
        await msg.reply_text("Please send a photo or video.")
        return MEDIA
    tg = await file.get_file()
    suffix = Path(getattr(file, "file_name", "") or ("mp4" if kind == "video" else "jpg")).suffix
    dest = DOWNLOAD_DIR / f"{msg.id}{suffix}"
    await tg.download_to_drive(dest)
    context.user_data["media_files"].append((str(dest), kind))
    await msg.reply_text(
        f"✅ Got {kind} #{len(context.user_data['media_files'])}. Send more or tap Done.",
        reply_markup=kb([[("✅ Done", "media:done"), ("❌ Cancel", "cancel")]]),
    )
    return MEDIA


async def media_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not context.user_data["media_files"]:
        await q.edit_message_text("No media yet — send at least one photo/video.")
        return MEDIA
    await q.edit_message_text(
        "✍️ <b>Send the caption / text content</b> for the post.\n"
        "This will be posted along with the media.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb([[("Skip (no caption)", "cap:skip"), ("❌ Cancel", "cancel")]]),
    )
    return CAPTION


async def caption_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["caption"] = update.message.text or ""
    return await ask_channels(update, context)


async def caption_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["caption"] = ""
    return await ask_channels(update, context, q)


async def get_integrations():
    status, body = postiz("GET", "/integrations")
    if not (200 <= status < 300) or not isinstance(body, list):
        log.error("integrations failed: %s %s", status, body)
        return []
    return [i for i in body if not i.get("disabled")]


def channel_keyboard(ud):
    """Channel list with ✅ checklist marks + Done/Cancel."""
    chosen = ud.get("chosen", [])
    rows = []
    ints = ud.get("integrations", [])
    # one channel per row, marked when selected
    for i in ints:
        mark = "✅ " if i["id"] in chosen else ""
        rows.append([(f"{mark}{i['name']} ({i['identifier']})", f"ch:{i['id']}")])
    rows.append([("✅ Done — continue", "ch:done"), ("❌ Cancel", "cancel")])
    return kb(rows)


async def ask_channels(update, context, q=None):
    integrations = await get_integrations()
    if not integrations:
        text = "⚠️ No channels connected in Postiz."
        if q:
            await q.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return ConversationHandler.END
    context.user_data["integrations"] = integrations
    context.user_data["chosen"] = []
    text = "📍 <b>Choose channel(s)</b> to post to:\n<i>Tap to select, tap again to deselect.</i>"
    if q:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=channel_keyboard(context.user_data))
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=channel_keyboard(context.user_data))
    return CHANNEL


async def channel_toggled(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "ch:done":
        chosen = context.user_data.get("chosen", [])
        if not chosen:
            await q.answer("Select at least one channel!", show_alert=True)
            return CHANNEL
        names = ", ".join(
            i["name"] for i in context.user_data["integrations"] if i["id"] in chosen
        )
        await context.bot.send_message(
            q.message.chat_id,
            f"✅ Channels: <b>{names}</b>\n\n➕ Add another channel?",
            parse_mode=ParseMode.HTML,
            reply_markup=kb([[("➕ Add channel", "more:yes"), ("✅ No, continue", "more:no")]]),
        )
        return MORE_CHANNEL
    ch_id = q.data.split(":")[1]
    chosen = context.user_data.setdefault("chosen", [])
    if ch_id in chosen:
        chosen.remove(ch_id)
        await q.answer("Removed")
    else:
        chosen.append(ch_id)
        await q.answer("Added")
    # re-render with updated ✅ marks; no-op edit is fine to swallow
    try:
        await q.edit_message_reply_markup(reply_markup=channel_keyboard(context.user_data))
    except BadRequest:
        pass
    return CHANNEL


async def more_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "more:yes":
        # back to channel list, checklist preserved
        await q.edit_message_text(
            "📍 <b>Choose channel(s)</b> to post to:\n<i>Tap to select, tap again to deselect.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=channel_keyboard(context.user_data),
        )
        return CHANNEL
    await q.edit_message_text(
        "🕒 <b>When to post?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb(
            [
                [("🚀 Post immediately", "t:now")],
                [("🤖 Auto — next slot per channel", "t:auto")],
                [("📅 Schedule", "t:schedule")],
                [("❌ Cancel", "cancel")],
            ]
        ),
    )
    return SCHEDULE


async def time_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    t = q.data.split(":")[1]
    context.user_data["when"] = t
    if t == "now":
        context.user_data["post_date"] = datetime.now(timezone.utc).isoformat()
        return await confirm_summary(update, context, q)
    if t == "auto":
        # per-channel auto date: +1h after that channel's last post/schedule
        await q.edit_message_text("⏳ Finding next slot per channel…")
        auto_dates = {}
        for cid in context.user_data["chosen"]:
            auto_dates[cid] = await asyncio.to_thread(auto_date, cid)
        context.user_data["auto_dates"] = auto_dates
        return await confirm_summary(update, context, q)
    await q.edit_message_text(
        "📅 <b>Send the date</b> (e.g. <code>2026-10-20</code> or <code>tomorrow</code>)",
        parse_mode=ParseMode.HTML,
        reply_markup=kb([[("❌ Cancel", "cancel")]]),
    )
    return DATE


def parse_date(text, tz=None):
    text = text.strip().lower()
    now = datetime.now(tz)
    if text in ("today", "hari ini"):
        return now.date()
    if text in ("tomorrow", "besok"):
        return (now + timedelta(days=1)).date()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", text)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


async def date_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = parse_date(update.message.text, timezone.utc)
    if not d:
        await update.message.reply_text("⚠️ Can't parse date. Use YYYY-MM-DD.")
        return DATE
    context.user_data["date"] = d
    await update.message.reply_text(
        f"📅 {d.isoformat()}\n\n⏰ <b>Send the time</b> (HH:MM, WIB/UTC+7)",
        parse_mode=ParseMode.HTML,
        reply_markup=kb([[("❌ Cancel", "cancel")]]),
    )
    return TIME


async def time_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = re.match(r"^(\d{1,2}):(\d{2})$", update.message.text.strip())
    if not m:
        await update.message.reply_text("⚠️ Use HH:MM format.")
        return TIME
    hh, mm = int(m[1]), int(m[2])
    if hh > 23 or mm > 59:
        await update.message.reply_text("⚠️ Hour must be 00-23, minute 00-59.")
        return TIME
    d = context.user_data["date"]
    # ponytail: hard-coded WIB (UTC+7); add TZ offset config if needed elsewhere
    dt = datetime(d.year, d.month, d.day, hh, mm, tzinfo=timezone(timedelta(hours=7)))
    if dt < datetime.now(timezone.utc):
        await update.message.reply_text("⚠️ That time is in the past. Try again.")
        return TIME
    context.user_data["post_date"] = dt.astimezone(timezone.utc).isoformat()
    return await confirm_summary_msg(update, context)


def when_text(ud):
    """Human summary of the post timing, incl. per-channel auto slots."""
    if ud["when"] == "now":
        return "🚀 Immediately"
    if ud["when"] == "auto":
        lines = [
            f"🤖 {name}: {dt.astimezone(timezone(timedelta(hours=7))).strftime('%d %b %H:%M')} WIB"
            for cid, dt in ud["auto_dates"].items()
            for name in [next(i["name"] for i in ud["integrations"] if i["id"] == cid)]
        ]
        return "🤖 Auto (next slot):\n  " + "\n  ".join(lines)
    return f"📅 {ud['post_date'][:16].replace('T', ' ')} UTC"


def confirm_text(ud):
    names = ", ".join(
        next(i["name"] for i in ud["integrations"] if i["id"] == c)
        for c in ud["chosen"]
    )
    media_desc = f"{len(ud['media_files'])} file(s)" if ud["media_files"] else "none"
    return (
        "📋 <b>Confirm your post:</b>\n\n"
        f"🖼 Media: {media_desc}\n"
        f"✍️ Caption: {ud['caption'][:100] or '(none)'}{'…' if len(ud['caption']) > 100 else ''}\n"
        f"📍 Channels: {names}\n"
        f"🕒 When: {when_text(ud)}"
    )


async def confirm_summary_msg(update, context):
    await update.message.reply_text(
        confirm_text(context.user_data),
        parse_mode=ParseMode.HTML,
        reply_markup=kb([[("✅ Confirm & Post", "go"), ("❌ Cancel", "cancel")]]),
    )
    return CONFIRM


async def confirm_summary(update, context, q):
    await q.edit_message_text(
        confirm_text(context.user_data),
        parse_mode=ParseMode.HTML,
        reply_markup=kb([[("✅ Confirm & Post", "go"), ("❌ Cancel", "cancel")]]),
    )
    return CONFIRM


def ytdlp_download(url: str, dest_dir: Path) -> Path | None:
    """Download video from URL using yt-dlp. Returns local file path or None."""
    opts = {
        "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
        "format": "mp4/bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": 90 * 1024 * 1024,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))
            if path.suffix != ".mp4":
                # merged or converted filename guess
                guess = path.with_suffix(".mp4")
                if guess.exists():
                    path = guess
            if path.exists():
                return path
        return None
    except Exception as e:
        log.error("ytdlp failed for %s: %s", url, e)
        return None


async def confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Processing…")
    ud = context.user_data
    await q.edit_message_text("⏳ Processing — uploading media & creating post…")
    try:
        ok, msg = await asyncio.to_thread(sync_do_post, ud)
    except Exception as e:
        log.exception("post failed")
        ok, msg = False, f"Error: {e}"
    if ok:
        await context.bot.send_message(
            q.message.chat_id,
            f"🎉 <b>Post created!</b>\nPost ID: <code>{msg}</code>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await context.bot.send_message(
            q.message.chat_id,
            f"❌ <b>Failed:</b> <code>{msg}</code>",
            parse_mode=ParseMode.HTML,
        )
    context.user_data.clear()
    return ConversationHandler.END


def sync_do_post(ud):
    """Synchronous posting pipeline, run in thread."""
    caption = ud["caption"]
    media_files = ud["media_files"]
    chosen = ud["chosen"]
    integrations = ud["integrations"]

    uploaded = []
    for path, _kind in media_files:
        with open(path, "rb") as f:
            status, body = postiz("POST", "/upload", files={"file": f})
        if not (200 <= status < 300) or not isinstance(body, dict):
            log.error("postiz /upload failed: %s %s", status, body)
            return False, f"Upload failed ({status}): {body}"
        uploaded.append({"id": body["id"], "path": body["path"]})
        log.info("uploaded %s -> %s", path, body["id"])

    settings_by_id = {}
    for i in integrations:
        if i["id"] not in chosen:
            continue
        ident = i["identifier"]
        s = {"__type": ident}
        # post_type per channel: fetched from that channel's settings schema
        # (Instagram: post/story; other channels may have different enums)
        pt = fetch_post_type(i["id"])
        if pt:
            s["post_type"] = pt
        settings_by_id[i["id"]] = s

    def payload_for(cids, when, date):
        return {
            "type": when,
            "date": date,
            "shortLink": False,
            "tags": [],
            "posts": [
                {
                    "integration": {"id": cid},
                    "value": [{"content": caption, "image": uploaded}],
                    "settings": settings_by_id[cid],
                }
                for cid in cids
            ],
        }

    if ud["when"] == "auto":
        # per-channel date: one API call per channel, each with its own auto slot
        results = []
        for cid, dt in ud["auto_dates"].items():
            status, body = postiz("POST", "/posts", payload_for([cid], "schedule", dt.isoformat()))
            if not (200 <= status < 300):
                log.error("postiz /posts (auto %s) failed: %s %s", cid, status, body)
                return False, f"Postiz error ({status}): {body}"
            if isinstance(body, list):
                results.extend(str(p.get("postId", "?")) for p in body)
            else:
                results.append(str(body))
        return True, ", ".join(results)

    payload = payload_for(chosen, ud["when"], ud["post_date"])
    status, body = postiz("POST", "/posts", payload)
    if not (200 <= status < 300):
        log.error("postiz /posts failed: %s %s", status, body)
        return False, f"Postiz error ({status}): {body}"
    if isinstance(body, list):
        ids = ", ".join(str(p.get("postId", "?")) for p in body)
    else:
        ids = str(body)
    return True, ids


async def url_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()
    if not re.match(r"^https?://", url):
        await update.message.reply_text("⚠️ Send a valid URL starting with http(s)://")
        return URL
    await update.message.reply_text("⏳ Downloading video… this can take a minute.")
    path = await asyncio.to_thread(ytdlp_download, url, DOWNLOAD_DIR)
    if not path:
        await update.message.reply_text(
            "❌ Download failed. Check the URL, or the video may be private/too long."
        )
        return URL
    kind = "video" if path.suffix.lower() in (".mp4", ".webm", ".mov") else "image"
    context.user_data["media_files"].append((str(path), kind))
    await update.message.reply_text(
        f"✅ Video downloaded: <code>{path.name}</code> "
        f"({path.stat().st_size // 1024} KB).\n\n"
        "✍️ <b>Send the caption</b> for this post.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb([[("Skip (no caption)", "cap:skip"), ("❌ Cancel", "cancel")]]),
    )
    return CAPTION


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    context.user_data.clear()
    text = "❌ Cancelled. Send /start to begin again."
    if q:
        await q.answer()
        await q.edit_message_text(text)
    elif update.message:
        await update.message.reply_text(text)
    return ConversationHandler.END


async def start_from_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Smart entry: user pasted a URL without /start — skip source step."""
    if not is_allowed(update):
        await update.message.reply_text("Not authorized.")
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["media_files"] = []
    context.user_data["source"] = "url"
    return await url_received(update, context)


async def start_from_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Smart entry: user sent media without /start — skip source step."""
    if not is_allowed(update):
        await update.message.reply_text("Not authorized.")
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["media_files"] = []
    context.user_data["source"] = "upload"
    await update.message.reply_text(
        "📷 Got media. Send more, or tap Done when finished.",
        reply_markup=kb([[("✅ Done", "media:done"), ("❌ Cancel", "cancel")]]),
    )
    return await media_received(update, context)


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    url_filter = filters.Regex(r"^\s*https?://\S+\s*$")
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(url_filter & ~filters.COMMAND, start_from_url),
            MessageHandler(filters.PHOTO | filters.VIDEO, start_from_media),
        ],
        states={
            SOURCE: [CallbackQueryHandler(source_chosen, pattern=r"^src:")],
            MEDIA: [
                CallbackQueryHandler(media_done, pattern=r"^media:done"),
                MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.ALL, media_received),
            ],
            URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, url_received),
            ],
            CAPTION: [
                CallbackQueryHandler(caption_skip, pattern=r"^cap:skip"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, caption_received),
            ],
            CHANNEL: [CallbackQueryHandler(channel_toggled, pattern=r"^ch:")],
            MORE_CHANNEL: [CallbackQueryHandler(more_channel, pattern=r"^more:")],
            SCHEDULE: [CallbackQueryHandler(time_chosen, pattern=r"^t:")],
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, date_received)],
            TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, time_received)],
            CONFIRM: [CallbackQueryHandler(confirmed, pattern=r"^(go|cancel)$")],
        },
        fallbacks=[CallbackQueryHandler(cancel, pattern=r"^cancel$"), CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    # global cancel button safety
    app.add_handler(CallbackQueryHandler(cancel, pattern=r"^cancel$"))
    log.info("Bot starting (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
