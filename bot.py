import asyncio
import os
import logging
import re
import feedparser
import requests
import telegram
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler()])
log = logging.getLogger(__name__)

def require_env(key):
    val = os.environ.get(key)
    if not val:
        raise EnvironmentError(f"❌ Missing env var: {key}")
    return val

TELEGRAM_BOT_TOKEN = require_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = require_env("TELEGRAM_CHAT_ID")
TWITTER_USERNAMES  = [u.strip().lstrip("@") for u in require_env("TWITTER_USERNAMES").split(",") if u.strip()]
POLL_INTERVAL      = max(15, int(os.environ.get("POLL_INTERVAL_SECONDS", "30")))
INCLUDE_RETWEETS   = os.environ.get("INCLUDE_RETWEETS", "false").lower() == "true"
CUSTOM_PREFIX      = os.environ.get("CUSTOM_PREFIX", "🐦 New Tweet")

NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.poast.org",
    "https://nitter.1d4.us",
    "https://nitter.kavin.rocks",
    "https://nitter.privacyredirect.com",
]

seen_ids: set = set()
executor = ThreadPoolExecutor(max_workers=10)

def normalize_id(url: str) -> str:
    if not url:
        return ""
    url = url.split("#")[0].strip()
    match = re.search(r'/status/(\d+)', url)
    if match:
        return match.group(1)
    return url

def _fetch_sync(username: str):
    last_error = None
    for instance in NITTER_INSTANCES:
        url = f"{instance}/{username}/rss"
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}"
                continue
            feed = feedparser.parse(resp.content)
            if feed.entries and feed.feed.get("title"):
                log.info(f"✓ {instance} → @{username}")
                return feed.entries
            last_error = "Empty feed"
        except requests.exceptions.Timeout:
            log.warning(f"⏰ Timeout: {instance}")
            last_error = "Timeout"
        except Exception as e:
            last_error = str(e)
    raise Exception(f"All Nitter failed @{username}: {last_error}")

async def fetch_tweets(username: str):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _fetch_sync, username)

def extract_image(html: str):
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html)
    if not match:
        return None
    img_url = match.group(1)
    if img_url.startswith("/pic/"):
        import urllib.parse
        decoded = urllib.parse.unquote(img_url[5:])
        if not decoded.startswith("http"):
            decoded = "https://pbs.twimg.com/" + decoded
        return decoded
    elif img_url.startswith("http"):
        return img_url
    return None

def format_caption(entry, username: str) -> str:
    title   = entry.get("title", "")
    summary = entry.get("summary", "")

    text = summary
    text = re.sub(r'<img[^>]+>', '', text)
    text = re.sub(r'<a[^>]+>', '', text)
    text = re.sub(r'</a>', '', text)
    text = re.sub(r'<[^>]+>', '', text).strip()
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    text = re.sub(r'\n{3,}', '\n\n', text).strip()

    if not text or len(text) < 3:
        text = re.sub(r'<[^>]+>', '', title).strip()

    link = entry.get("link", "")
    link = link.split("#")[0]
    link = re.sub(r"https?://[^/]+/", "https://twitter.com/", link)

    try:
        dt = datetime(*entry.published_parsed[:6])
        dt_ist = dt + timedelta(hours=5, minutes=30)
        timestamp = dt_ist.strftime("%d %b %Y, %I:%M %p IST")
    except:
        timestamp = entry.get("published", "")

    return (
        f"{CUSTOM_PREFIX}\n\n"
        f"👤 @{username}\n"
        f"🕐 {timestamp}\n\n"
        f"{text}\n\n"
        f"View on X: {link}"
    )

async def send_to_telegram(entry, username: str):
    bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
    caption = format_caption(entry, username)
    image_url = extract_image(entry.get("summary", ""))

    if image_url:
        try:
            await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=image_url, caption=caption)
            log.info(f"✅ Photo sent @{username}")
            return
        except Exception as e:
            log.warning(f"⚠️ Photo failed: {e}")

    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=caption, disable_web_page_preview=False)
        log.info(f"✅ Text sent @{username}")
    except Exception as e:
        log.error(f"❌ Send failed @{username}: {e}")

async def check_user(username: str):
    try:
        entries = await fetch_tweets(username)
        new_count = 0
        for entry in reversed(entries):
            raw_uid = entry.get("link") or entry.get("id") or ""
            uid = normalize_id(raw_uid)
            if not uid or uid in seen_ids:
                continue
            if not INCLUDE_RETWEETS and "RT by" in entry.get("title", ""):
                seen_ids.add(uid)
                continue
            seen_ids.add(uid)
            await send_to_telegram(entry, username)
            new_count += 1
            await asyncio.sleep(1)
        if new_count:
            log.info(f"📨 @{username}: {new_count} new!")
        else:
            log.info(f"😴 @{username}: no new")
    except Exception as e:
        log.error(f"Error @{username}: {e}")

async def run():
    log.info("🚀 Twitter → Telegram Bot (Nitter RSS)")
    log.info(f"📋 Monitoring: {', '.join(TWITTER_USERNAMES)}")
    log.info(f"⏱  Poll every {POLL_INTERVAL}s")

    log.info("🌱 Seeding...")
    results = await asyncio.gather(*[fetch_tweets(u) for u in TWITTER_USERNAMES], return_exceptions=True)
    for username, result in zip(TWITTER_USERNAMES, results):
        if isinstance(result, Exception):
            log.warning(f"⚠️ Seed failed @{username}: {result}")
        else:
            for e in result:
                raw = e.get("link") or e.get("id") or ""
                uid = normalize_id(raw)
                if uid:
                    seen_ids.add(uid)
            log.info(f"✓ @{username} seeded {len(result)}")

    log.info("✅ Watching for NEW tweets!\n")
    while True:
        await asyncio.gather(*[check_user(u) for u in TWITTER_USERNAMES])
        log.info(f"💤 Sleeping {POLL_INTERVAL}s...")
        await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(run())
