"""Postiz Telegram Bot - schedule posts to Postiz via chat.

Flow: /start -> source (upload/url) -> media -> caption -> product name -> product url
-> channels (multi) -> post time (now/auto/schedule) -> date+time -> confirm
-> submit to Postiz API + register product on per-channel bio landing page.
"""

import asyncio
import html
import json
import logging
import os
import re
import subprocess
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
LANDING_URL = os.environ.get("LANDING_URL", "").rstrip("/")  # https://bio-links.xxx.workers.dev
LANDING_SECRET = os.environ.get("LANDING_SECRET", "")
YTDLP_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE", "")
CF_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip() or next(
    (
        x.strip()
        for x in os.environ.get("CLOUDFLARE_ACCOUNT_IDS", "").split(",")
        if x.strip()
    ),
    "",
)
CF_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "@cf/openai/whisper")
CAPTION_MODEL = os.environ.get(
    "CAPTION_MODEL", "@cf/deepseek-ai/deepseek-v4.1-flash-1017"
)
AI_READY = bool(CF_ACCOUNT_ID and CF_API_TOKEN)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# conversation states
SOURCE, MEDIA, URL, CAPTION, PRODUCT_NAME, PRODUCT_URL, CHANNEL, MORE_CHANNEL, SCHEDULE, DATE, TIME, CONFIRM, AI_CAPTION = range(
    13
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


def register_product(channel, name, url):
    """Add product to a channel's bio landing page. Returns assigned number, or None."""
    if not LANDING_URL or not LANDING_SECRET:
        return None
    try:
        r = requests.post(
            f"{LANDING_URL}/{channel}/add",
            headers={"X-Webhook-Secret": LANDING_SECRET},
            json={"name": name, "url": url},
            timeout=30,
        )
        body = r.json()
        if r.status_code == 200 and body.get("ok"):
            return body["num"]
        log.error("landing /%s/add failed: %s %s", channel, r.status_code, body)
    except (requests.RequestException, ValueError) as e:
        log.error("landing /%s/add error: %s", channel, e)
    return None


def build_caption(caption, num, url):
    """Final caption: body + product tutorial + trailing hashtags at the very bottom.

    Only a *trailing* block of hashtags is moved (that's where users put them);
    hashtags in the middle of the text (e.g. 'Produk #1 terbaik') stay untouched.
    """
    body, trailing = caption, ""
    m = re.search(r"(?:#[\w]+[\s]*)+$", caption)
    if m:
        trailing = m.group(0).strip()
        body = caption[: m.start()].rstrip()
    tutorial = (
        f"Kamu bisa cek produknya di bio dengan cara:\n"
        f"1. Klik tautan di bio\n"
        f"2. Pilih produk nomor {num}\n"
        f"3. Produk bisa juga kamu cari melalui url {url}"
    )
    parts = [p for p in (body, tutorial, trailing) if p]
    return "\n\n".join(parts)


CAPTION_PROMPT = """Kamu adalah copywriter afiliasi Indonesia. Kamu menerima transkrip audio dari video produk. Tulis caption dalam Bahasa Indonesia dengan format TEPAT tiga blok dipisahkan satu baris kosong:

Blok 1: Nama produk — satu baris, singkat, spesifik. Sertakan merek jika disebut. Jika produk atau merek tidak bisa diidentifikasi dari transkrip, gunakan nama umum yang deskriptif (contoh: Tempat Sampah).
Blok 2: Caption — maksimal 3 paragraf pendek. Paragraf pertama harus punya hook yang menarik. Singkat, catchy, engaging, natural. Jangan pakai markdown.
Blok 3: Hashtag — 3 sampai 5 hashtag relevan, dipisahkan spasi, masing-masing diawali #.

Jangan tulis label "Nama Produk:", "Caption:", atau "Hashtag:". Jangan tambah teks lain di luar tiga blok."""


def cf_ai(model, body, content_type):
    """Run a Cloudflare Workers AI model. Returns result dict or None."""
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{model}"
    try:
        r = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {CF_API_TOKEN}",
                "Content-Type": content_type,
            },
            data=body,
            timeout=300,
        )
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log.error("cloudflare ai %s error: %s", model, e)
        return None
    if r.status_code != 200 or not isinstance(data, dict) or not data.get("success"):
        log.error("cloudflare ai %s failed: %s %s", model, r.status_code, str(data)[:300])
        return None
    return data.get("result")


def extract_audio(video_path):
    """Extract mono 16 kHz MP3 from a video. Returns path or None."""
    fd, name = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    out = Path(name)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(video_path),
                "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k",
                str(out),
            ],
            check=True,
            capture_output=True,
            timeout=600,
        )
    except subprocess.CalledProcessError as e:
        log.error(
            "ffmpeg failed for %s: %s",
            video_path,
            e.stderr.decode("utf-8", "replace")[-300:],
        )
        out.unlink(missing_ok=True)
        return None
    except (OSError, subprocess.SubprocessError) as e:
        log.error("ffmpeg error for %s: %s", video_path, e)
        out.unlink(missing_ok=True)
        return None
    if not out.exists() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        return None
    return out


def transcribe_video(video_path):
    """Whisper transcript of the video's audio, or None."""
    audio = extract_audio(video_path)
    if not audio:
        return None
    try:
        result = cf_ai(WHISPER_MODEL, audio.read_bytes(), "audio/mpeg")
        return ((result or {}).get("text") or "").strip() or None
    finally:
        audio.unlink(missing_ok=True)


def parse_caption(text):
    """Split the model output into (product name, caption, hashtags)."""
    parts = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    if len(parts) < 3:
        return None
    name_lines = parts[0].splitlines()
    if not name_lines:
        return None
    name = re.sub(r"^(nama produk|product name)\s*:\s*", "", name_lines[0].strip(), flags=re.I)
    caption = "\n\n".join(parts[1:-1])
    caption = re.sub(r"^caption\s*:\s*", "", caption, flags=re.I)
    hashtags = re.sub(
        r"^hashtags?\s*:\s*", "", " ".join(parts[-1].split()), flags=re.I
    )
    paras = [p.strip() for p in re.split(r"\n\s*\n", caption) if p.strip()]
    caption = "\n\n".join(paras[:3])
    tags = [t.strip(".,;") for t in hashtags.split() if t.strip(".,;")]
    hashtags = " ".join((t if t.startswith("#") else f"#{t}") for t in tags[:5])
    if not name or not caption or not hashtags:
        return None
    return name, caption, hashtags


def generate_caption(transcript):
    """Ask the LLM for (product name, caption, hashtags), or None."""
    body = json.dumps(
        {
            "messages": [
                {"role": "system", "content": CAPTION_PROMPT},
                {"role": "user", "content": "Transkrip:\n" + transcript},
            ],
            "max_tokens": 3000,
            "temperature": 0.7,
        }
    ).encode()
    result = cf_ai(CAPTION_MODEL, body, "application/json")
    try:
        text = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return parse_caption(text)


def generate_caption_from_video(video_path):
    """Transcribe a video and generate a caption. Returns parsed tuple or None."""
    transcript = transcribe_video(video_path)
    if not transcript:
        log.warning("no transcript for %s", video_path)
        return None
    words = len(transcript.split())
    if words < 8:
        log.warning("transcript too short for %s (%s words)", video_path, words)
        return None
    log.info("transcript %s: %s words", Path(video_path).name, words)
    return generate_caption(transcript)


def fetch_post_type(integration_id):
    """Default post_type from the channel's settings schema, or None if absent.

    Each provider has a different enum (Instagram: post/story, Facebook:
    post/story optional, others vary) — discovered live from
    /integration-settings so new channels work without code changes.
    Sent even when optional: explicit beats relying on provider defaults.
    """
    if integration_id in _settings_cache:
        return _settings_cache[integration_id]
    status, body = postiz("GET", f"/integration-settings/{integration_id}")
    pt = None
    if 200 <= status < 300 and isinstance(body, dict):
        try:
            out = body["output"]["settings"]
            props = out["properties"]
            if "post_type" in props:
                enum = props["post_type"].get("enum", [])
                pt = "post" if "post" in enum else (enum[0] if enum else None)
        except (KeyError, TypeError, IndexError):
            pt = None
    _settings_cache[integration_id] = pt
    return pt


WIB = timezone(timedelta(hours=7))  # UTC+7, all auto-slot rules are in WIB


def next_8am(now_wib):
    """Next 08:00 WIB: today if before 08:00, else tomorrow."""
    d = now_wib.replace(hour=8, minute=0, second=0, microsecond=0)
    if d <= now_wib:
        d += timedelta(days=1)
    return d


def clamp_auto_slot(dt, now_wib):
    """Apply posting window rules to a computed slot.

    - A missed/empty slot (already in the past) becomes now+5min.
    - 21:00 WIB is the last allowed slot; anything after it, or before
      08:00, moves to the next day's 08:00.
    """
    if dt <= now_wib:
        dt = now_wib + timedelta(minutes=5)
    if dt.hour < 8 or dt > dt.replace(hour=21, minute=0, second=0, microsecond=0):
        return next_8am(now_wib)
    return dt


def auto_date(integration_id):
    """Auto slot per channel, in the daily posting window (WIB).

    Rules: last post + 2h while that slot is still ahead. If the slot is
    empty/missed (including no history at all), post now+5min. 21:00 is
    the last slot; anything after it moves to the next day's 08:00.
    """
    now_wib = datetime.now(WIB)
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
    if latest is None:
        # no history: the slot is empty -> now+5min (window applies)
        return clamp_auto_slot(now_wib + timedelta(minutes=5), now_wib).astimezone(timezone.utc)
    slot = clamp_auto_slot(latest.astimezone(WIB) + timedelta(hours=2), now_wib)
    return slot.astimezone(timezone.utc)


def is_allowed(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True  # no restriction configured
    u = update.effective_user
    return u is not None and u.id in ALLOWED_USERS


BACK_ROW = [("◀️ Back", "back"), ("❌ Cancel", "cancel")]


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
        reply_markup=kb([BACK_ROW]),
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
        reply_markup=kb([[("✅ Done", "media:done")] + BACK_ROW]),
    )
    return MEDIA


async def maybe_ai_caption(update, context, q=None):
    """Generate a caption from the first video. Returns a state or None."""
    if not AI_READY:
        return None
    video = next(
        (p for p, kind in (context.user_data.get("media_files") or []) if kind == "video"),
        None,
    )
    if not video:
        return None
    msg = await (
        q.edit_message_text("🎧 Transcribing audio & generating caption…")
        if q
        else update.message.reply_text("🎧 Transcribing audio & generating caption…")
    )
    try:
        result = await asyncio.to_thread(generate_caption_from_video, video)
    except Exception:
        log.exception("ai caption failed")
        result = None
    if not result:
        await msg.edit_text(
            "✍️ <b>Send the caption / text content</b> for the post.\n"
            "Hashtags will be moved to the very bottom automatically.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb([[("Skip (no caption)", "cap:skip")] + BACK_ROW]),
        )
        return CAPTION
    name, caption, hashtags = result
    context.user_data["ai_product_name"] = name
    context.user_data["ai_caption"] = caption
    context.user_data["ai_hashtags"] = hashtags
    preview = f"{name}\n\n{caption}\n\n{hashtags}"
    await msg.edit_text(
        "🤖 <b>Generated caption</b>\n\n<pre>" + html.escape(preview) + "</pre>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb(
            [
                [("✅ Use this", "ai:use")],
                [("✏️ Edit manually", "ai:edit"), ("❌ Cancel", "cancel")],
            ]
        ),
    )
    return AI_CAPTION


async def media_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not context.user_data["media_files"]:
        await q.edit_message_text("No media yet — send at least one photo/video.")
        return MEDIA
    state = await maybe_ai_caption(update, context, q)
    if state:
        return state
    await q.edit_message_text(
        "✍️ <b>Send the caption / text content</b> for the post.\n"
        "Hashtags will be moved to the very bottom automatically.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb([[("Skip (no caption)", "cap:skip")] + BACK_ROW]),
    )
    return CAPTION


def ask_product_name(update_or_q, context, is_query=False):
    """Send the product-name prompt. Returns PRODUCT_NAME."""
    text = (
        "🛍 <b>Product name?</b>\n"
        "Short name shown on the bio page (e.g. <i>Sarung Tangan HP</i>)."
    )
    markup = kb([[("Skip — no product", "pn:skip")] + BACK_ROW])
    if is_query:
        return update_or_q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    return update_or_q.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def caption_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["caption"] = update.message.text or ""
    await ask_product_name(update.message, context)
    return PRODUCT_NAME


async def caption_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["caption"] = ""
    await ask_product_name(q, context, is_query=True)
    return PRODUCT_NAME


async def ai_caption_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ud = context.user_data
    if q.data == "ai:use":
        ud["product_name"] = ud.pop("ai_product_name")
        ud["caption"] = ud.pop("ai_caption") + "\n\n" + ud.pop("ai_hashtags")
        await q.edit_message_text(
            "🔗 <b>Send the affiliate URL</b> (Shopee/Tokopedia/etc.)\n"
            "Or tap Skip if there is no product link.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb([[("Skip — no product link", "pu:skip")], BACK_ROW]),
        )
        return PRODUCT_URL
    ud.pop("ai_product_name", None)
    ud.pop("ai_caption", None)
    ud.pop("ai_hashtags", None)
    await q.edit_message_text(
        "✍️ <b>Send the caption / text content</b> for the post.\n"
        "Hashtags will be moved to the very bottom automatically.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb([[("Skip (no caption)", "cap:skip")] + BACK_ROW]),
    )
    return CAPTION


async def product_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["product_name"] = (update.message.text or "").strip()
    await update.message.reply_text(
        "🔗 <b>Send the affiliate URL</b> (Shopee/Tokopedia/etc.)",
        parse_mode=ParseMode.HTML,
        reply_markup=kb([BACK_ROW]),
    )
    return PRODUCT_URL


async def product_name_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["product_name"] = None
    context.user_data["product_url"] = None
    return await ask_channels(update, context, q)


async def product_url_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()
    if not re.match(r"^https?://", url):
        await update.message.reply_text("⚠️ Send a valid URL starting with http(s)://")
        return PRODUCT_URL
    context.user_data["product_url"] = url
    return await ask_channels(update, context)


async def product_url_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["product_name"] = None
    context.user_data["product_url"] = None
    return await ask_channels(update, context, q)


async def get_integrations():
    status, body = postiz("GET", "/integrations")
    if not (200 <= status < 300) or not isinstance(body, list):
        log.error("integrations failed: %s %s", status, body)
        return []
    return [i for i in body if not i.get("disabled")]


def channel_keyboard(ud):
    """Channel list with ✅ checklist marks, Select All (bottom), Done/Cancel."""
    chosen = ud.get("chosen", [])
    ints = ud.get("integrations", [])
    all_selected = len(chosen) == len(ints) and bool(ints)
    rows = []
    for i in ints:
        mark = "✅ " if i["id"] in chosen else ""
        rows.append([(f"{mark}{i['name']} ({i['identifier']})", f"ch:{i['id']}")])
    rows.append([("🌐 " + ("✅ " if all_selected else "") + "All channels", "ch:all")])
    rows.append([("✅ Done — continue", "ch:done")])
    rows.append(BACK_ROW)
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
        ints = context.user_data.get("integrations", [])
        all_selected = len(chosen) == len(ints) and bool(ints)
        names = ", ".join(
            i["name"] for i in ints if i["id"] in chosen
        )
        if all_selected:
            # every channel already selected — no point asking to add more
            await context.bot.send_message(
                q.message.chat_id,
                f"✅ Channels: <b>all {len(chosen)}</b>",
                parse_mode=ParseMode.HTML,
            )
            await context.bot.send_message(
                q.message.chat_id,
                "🕒 <b>When to post?</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=kb(
                    [
                        [("🚀 Post immediately", "t:now")],
                        [("🤖 Auto — next slot per channel", "t:auto")],
                        [("📅 Schedule", "t:schedule")],
                    ] + [BACK_ROW]
                ),
            )
            return SCHEDULE
        await context.bot.send_message(
            q.message.chat_id,
            f"✅ Channels: <b>{names}</b>\n\n➕ Add another channel?",
            parse_mode=ParseMode.HTML,
            reply_markup=kb([[("➕ Add channel", "more:yes"), ("✅ No, continue", "more:no")], BACK_ROW]),
        )
        return MORE_CHANNEL
    ch_id = q.data.split(":")[1]
    chosen = context.user_data.setdefault("chosen", [])
    ints = context.user_data.get("integrations", [])
    if ch_id == "all":
        # toggle all: on if not everything selected, off if it is
        if len(chosen) == len(ints) and ints:
            chosen.clear()
            await q.answer("All cleared")
        else:
            chosen[:] = [i["id"] for i in ints]
            await q.answer(f"All {len(ints)} selected")
        try:
            await q.edit_message_reply_markup(reply_markup=channel_keyboard(context.user_data))
        except BadRequest:
            pass
        return CHANNEL
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
            ] + [BACK_ROW]
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
        reply_markup=kb([BACK_ROW]),
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
        reply_markup=kb([BACK_ROW]),
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
            f"🤖 {html.escape(name)}: {dt.astimezone(timezone(timedelta(hours=7))).strftime('%d %b %H:%M')} WIB"
            for cid, dt in ud["auto_dates"].items()
            for name in [next(i["name"] for i in ud["integrations"] if i["id"] == cid)]
        ]
        return "🤖 Auto (next slot):\n  " + "\n  ".join(lines)
    return f"📅 {ud['post_date'][:16].replace('T', ' ')} UTC"


def confirm_text(ud):
    names = ", ".join(
        html.escape(next(i["name"] for i in ud["integrations"] if i["id"] == c))
        for c in ud["chosen"]
    )
    media_desc = f"{len(ud['media_files'])} file(s)" if ud["media_files"] else "none"
    product = (
        f"🛍 Product: {html.escape(ud['product_name'])}\n"
        f"🔗 URL: {html.escape(ud['product_url'])}\n"
        if ud.get("product_name")
        else ""
    )
    caption = ud["caption"][:100] or "(none)"
    return (
        "📋 <b>Confirm your post:</b>\n\n"
        f"🖼 Media: {media_desc}\n"
        f"✍️ Caption: {html.escape(caption)}{'…' if len(ud['caption']) > 100 else ''}\n"
        f"{product}"
        f"📍 Channels: {names}\n"
        f"🕒 When: {when_text(ud)}"
    )


async def confirm_summary_msg(update, context):
    await update.message.reply_text(
        confirm_text(context.user_data),
        parse_mode=ParseMode.HTML,
        reply_markup=kb([[("✅ Confirm & Post", "go")] + BACK_ROW]),
    )
    return CONFIRM


async def confirm_summary(update, context, q):
    await q.edit_message_text(
        confirm_text(context.user_data),
        parse_mode=ParseMode.HTML,
        reply_markup=kb([[("✅ Confirm & Post", "go")] + BACK_ROW]),
    )
    return CONFIRM


def ytdlp_download(url: str, dest_dir: Path) -> Path | None:
    """Download video from URL using yt-dlp. Returns local file path or None.

    Instagram requires login cookies (anonymous access is rate-limited);
    YTDLP_COOKIES_FILE points to a Netscape-format cookies.txt, used for
    instagram.com URLs only.
    """
    opts = {
        "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
        "format": "mp4/bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": 90 * 1024 * 1024,
    }
    if "instagram.com" in url and YTDLP_COOKIES_FILE:
        if Path(YTDLP_COOKIES_FILE).exists():
            opts["cookiefile"] = YTDLP_COOKIES_FILE
        else:
            log.warning("YTDLP_COOKIES_FILE missing: %s", YTDLP_COOKIES_FILE)
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
    ident_by_id = {}
    for i in integrations:
        if i["id"] not in chosen:
            continue
        ident = i["identifier"]
        ident_by_id[i["id"]] = ident
        s = {"__type": ident}
        # post_type per channel: fetched from that channel's settings schema
        # (Instagram: post/story; other channels may have different enums)
        pt = fetch_post_type(i["id"])
        if pt:
            s["post_type"] = pt
        settings_by_id[i["id"]] = s

    # register product once per landing page (identifier), not per channel:
    # 5 Facebook pages share the /facebook page -> one entry, shared number
    num_by_cid = {}
    if ud.get("product_name") and ud.get("product_url"):
        num_by_ident = {}
        for cid in chosen:
            ident = ident_by_id[cid]
            if ident not in num_by_ident:
                num = register_product(ident, ud["product_name"], ud["product_url"])
                if not num:
                    return False, f"Bio page registration failed for {ident} — post aborted, nothing sent to Postiz"
                num_by_ident[ident] = num
            num_by_cid[cid] = num_by_ident[ident]

    # per-channel caption: tutorial line carries that channel's own product number
    caption_by_cid = {}
    for cid in chosen:
        if cid in num_by_cid:
            caption_by_cid[cid] = build_caption(ud["caption"], num_by_cid[cid], ud["product_url"])
        else:
            caption_by_cid[cid] = ud["caption"]

    def payload_for(cids, when, date):
        return {
            "type": when,
            "date": date,
            "shortLink": False,
            "tags": [],
            "posts": [
                {
                    "integration": {"id": cid},
                    "value": [{"content": caption_by_cid[cid], "image": uploaded}],
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
    if not re.match(
        r"^https?://(?:[a-z0-9-]+\.)*(?:instagram\.com|instagr\.am|tiktok\.com|youtu\.be|youtube\.com|facebook\.com|fb\.watch|twitter\.com|x\.com)/",
        url,
    ):
        await update.message.reply_text(
            "⚠️ Not a video-platform URL (Instagram/TikTok/YouTube/X/Facebook). "
            "Download only supports those."
        )
        return URL
    await update.message.reply_text("⏳ Downloading video… this can take a minute.")
    path = await asyncio.to_thread(ytdlp_download, url, DOWNLOAD_DIR)
    if not path:
        await update.message.reply_text(
            "❌ Download failed — the video may be private, deleted, or too long.\n"
            "Send another URL to retry, or tap Back."
        )
        return URL
    kind = "video" if path.suffix.lower() in (".mp4", ".webm", ".mov") else "image"
    context.user_data["media_files"].append((str(path), kind))
    await update.message.reply_text(
        f"✅ Video downloaded: <code>{path.name}</code> "
        f"({path.stat().st_size // 1024} KB).",
        parse_mode=ParseMode.HTML,
    )
    state = await maybe_ai_caption(update, context)
    if state:
        return state
    await update.message.reply_text(
        "✍️ <b>Send the caption</b> for this post.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb([[("Skip (no caption)", "cap:skip"), ("❌ Cancel", "cancel")]]),
    )
    return CAPTION


async def go_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Back button: re-prompt the previous step based on user_data marks."""
    q = update.callback_query
    await q.answer()
    ud = context.user_data
    if ud.get("media_files") is None:
        return None

    # where are we? same inference as start_from_url
    if ud.get("product_name") is not None and "chosen" not in ud:
        # at PRODUCT_URL (or PRODUCT_NAME unanswered) -> go back to caption
        ud.pop("product_name", None)
        ud.pop("product_url", None)
        await q.edit_message_text(
            "✍️ <b>Edit caption</b> — send the caption again.\n"
            "Hashtags will be moved to the very bottom automatically.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb([[("Skip (no caption)", "cap:skip")] + BACK_ROW]),
        )
        return CAPTION
    if "caption" in ud and "chosen" not in ud:
        # at CHANNEL not yet opened (came from product_url just answered) —
        # can't happen here; CHANNEL prompt already rendered -> handled below
        pass
    if "chosen" in ud and "when" not in ud:
        # channel selection done, at MORE_CHANNEL/SCHEDULE -> back to channels
        ud.pop("chosen", None)
        await q.edit_message_text(
            "📍 <b>Choose channel(s)</b> to post to:\n<i>Tap to select, tap again to deselect.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=channel_keyboard(ud),
        )
        return CHANNEL
    if "when" in ud and "post_date" not in ud:
        # at DATE/TIME -> back to schedule choice
        ud.pop("when", None)
        ud.pop("date", None)
        await q.edit_message_text(
            "🕒 <b>When to post?</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb(
                [
                    [("🚀 Post immediately", "t:now")],
                    [("🤖 Auto — next slot per channel", "t:auto")],
                    [("📅 Schedule", "t:schedule")],
                ] + [BACK_ROW],
            ),
        )
        return SCHEDULE
    if "post_date" in ud and "go" not in ud:
        # at CONFIRM -> back to schedule choice
        ud.pop("post_date", None)
        ud.pop("when", None)
        await q.edit_message_text(
            "🕒 <b>When to post?</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb(
                [
                    [("🚀 Post immediately", "t:now")],
                    [("🤖 Auto — next slot per channel", "t:auto")],
                    [("📅 Schedule", "t:schedule")],
                ] + [BACK_ROW],
            ),
        )
        return SCHEDULE
    if "caption" in ud:
        # at PRODUCT_NAME (caption answered, product not yet) -> back to media
        ud.pop("caption", None)
        if ud.get("media_files"):
            await q.edit_message_text(
                "🖼 Media kept: "
                f"{len(ud['media_files'])} file(s). Send more, or tap Done.",
                reply_markup=kb([[("✅ Done", "media:done")] + BACK_ROW]),
            )
            return MEDIA
        await q.edit_message_text(
            "🔗 <b>Send the URL</b> (Instagram Reels, TikTok, YouTube, etc.)\n"
            "I'll download the video on the backend.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb([BACK_ROW]),
        )
        ud["source"] = "url"
        return URL
    # at MEDIA/CAPTION entry (caption not yet) -> back to source choice
    ud.pop("media_files", None)
    ud.pop("source", None)
    await q.edit_message_text(
        "👋 Let's create a post.\n\n<b>Choose content source:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb([[("📷 Upload Media", "src:upload"), ("🔗 From URL (Reels/Video)", "src:url")]]),
    )
    return SOURCE


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
    """Smart entry: user pasted a video-platform URL without /start.

    Only fires for URLs the downloader supports (see VIDEO_URL_FILTER) —
    affiliate links (Shopee etc.) never trigger a download.
    Stateless guard: intent is inferred from user_data alone, so a lost
    conversation state never traps the user (fixes retry-after-download-fail).
    """
    if not is_allowed(update):
        await update.message.reply_text("Not authorized.")
        return ConversationHandler.END
    ud = context.user_data
    if ud.get("media_files") is None:
        # fresh start
        ud.clear()
        ud["media_files"] = []
        ud["source"] = "url"
        return await url_received(update, context)
    # flow already running — where does this URL belong?
    if ud.get("product_name") is not None and "chosen" not in ud:
        return await product_url_received(update, context)  # affiliate step
    if ud.get("source") == "url" and not ud["media_files"] and "caption" not in ud:
        # download step: first URL never landed (download failed) — retry it
        return await url_received(update, context)
    # any other step (media upload, caption, channel, time): not a URL step
    await update.message.reply_text(
        "⚠️ Flow already running — this step doesn't take a video URL. "
        "Finish the current step or /cancel."
    )
    return None  # not handled -> state handlers run


async def start_from_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Smart entry: user sent media without /start — skip source step.

    Guard: never hijack an in-flight conversation (same reason as
    start_from_url — e.g. media added during the media step itself).
    """
    if context.user_data.get("media_files") is not None:
        return None  # conversation already active — let state handlers run
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
    # smart entry only for video platforms the downloader supports;
    # affiliate links (shopee etc.) fall through and never auto-download
    url_filter = filters.Regex(
        r"^\s*https?://(?:[a-z0-9-]+\.)*(?:instagram\.com|instagr\.am|tiktok\.com|youtu\.be|youtube\.com|facebook\.com|fb\.watch|twitter\.com|x\.com)/\S*\s*$"
    )
    back_handler = CallbackQueryHandler(go_back, pattern=r"^back$")
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
                back_handler,
            ],
            CAPTION: [
                CallbackQueryHandler(caption_skip, pattern=r"^cap:skip"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, caption_received),
                back_handler,
            ],
            PRODUCT_NAME: [
                CallbackQueryHandler(product_name_skip, pattern=r"^pn:skip"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, product_name_received),
                back_handler,
            ],
            PRODUCT_URL: [
                CallbackQueryHandler(product_url_skip, pattern=r"^pu:skip"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, product_url_received),
                back_handler,
            ],
            CHANNEL: [
                CallbackQueryHandler(channel_toggled, pattern=r"^ch:"),
                back_handler,
            ],
            MORE_CHANNEL: [CallbackQueryHandler(more_channel, pattern=r"^more:"), back_handler],
            SCHEDULE: [CallbackQueryHandler(time_chosen, pattern=r"^t:"), back_handler],
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, date_received), back_handler],
            TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, time_received), back_handler],
            CONFIRM: [CallbackQueryHandler(confirmed, pattern=r"^(go|cancel)$"), back_handler],
            AI_CAPTION: [CallbackQueryHandler(ai_caption_choice, pattern=r"^ai:")],
        },
        fallbacks=[
            CallbackQueryHandler(cancel, pattern=r"^cancel$"),
            CommandHandler("cancel", cancel),
            back_handler,
        ],
        allow_reentry=True,
    )
    app.add_handler(conv)
    # global cancel button safety
    app.add_handler(CallbackQueryHandler(cancel, pattern=r"^cancel$"))
    log.info("Bot starting (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
