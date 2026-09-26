"""
social_watch.py — `!register`'s other half: polls each registered account's
public profile/feed and posts an embed in SOCIAL_POST_CHANNEL_ID when it has
something new.

No developer API key for any of these platforms — YouTube's public RSS feed
and Twitch's public GraphQL endpoint (the same one twitch.tv's own web
client calls, under its well-known public client id) are genuinely
reliable. Instagram is read through an undocumented public endpoint that
can be throttled or changed without notice, especially from a datacenter
IP — which is why this is wired into quotes_watch_local.py (a real
residential machine), not the GitHub Actions loop. Twitter/X no longer
allows any of this without a login at all, so it only works if
SOCIAL_NITTER_BASE points at a Nitter mirror you trust (most public ones
are dead or unreliable — self-hosting is the only durable option). TikTok
isn't implemented: its post-list endpoint requires a signed request from a
real browser session and just returns empty otherwise (tested), so
handles are stored but never polled.

A brand-new registration never dumps someone's whole history into the
channel: the first check for a handle just seeds the "last seen" cursor.

Every failure that isn't a routine transient blip (a handle a checker can
never resolve, an announce that fails to post) is logged both to the
Python logger and to activity_log.log() so it shows up in the control
panel, not just a local console someone has to be watching.
"""
from __future__ import annotations

import calendar
import email.utils
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Optional

import requests

import activity_log
import discord_roles
import social_profiles

logger = logging.getLogger("porygon.social_watch")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "social_watch_state.json")

POST_CHANNEL_ID = os.environ.get("SOCIAL_POST_CHANNEL_ID", "")
POLL_SECONDS = int(os.environ.get("SOCIAL_POLL_MINUTES", 15)) * 60
NITTER_BASE = os.environ.get("SOCIAL_NITTER_BASE", "").rstrip("/")

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}
# instagram.com's own web client sends this app id on every anonymous
# request — it's public and baked into their frontend JS, not a developer
# credential, but Instagram can still tighten what it unlocks at any time.
_IG_APP_ID = "936619743392459"
# Optional: Instagram increasingly 401s this endpoint for a logged-out
# request. Setting this to the `sessionid` cookie value from your OWN
# browser while logged into instagram.com (never a bot/developer
# credential) makes the request look like a normal logged-in page view and
# is meaningfully more reliable. Treat it like a password — anyone with it
# can act as your Instagram account.
_IG_SESSION_ID = os.environ.get("SOCIAL_IG_SESSIONID", "")
# twitch.tv's own web client sends this on every anonymous GQL request —
# it's public, baked into their frontend JS, not a developer credential.
_TWITCH_GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
_HTTP_TIMEOUT = 15

PLATFORM_LABEL = {"twitter": "Twitter/X", "instagram": "Instagram", "youtube": "YouTube", "twitch": "Twitch", "tiktok": "TikTok"}
PLATFORM_COLOR = {"twitter": 0x1D9BF0, "instagram": 0xE1306C, "youtube": 0xFF0000, "twitch": 0x9146FF, "tiktok": 0x000000}

# Reasons a registered platform can't actually be checked right now — shown
# to whoever hits it via !post, instead of a generic "not registered" or a
# silent poll asking them to pick from platforms that don't include the one
# they meant. Twitter/X's reason is computed (depends on NITTER_BASE);
# everything else here is a fixed, structural limitation.
_UNSUPPORTED_REASONS: dict[str, str] = {
    "tiktok": (
        "TikTok's post list needs a signed request from a real logged-in "
        "browser session, which this bot can't do — handles are stored for "
        "whenever that changes, but nothing gets checked yet."
    ),
}


def unsupported_reason(platform: str) -> Optional[str]:
    """Why `platform` can't be checked right now, or None if it's fully
    working. Shared by social_backup.py's `!post` to explain a rejection
    instead of just refusing."""
    if platform == "twitter" and not NITTER_BASE:
        return (
            "Twitter/X needs a trusted Nitter mirror configured "
            "(SOCIAL_NITTER_BASE), and none is set up right now."
        )
    return _UNSUPPORTED_REASONS.get(platform)

# A given (kind, key) only warns once — scraping hiccups (rate limits,
# transient blocks) are routine, not exceptional, for these platforms, and a
# repeat of the same failure every cycle would drown the log/panel. Also
# mirrors the warning into activity_log so it's visible on the control panel,
# not just a console someone has to be watching.
_warned_once: set[str] = set()

# Process-local scheduling — deliberately NOT persisted to disk. This module
# only ever runs inside the single long-lived quotes_watch_local.py process,
# so an in-memory cursor is enough, and it keeps a restart from mattering
# more than "check once immediately" while avoiding a git commit every cycle
# just to bump a timestamp.
_last_scan = 0.0


def is_configured() -> bool:
    return bool(POST_CHANNEL_ID)


def _load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Failed to load {STATE_FILE}: {e}")
        return {}


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _warn_once(key: str, message: str) -> None:
    if key not in _warned_once:
        _warned_once.add(key)
        logger.warning(message)
        activity_log.log(f"⚠️ {message}")


def _fit(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


# ---- platform checkers ----------------------------------------------------
# Each returns recent posts newest-first, as {"id", "url", "text", "image",
# "posted_at" (epoch seconds or None)}. Best-effort: any failure returns [].

_yt_channel_id_cache: dict[str, str] = {}
_YT_CHANNEL_RE = re.compile(r'"channelId":"(UC[\w-]{22})"')


def _resolve_youtube_channel_id(handle: str) -> Optional[str]:
    handle = handle.lstrip("@")
    if handle in _yt_channel_id_cache:
        return _yt_channel_id_cache[handle]
    if re.fullmatch(r"UC[\w-]{22}", handle):
        _yt_channel_id_cache[handle] = handle
        return handle
    try:
        resp = requests.get(f"https://www.youtube.com/@{handle}", headers=_HTTP_HEADERS, timeout=_HTTP_TIMEOUT)
        match = _YT_CHANNEL_RE.search(resp.text) if resp.status_code == 200 else None
        if match:
            _yt_channel_id_cache[handle] = match.group(1)
            return match.group(1)
    except requests.RequestException as e:
        logger.warning(f"YouTube channel lookup failed for @{handle}: {e}")
    return None


def _parse_iso8601(value: str) -> Optional[float]:
    """YouTube's Atom feed always reports UTC (the '+00:00'/'Z' suffix is
    just a format detail) — take the fixed-width prefix and interpret it as
    UTC directly, rather than mktime's local-timezone assumption."""
    if not value or len(value) < 19:
        return None
    try:
        return calendar.timegm(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def check_youtube(handle: str) -> list[dict]:
    channel_id = _resolve_youtube_channel_id(handle)
    if not channel_id:
        _warn_once(f"yt:{handle}", f"Couldn't resolve a YouTube channel id for @{handle}")
        return []
    try:
        resp = requests.get(
            "https://www.youtube.com/feeds/videos.xml",
            params={"channel_id": channel_id}, headers=_HTTP_HEADERS, timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return []
        ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
        entries = ET.fromstring(resp.content).findall("a:entry", ns)
    except (requests.RequestException, ET.ParseError) as e:
        logger.warning(f"YouTube feed fetch failed for @{handle}: {e}")
        return []

    posts = []
    for entry in entries:
        video_id = entry.findtext("yt:videoId", default="", namespaces=ns)
        link_el = entry.find("a:link", ns)
        if not video_id or link_el is None:
            continue
        posts.append({
            "id": video_id,
            "url": link_el.get("href", f"https://www.youtube.com/watch?v={video_id}"),
            "text": entry.findtext("a:title", default="", namespaces=ns),
            "image": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "posted_at": _parse_iso8601(entry.findtext("a:published", default="", namespaces=ns)),
        })
    return posts


def check_instagram(handle: str) -> list[dict]:
    handle = handle.lstrip("@")
    headers = {**_HTTP_HEADERS, "X-IG-App-ID": _IG_APP_ID, "Accept": "application/json"}
    cookies = {"sessionid": _IG_SESSION_ID} if _IG_SESSION_ID else None
    try:
        resp = requests.get(
            "https://www.instagram.com/api/v1/users/web_profile_info/",
            params={"username": handle}, headers=headers, cookies=cookies, timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            _warn_once(
                f"ig:{handle}",
                f"Instagram check for @{handle} returned {resp.status_code} — "
                "Instagram may be rate-limiting or blocking this endpoint",
            )
            return []
        edges = (
            resp.json().get("data", {}).get("user", {})
            .get("edge_owner_to_timeline_media", {}).get("edges", [])
        )
    except (requests.RequestException, ValueError) as e:
        logger.warning(f"Instagram check failed for @{handle}: {e}")
        return []

    posts = []
    for edge in edges:
        node = edge.get("node", {})
        shortcode = node.get("shortcode")
        if not shortcode:
            continue
        caption_edges = node.get("edge_media_to_caption", {}).get("edges", [])
        caption = caption_edges[0]["node"]["text"] if caption_edges else ""
        posts.append({
            "id": shortcode,
            "url": f"https://www.instagram.com/p/{shortcode}/",
            "text": caption,
            "image": node.get("display_url"),
            "posted_at": node.get("taken_at_timestamp"),
        })
    return posts


def check_twitter(handle: str) -> list[dict]:
    handle = handle.lstrip("@")
    if not NITTER_BASE:
        _warn_once("twitter:disabled", "SOCIAL_NITTER_BASE not set — Twitter/X checks are skipped")
        return []
    try:
        resp = requests.get(f"{NITTER_BASE}/{handle}/rss", headers=_HTTP_HEADERS, timeout=_HTTP_TIMEOUT)
        if resp.status_code != 200:
            _warn_once(f"tw:{handle}", f"Nitter check for @{handle} returned {resp.status_code}")
            return []
        items = ET.fromstring(resp.content).findall(".//item")
    except (requests.RequestException, ET.ParseError) as e:
        logger.warning(f"Twitter/Nitter check failed for @{handle}: {e}")
        return []

    posts = []
    for item in items:
        link = (item.findtext("link") or "").strip()
        if not link:
            continue
        posted_at = None
        pub_date = item.findtext("pubDate")
        if pub_date:
            try:
                posted_at = email.utils.parsedate_to_datetime(pub_date).timestamp()
            except Exception:
                pass
        posts.append({
            "id": link,
            "url": link,
            "text": item.findtext("title") or "",
            "image": None,
            "posted_at": posted_at,
        })
    return posts


_TWITCH_VIDEOS_QUERY = """
query {
  user(login: "%s") {
    videos(first: 5, sort: TIME) {
      edges {
        node {
          id
          title
          publishedAt
          previewThumbnailURL(width: 320, height: 180)
        }
      }
    }
  }
}
"""


def check_twitch(handle: str) -> list[dict]:
    """Most-recent VODs (the closest Twitch equivalent of "a new post" — a
    fresh livestream only becomes a checkable video once the broadcast
    itself has ended and Twitch has processed the VOD)."""
    handle = handle.lstrip("@").lower()
    try:
        resp = requests.post(
            "https://gql.twitch.tv/gql",
            headers={**_HTTP_HEADERS, "Client-Id": _TWITCH_GQL_CLIENT_ID, "Content-Type": "application/json"},
            json={"query": _TWITCH_VIDEOS_QUERY % handle}, timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            _warn_once(f"tv:{handle}", f"Twitch check for {handle} returned {resp.status_code}")
            return []
        user = resp.json().get("data", {}).get("user")
        if not user:
            _warn_once(f"tv:{handle}", f"No Twitch user found for '{handle}' — check the handle in !register")
            return []
        edges = user.get("videos", {}).get("edges", [])
    except (requests.RequestException, ValueError) as e:
        logger.warning(f"Twitch check failed for {handle}: {e}")
        return []

    posts = []
    for edge in edges:
        node = edge.get("node", {})
        video_id = node.get("id")
        if not video_id:
            continue
        thumb = node.get("previewThumbnailURL") or ""
        posts.append({
            "id": video_id,
            "url": f"https://www.twitch.tv/videos/{video_id}",
            "text": node.get("title") or "",
            # A still-processing VOD's thumbnail is a placeholder image, not
            # a broken link — not worth filtering out, it'll be a real
            # thumbnail again by the time anyone clicks through.
            "image": thumb or None,
            "posted_at": _parse_iso8601(node.get("publishedAt") or ""),
        })
    return posts


_CHECKERS = {
    "youtube": check_youtube, "instagram": check_instagram,
    "twitter": check_twitter, "twitch": check_twitch,
}


# ---- driving loop -----------------------------------------------------------


def _new_posts(items: list[dict], last_id: Optional[str]) -> tuple[list[dict], Optional[str]]:
    """`items` newest-first. Returns (new posts oldest-first, updated cursor).
    A never-checked handle (`last_id is None`) only seeds the cursor — it
    never replays a backlog into the channel."""
    if not items:
        return [], last_id
    newest_id = items[0]["id"]
    if last_id is None or last_id == newest_id:
        return [], newest_id
    new = []
    for item in items:
        if item["id"] == last_id:
            break
        new.append(item)
    new.reverse()
    return new, newest_id


def _announce(user_id: str, display_name: str, platform: str, handle: str, post: dict, token: str) -> Optional[str]:
    """Returns the posted message id (so a caller like social_backup's
    `!post` can later delete a test post), or None on failure."""
    label = PLATFORM_LABEL.get(platform, platform.capitalize())
    embed = {
        "title": _fit(f"{display_name} posted on {label}", 256),
        "description": _fit(post.get("text") or "", 500),
        "color": PLATFORM_COLOR.get(platform, 0x2C3B66),
        "footer": {"text": f"@{handle} · {label}"},
    }
    if post.get("url"):
        embed["url"] = post["url"]
    if post.get("image"):
        embed["image"] = {"url": post["image"]}
    if post.get("posted_at"):
        embed["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(post["posted_at"]))

    content = f"<@{user_id}>" if user_id.isdigit() else None
    message_id = discord_roles.post_message(POST_CHANNEL_ID, token, embed, content=content)
    if message_id:
        activity_log.log(f"\U0001f4e3 New {label} post from {display_name}: {post.get('url')}")
        return message_id
    logger.warning(f"Failed to announce {platform} post for {display_name}")
    activity_log.log(f"❌ Failed to post {label} announcement for {display_name} (Discord API error)")
    return None


def due() -> bool:
    return time.time() - _last_scan >= POLL_SECONDS


# ---- reused by social_backup.py's `!post` command --------------------------


def fetch_latest(platform: str, handle: str) -> Optional[dict]:
    """The single newest post for one handle, or None on any failure/empty
    result. Doesn't touch the cursor — the caller decides what counts as
    "new" (an auto-scan cursor and a manual `!post` have different needs)."""
    checker = _CHECKERS.get(platform)
    if not checker:
        return None
    try:
        items = checker(handle)
    except Exception as e:
        key = f"{platform}:{handle.lower()}"
        _warn_once(f"fetch:{key}", f"{PLATFORM_LABEL.get(platform, platform)} check raised for @{handle}: {e}")
        return None
    return items[0] if items else None


def get_cursor(platform: str, handle: str) -> Optional[str]:
    state = _load_state()
    return state.get(f"{platform}:{handle.lower()}")


def record_seen(platform: str, handle: str, item_id: str) -> None:
    """Advance a handle's cursor to `item_id` so the next auto-scan doesn't
    re-announce something `!post` already posted manually."""
    state = _load_state()
    key = f"{platform}:{handle.lower()}"
    if state.get(key) != item_id:
        state[key] = item_id
        _save_state(state)


def announce(user_id: str, display_name: str, platform: str, handle: str, post: dict, token: str) -> Optional[str]:
    return _announce(user_id, display_name, platform, handle, post, token)


def scan_and_process() -> bool:
    """Returns True if social_watch_state.json changed (new posts announced
    or a handle's cursor was seeded for the first time)."""
    global _last_scan
    if not is_configured() or not due():
        return False
    _last_scan = time.time()

    token = os.environ["DISCORD_BOT_TOKEN"]
    state = _load_state()
    changed = False

    for user_id, profile in social_profiles.load_profiles().items():
        display_name = profile.get("name", user_id)
        for platform, handles in (profile.get("platforms") or {}).items():
            checker = _CHECKERS.get(platform)
            if not checker:
                continue
            for handle in handles:
                key = f"{platform}:{handle.lower()}"
                last_id = state.get(key)
                try:
                    items = checker(handle)
                except Exception as e:
                    _warn_once(f"scan:{key}", f"{PLATFORM_LABEL.get(platform, platform)} check raised for @{handle}: {e}")
                    continue
                new_posts, newest_id = _new_posts(items, last_id)
                if newest_id != last_id:
                    state[key] = newest_id
                    changed = True
                for post in new_posts:
                    _announce(user_id, display_name, platform, handle, post, token)

    if changed:
        _save_state(state)
    return changed
