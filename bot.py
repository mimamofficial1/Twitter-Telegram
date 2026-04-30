import asyncio
import os
import logging
import re
import feedparser
import telegram
from datetime import datetime
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
TWITTER_USERNAMES  = [u.strip().lstrip("@") for u in require_env("TWITTER_USERNAMES").split(",")]
POLL_INTERVAL      = max(60, int(os.environ.get("POLL_INTERVAL_SECONDS", "120")))
INCLUDE_RETWEETS   = os.environ.get("INCLUDE_RETWEETS", "false").lower() == "true"
CUSTOM_PREFIX      = os.environ.get("CUSTOM_PREFIX", "🐦 *New Tweet*")

# xcancel is currently the most stable Nitter instance (99.99% uptime)
# rss.xcancel.com is their dedicated RSS subdomain
# Fallbacks included for redundancy
RSS_INSTANCES = [
    "https://rss.xcancel.com",       # Most stable — dedicated RSS subdomain
    "https://xcancel.com",           # Main xcancel instance
    "https://nitter.poast.org",      # Sometimes works
    "https://nitter.privacydev.net", # Occasional fallback
]

# xcancel requires a real browser User-Agent to serve RSS
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

seen_ids: set = set()
executor = ThreadPoolExecutor(max_workers=10)


def _fetch_sync(username: str):
    import urllib.request
    last_error = None
    for instance in RSS_INSTANCES:
        url = f"{instance}/{username}/rss"
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read()
            feed = feedparser.parse(content)
            if feed.entries and not feed.bozo:
                log.info(f"✓ {instance} → @{username} ({len(feed.entries)} entries)")
                return feed.entries
            # bozo means parse error / empty
            last_error = f"Empty or invalid feed from {instance}"
            log.warning(f"⚠️ {instance} → @{username}: {last_error}")
        except Exception as e:
            last_error = str(e)
            log.warning(f"⚠️ {instance} failed for @{username}: {e}")
    raise Exception(f"All RSS instances failed for @{username}: {last_error}")


async def fetch_tweets(username: str):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _fetch_sync, username)


def extract_image(html: str) -> str | None:
    """Extract first usable image URL from HTML content."""
    import urllib.parse
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html)
    if not match:
        return None
    img_url = match.group(1)
    # Nitter/xcancel proxy URLs: /pic/media%2F... or /pic/pbs.twimg.com%2F...
    if img_url.startswith("/pic/"):
        encoded = img_url[5:]
        decoded = urllib.parse.unquote(encoded)
        if decoded.startswith("pbs.twimg.com") or decoded.startswith("video.twimg.com"):
            return "https://" + decoded
        if decoded.startswith("http"):
            return decoded
        return "https://pbs.twimg.com/" + decoded
    if img_url.startswith("http"):
        return img_url
    return None


def format_caption(entry, username: str) -> str:
    """Format tweet as Telegram caption (Markdown)."""
    summary = entry.get("summary", entry.get("title", ""))
    summary = re.sub(r'<img[^>]+>', '', summary)
    summary = re.sub(r'<[^>]+>', '', summary).strip()
    summary = (summary
               .replace("&amp;", "&")
               .replace("&lt;", "<")
               .replace("&gt;", ">")
               .replace("&quot;", '"')
               .replace("&#39;", "'"))
    summary = re.sub(r'\n{3,}', '\n\n', summary).strip()

    link = entry.get("link", "")
    # Normalise to twitter.com link
    link = re.sub(r"https?://[^/]+/", "https://twitter.com/", link)

    try:
        dt = datetime(*entry.published_parsed[:6])
        timestamp = dt.strftime("%d %b %Y, %I:%M %p UTC")
    except Exception:
        timestamp = entry.get("published", "")

    return (
        f"{CUSTOM_PREFIX}\n\n"
        f"👤 *@{username}*\n"
        f"🕐 {timestamp}\n\n"
        f"{summary}\n\n"
        f"[View on X ↗]({link})"
    )


async def send_to_telegram(entry, username: str):
    bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
    caption = format_caption(entry, username)
    summary_html = entry.get("summary", "")
    image_url = extract_image(summary_html)

    try:
        if image_url:
            try:
                await bot.send_photo(
                    chat_id=TELEGRAM_CHAT_ID,
                    photo=image_url,
                    caption=caption,
                    parse_mode="Markdown"
                )
                log.info("✅ Sent with image!")
                return
            except Exception as img_err:
                log.warning(f"⚠️ Image send failed ({img_err}), falling back to text...")

        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=caption,
            parse_mode="Markdown",
            disable_web_page_preview=False
        )
        log.info("✅ Sent as text!")
    except Exception as e:
        log.error(f"❌ Telegram error: {e}")


async def check_user(username: str):
    try:
        entries = await fetch_tweets(username)
        new_count = 0
        for entry in reversed(entries):
            uid = entry.get("id") or entry.get("link")
            if uid in seen_ids:
                continue
            if not INCLUDE_RETWEETS and "RT by" in entry.get("title", ""):
                seen_ids.add(uid)
                continue
            await send_to_telegram(entry, username)
            seen_ids.add(uid)
            new_count += 1
            await asyncio.sleep(1)
        if new_count:
            log.info(f"📨 @{username}: {new_count} new tweet(s) sent")
        else:
            log.info(f"😴 @{username}: no new tweets")
    except Exception as e:
        log.error(f"Error (@{username}): {e}")


async def run():
    log.info("🚀 Twitter → Telegram Bot (xcancel RSS mode)")
    log.info(f"📋 Monitoring: {', '.join(TWITTER_USERNAMES)}")
    log.info(f"⏱  Poll every {POLL_INTERVAL}s")

    log.info("🌱 Seeding existing tweets (won't re-send on startup)...")
    seed_results = await asyncio.gather(
        *[fetch_tweets(u) for u in TWITTER_USERNAMES],
        return_exceptions=True
    )
    for username, result in zip(TWITTER_USERNAMES, seed_results):
        if isinstance(result, Exception):
            log.warning(f"⚠️ Seed failed @{username}: {result}")
        else:
            for e in result:
                seen_ids.add(e.get("id") or e.get("link"))
            log.info(f"✓ @{username} seeded {len(result)} tweet IDs")

    log.info("✅ Watching for NEW tweets!\n")
    while True:
        await asyncio.gather(*[check_user(u) for u in TWITTER_USERNAMES])
        log.info(f"💤 Sleeping {POLL_INTERVAL}s...")
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run())
