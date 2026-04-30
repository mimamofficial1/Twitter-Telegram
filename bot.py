import os
import asyncio
import feedparser
import aiohttp
import logging
from datetime import datetime, timezone
from telegram import Bot
from telegram.error import TelegramError
from telegram.constants import ParseMode

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── CONFIG ────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
TWITTER_USERNAMES  = [u.strip() for u in os.environ["TWITTER_USERNAMES"].split(",")]
CHECK_INTERVAL     = int(os.getenv("CHECK_INTERVAL", "120"))   # seconds

NITTER_INSTANCES = [
    "https://nitter.privacyredirect.com",
    "https://nitter.poast.org",
    "https://nitter.net",
    "https://nitter.1d4.us",
    "https://kavin.rocks",
    "https://nitter.catsarch.com",
]

# ─── STATE ─────────────────────────────────────────────────────────────────────
seen_ids: dict[str, set] = {u: set() for u in TWITTER_USERNAMES}


# ─── HELPERS ───────────────────────────────────────────────────────────────────
def get_rss_url(instance: str, username: str) -> str:
    return f"{instance.rstrip('/')}/{username}/rss"


def extract_image(entry) -> str | None:
    """Try to get the first image URL from an RSS entry."""
    # 1. media_thumbnail
    if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
        return entry.media_thumbnail[0].get("url")
    # 2. media_content
    if hasattr(entry, "media_content") and entry.media_content:
        for m in entry.media_content:
            if m.get("medium") == "image" or "image" in m.get("type", ""):
                return m.get("url")
    # 3. enclosures
    if hasattr(entry, "enclosures") and entry.enclosures:
        for enc in entry.enclosures:
            if "image" in enc.get("type", ""):
                return enc.get("href") or enc.get("url")
    # 4. <img> in summary HTML
    import re
    summary = getattr(entry, "summary", "") or ""
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', summary)
    if match:
        return match.group(1)
    return None


def clean_summary(summary: str) -> str:
    """Strip HTML tags from summary."""
    import re
    text = re.sub(r"<[^>]+>", "", summary)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    return text.strip()


async def fetch_feed(session: aiohttp.ClientSession, username: str) -> feedparser.FeedParserDict | None:
    """Try each Nitter instance until one works."""
    for instance in NITTER_INSTANCES:
        url = get_rss_url(instance, username)
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    content = await resp.text()
                    feed = feedparser.parse(content)
                    if feed.entries:
                        logger.info(f"✅ {username} → {instance} ({len(feed.entries)} entries)")
                        return feed
        except Exception as e:
            logger.warning(f"⚠️  {instance} failed for @{username}: {e}")
    logger.error(f"❌ All Nitter instances failed for @{username}")
    return None


async def send_tweet(bot: Bot, entry, username: str):
    """Send a single tweet as image+caption or text message."""
    tweet_text = clean_summary(getattr(entry, "summary", ""))
    tweet_url  = entry.link
    pub_date   = entry.get("published", "")

    # Try to parse a clean date
    try:
        dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        date_str = dt.strftime("%d %b %Y, %I:%M %p UTC")
    except Exception:
        date_str = pub_date

    caption = (
        f"🐦 <b>@{username}</b>  •  {date_str}\n\n"
        f"{tweet_text}\n\n"
        f"<a href='{tweet_url}'>🔗 View on X</a>"
    )

    image_url = extract_image(entry)

    try:
        if image_url:
            await bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=image_url,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        else:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=caption,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
        logger.info(f"📨 Sent tweet {entry.id} from @{username}")
    except TelegramError as e:
        # If image fails, fallback to text
        logger.warning(f"Image send failed ({e}), falling back to text")
        try:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=caption,
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as e2:
            logger.error(f"❌ Could not send tweet: {e2}")


# ─── MAIN LOOP ─────────────────────────────────────────────────────────────────
async def main():
    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    # Announce startup
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                "🤖 <b>Twitter→Telegram Bot started!</b>\n"
                f"👀 Tracking: {', '.join('@' + u for u in TWITTER_USERNAMES)}\n"
                f"🔄 Check interval: {CHECK_INTERVAL}s"
            ),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        logger.error(f"Startup message failed: {e}")

    logger.info(f"Bot started. Watching: {TWITTER_USERNAMES}")

    # Seed seen_ids with current tweets so we don't flood on first run
    async with aiohttp.ClientSession() as session:
        for username in TWITTER_USERNAMES:
            feed = await fetch_feed(session, username)
            if feed:
                for entry in feed.entries:
                    seen_ids[username].add(entry.id)
        logger.info("Initial seed done. Waiting for new tweets…")

    # Poll loop
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        async with aiohttp.ClientSession() as session:
            for username in TWITTER_USERNAMES:
                feed = await fetch_feed(session, username)
                if not feed:
                    continue
                new_entries = [e for e in feed.entries if e.id not in seen_ids[username]]
                # Send oldest first
                for entry in reversed(new_entries):
                    await send_tweet(bot, entry, username)
                    seen_ids[username].add(entry.id)
                    await asyncio.sleep(2)   # avoid rate limits


if __name__ == "__main__":
    asyncio.run(main())
