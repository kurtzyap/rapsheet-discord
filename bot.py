#!/usr/bin/env python3
"""
RapSheet -> Discord

Polls Ian Rapoport's Bluesky feed, keeps the posts that look like real NFL news
(injuries, trades/signings, coaching moves, suspensions), and pushes them to a
Discord channel via webhook.

No API keys. No Twitter bill. Bluesky's public read API needs no auth.

Usage:
    python bot.py                 # normal run
    python bot.py --dry-run       # classify and print, post nothing
    python bot.py --selftest      # run the classifier fixtures, exit non-zero on failure
    python bot.py --backfill 10   # post the 10 most recent matching posts (ignores state)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
RULES_PATH = os.path.join(HERE, "rules.json")
STATE_PATH = os.path.join(HERE, "state.json")

BSKY_API = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
DEFAULT_HANDLE = "rapsheet.bsky.social"

# Don't post anything older than this on a normal run. Stops a stalled workflow
# from dumping a day of news into the channel when it comes back.
MAX_AGE_HOURS = 6

# How many post IDs to remember. ~2 days of his volume.
STATE_KEEP = 400

FALLBACK_COLOR = 0x5865F2
USER_AGENT = "rapsheet-discord-bot/1.0 (+https://github.com)"


# ----------------------------------------------------------------------------
# rules / classification
# ----------------------------------------------------------------------------

class Classifier:
    def __init__(self, rules: dict[str, Any]):
        self.excludes = [re.compile(p, re.I) for p in rules.get("exclude", [])]
        self.categories = []
        for cat in rules.get("categories", []):
            if not cat.get("enabled", True):
                continue
            self.categories.append({
                "key": cat["key"],
                "label": cat.get("label", cat["key"].title()),
                "emoji": cat.get("emoji", ""),
                "color": cat.get("color", FALLBACK_COLOR),
                "priority": cat.get("priority", False),
                "patterns": [re.compile(p, re.I) for p in cat.get("patterns", [])],
            })

    def classify(self, text: str) -> dict[str, Any] | None:
        """Return the first matching category, or None if the post is noise."""
        if not text or not text.strip():
            return None
        for ex in self.excludes:
            if ex.search(text):
                return None
        for cat in self.categories:
            for pat in cat["patterns"]:
                if pat.search(text):
                    return cat
        return None


def load_rules() -> Classifier:
    with open(RULES_PATH, encoding="utf-8") as fh:
        return Classifier(json.load(fh))


# ----------------------------------------------------------------------------
# state
# ----------------------------------------------------------------------------

def load_state() -> dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {"seen": [], "bootstrapped": False, "last_run": None}
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            state = json.load(fh)
    except (json.JSONDecodeError, OSError):
        # Corrupt state is recoverable: treat it as a fresh bootstrap rather
        # than crashing the workflow forever.
        return {"seen": [], "bootstrapped": False, "last_run": None}
    state.setdefault("seen", [])
    state.setdefault("bootstrapped", False)
    state.setdefault("last_run", None)
    return state


def save_state(state: dict[str, Any]) -> None:
    state["seen"] = state["seen"][-STATE_KEEP:]
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
        fh.write("\n")
    os.replace(tmp, STATE_PATH)


# ----------------------------------------------------------------------------
# bluesky
# ----------------------------------------------------------------------------

def http_get_json(url: str, tries: int = 3) -> dict[str, Any]:
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last = exc
            if attempt < tries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Bluesky request failed after {tries} tries: {last}")


def fetch_feed(handle: str, limit: int = 50) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode({
        "actor": handle,
        "limit": str(limit),
        "filter": "posts_no_replies",
    })
    data = http_get_json(f"{BSKY_API}?{params}")
    return data.get("feed", [])


def post_web_url(post: dict[str, Any]) -> str:
    handle = post.get("author", {}).get("handle", DEFAULT_HANDLE)
    rkey = post.get("uri", "").rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


def extract_media(post: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (image_url, link_url) from whatever embed shape the post has."""
    embed = post.get("embed") or {}
    etype = embed.get("$type", "")
    image = None
    link = None

    if "images" in etype:
        imgs = embed.get("images") or []
        if imgs:
            image = imgs[0].get("fullsize") or imgs[0].get("thumb")
    elif "external" in etype:
        ext = embed.get("external") or {}
        link = ext.get("uri")
        image = ext.get("thumb")
    elif "recordWithMedia" in etype:
        media = embed.get("media") or {}
        if "images" in media.get("$type", ""):
            imgs = media.get("images") or []
            if imgs:
                image = imgs[0].get("fullsize") or imgs[0].get("thumb")
        elif "external" in media.get("$type", ""):
            ext = media.get("external") or {}
            link = ext.get("uri")
            image = ext.get("thumb")

    return image, link


# ----------------------------------------------------------------------------
# discord
# ----------------------------------------------------------------------------

def build_embed(post: dict[str, Any], cat: dict[str, Any]) -> dict[str, Any]:
    record = post.get("record", {})
    author = post.get("author", {})
    text = (record.get("text") or "").strip()
    image, link = extract_media(post)

    # Discord embed descriptions cap at 4096; his posts are nowhere near that,
    # but truncate defensively.
    if len(text) > 3900:
        text = text[:3897] + "..."

    title = f"{cat['emoji']} {cat['label']}".strip()

    embed: dict[str, Any] = {
        "title": title,
        "description": text,
        "url": post_web_url(post),
        "color": cat["color"],
        "timestamp": record.get("createdAt") or post.get("indexedAt"),
        "author": {
            "name": author.get("displayName") or "Ian Rapoport",
            "url": f"https://bsky.app/profile/{author.get('handle', DEFAULT_HANDLE)}",
        },
        "footer": {"text": "via Bluesky"},
    }

    avatar = author.get("avatar")
    if avatar:
        embed["author"]["icon_url"] = avatar
    if image:
        embed["image"] = {"url": image}
    if link:
        embed["fields"] = [{"name": "Link", "value": link, "inline": False}]

    return embed


def send_to_discord(webhook: str, embed: dict[str, Any], content: str | None = None) -> None:
    payload: dict[str, Any] = {
        "username": "RapSheet",
        "embeds": [embed],
        "allowed_mentions": {"parse": ["roles"]},
    }
    if content:
        payload["content"] = content

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )

    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status in (200, 204):
                    return
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                # Discord tells us how long to wait.
                try:
                    retry = json.loads(exc.read().decode()).get("retry_after", 2)
                except Exception:
                    retry = 2
                time.sleep(float(retry) + 0.5)
                continue
            if 500 <= exc.code < 600 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Discord rejected the post ({exc.code}): {exc.read()[:300]!r}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Could not reach Discord: {exc}")
    raise RuntimeError("Discord post failed after retries")


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def is_fresh(post: dict[str, Any], max_age_hours: int) -> bool:
    stamp = post.get("record", {}).get("createdAt") or post.get("indexedAt")
    if not stamp:
        return True
    try:
        created = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc) - created < timedelta(hours=max_age_hours)


def run(args: argparse.Namespace) -> int:
    handle = os.environ.get("BSKY_HANDLE") or DEFAULT_HANDLE
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    role_id = os.environ.get("PING_ROLE_ID", "").strip()
    # "all"      -> ping on every post that clears the filter
    # "priority" -> ping only on injuries, trades and coaching moves
    ping_mode = (os.environ.get("PING_MODE") or "all").strip().lower()
    max_age = int(os.environ.get("MAX_AGE_HOURS") or MAX_AGE_HOURS)

    if not webhook and not args.dry_run:
        print("ERROR: DISCORD_WEBHOOK_URL is not set.", file=sys.stderr)
        return 2

    clf = load_rules()
    state = load_state()
    seen = set(state["seen"])

    try:
        feed = fetch_feed(handle, limit=50)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Fetched {len(feed)} items from @{handle}")

    # Bluesky returns newest-first; post oldest-first so the channel reads in order.
    items = list(reversed(feed))

    # First ever run: remember everything currently in the feed, post nothing.
    if not state["bootstrapped"] and not args.backfill:
        for item in items:
            uri = item.get("post", {}).get("uri")
            if uri:
                state["seen"].append(uri)
        state["bootstrapped"] = True
        save_state(state)
        print("Bootstrap run — seeded state with the current feed, posted nothing.")
        print("The next run will post anything new from here on.")
        return 0

    posted = 0
    skipped_seen = 0
    skipped_noise = 0
    skipped_old = 0

    for item in items:
        post = item.get("post") or {}
        uri = post.get("uri")
        if not uri:
            continue

        # item["reason"] present == this is a repost of someone else, not his words.
        if item.get("reason"):
            continue

        if uri in seen and not args.backfill:
            skipped_seen += 1
            continue

        text = (post.get("record", {}).get("text") or "").strip()
        cat = clf.classify(text)
        if not cat:
            skipped_noise += 1
            state["seen"].append(uri)
            seen.add(uri)
            continue

        if not args.backfill and not is_fresh(post, max_age):
            skipped_old += 1
            state["seen"].append(uri)
            seen.add(uri)
            continue

        embed = build_embed(post, cat)
        content = None
        if role_id and (ping_mode == "all" or cat.get("priority")):
            content = f"<@&{role_id}>"

        if args.dry_run:
            print(f"\n[{cat['label']}] {post_web_url(post)}")
            print(f"  {text[:200]}")
        else:
            send_to_discord(webhook, embed, content)
            time.sleep(1.1)  # stay well under Discord's webhook rate limit

        state["seen"].append(uri)
        seen.add(uri)
        posted += 1

        if args.backfill and posted >= args.backfill:
            break

    if not args.dry_run:
        save_state(state)

    print(
        f"Done. posted={posted} already_seen={skipped_seen} "
        f"not_news={skipped_noise} too_old={skipped_old}"
    )
    return 0


# ----------------------------------------------------------------------------
# selftest
# ----------------------------------------------------------------------------

FIXTURES: list[tuple[str, str | None]] = [
    # --- injuries ---
    ("Sources: #Bengals WR Tee Higgins suffered a torn ACL in practice today and "
     "is expected to miss the rest of the season.", "injury"),
    ("#Jets QB Aaron Rodgers is having an MRI on his ankle this morning, source said.", "injury"),
    ("The #Cowboys are placing LB Micah Parsons on injured reserve.", "injury"),
    ("#Ravens RB has a high-ankle sprain and is considered week-to-week, per sources.",
     "injury"),
    ("He was carted off in the third quarter and did not return.", "injury"),
    ("Good news for the #Lions: tests revealed no structural damage and he avoided a "
     "serious knee injury.", "injury"),

    # --- transactions ---
    ("Trade! The #Browns are sending WR Amari Cooper to the #Bills for a third-round pick.",
     "transaction"),
    ("The #Steelers and QB Russell Wilson have agreed to terms on a 1-year deal.",
     "transaction"),
    ("#Chiefs are signing veteran CB to a 2-year, $18 million contract with $12M guaranteed.",
     "transaction"),
    ("The #Giants are releasing veteran S Xavier McKinney.", "transaction"),
    ("#Packers placed the franchise tag on their All-Pro.", "transaction"),

    # --- staff ---
    ("The #Panthers have fired head coach Frank Reich, sources say.", "staff"),
    ("#Raiders GM is being relieved of his duties after four seasons.", "staff"),
    ("The #Commanders have requested permission to interview Lions OC Ben Johnson "
     "for their head coaching job.", "staff"),
    ("Sources: the #Titans are hiring Brian Callahan as their new head coach.", "staff"),
    ("He is stepping down for health reasons, the team announced.", "staff"),

    # --- discipline ---
    ("#Falcons player has been suspended six games for violating the NFL's gambling policy.",
     "discipline"),
    ("He was placed on the commissioner's exempt list this afternoon.", "discipline"),
    ("The appeal was denied and the suspension stands.", "discipline"),
    ("He's been reinstated by the NFL and is eligible to return Week 5.", "discipline"),

    # --- ambiguous wording that used to trip the classifier ---
    ("The #Dolphins are expected to activate their RB off IR on Saturday.", "injury"),
    ("The #Jaguars are expected to part ways with their head coach after the season.",
     "staff"),
    ("Team is promoting its assistant GM to general manager.", "staff"),
    ("Deal: #Vikings and their All-Pro have agreed to a 4-year, $140M extension.",
     "transaction"),
    ("Sources: QB expected to undergo season-ending shoulder surgery this week.", "injury"),

    # --- noise that should NOT post ---
    ("Tune in to NFL GameDay Morning at 9am ET.", None),
    ("Happy birthday to my guy!", None),
    ("What a finish. Unbelievable game.", None),
    ("Good morning from Indianapolis.", None),
    ("This is great.", None),
    ("Congrats to the whole crew on a big night.", None),
    ("What a sign of things to come.", None),
    ("That was a cut above anything we saw last year.", None),
    ("I appeal to everyone to be patient with this rookie.", None),
    ("Big deal for that fanbase to see this kind of energy.", None),
    ("The atmosphere here is unreal. Chills.", None),
    ("On the road again. Headed to Green Bay.", None),
    ("From today's NFL GameDay Morning:", None),
    ("Here's my full conversation with the commissioner.", None),
    ("A story I've wanted to write for a long time.", None),
    ("Week 3 is going to be wild.", None),
]


def selftest() -> int:
    clf = load_rules()
    failures = []
    for text, expected in FIXTURES:
        cat = clf.classify(text)
        got = cat["key"] if cat else None
        if got != expected:
            failures.append((text, expected, got))

    total = len(FIXTURES)
    print(f"Classifier self-test: {total - len(failures)}/{total} passed")
    for text, expected, got in failures:
        print(f"  FAIL  expected={expected!r:14} got={got!r:14} :: {text[:80]}")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Post Ian Rapoport's NFL news to Discord.")
    ap.add_argument("--dry-run", action="store_true",
                    help="classify and print, post nothing, don't touch state")
    ap.add_argument("--selftest", action="store_true",
                    help="run the classifier fixtures and exit")
    ap.add_argument("--backfill", type=int, default=0, metavar="N",
                    help="post the N most recent matching posts, ignoring state")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
