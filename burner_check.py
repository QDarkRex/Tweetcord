"""
Burner health report — is each X account healthy, flagged, and reply-capable?

Checks every burner in TWITTER_TOKEN and reports:
  - auth        : does the auth_token still log in?
  - identity    : the account it logs in as (a suspended/locked account fails here)
  - feed        : notifications feed item count — this is what drives tweet/RT/quote
                  notifications, so a burner with a broken feed silently stops
                  delivering for EVERY account it tracks
  - replies     : can it reach X's per-account replies endpoint? Some accounts are
                  read-limited: auth + feed work but UserTweetsAndReplies 404s
  - tracking    : how many accounts this burner is responsible for (from the DB)

Run it inside the running container (reads the same .env and database):

    docker compose exec Tweetcord python burner_check.py

Notes:
  - Read-only. It never follows, unfollows, posts, or writes to the database.
  - The replies endpoint is flaky; a single failure isn't proof of a bad burner,
    so each burner is retried before being reported as reply-incapable.
"""

import asyncio
import os
import sqlite3

from dotenv import load_dotenv

load_dotenv()

from tweety import Twitter  # noqa: E402  (after load_dotenv)
from tweety.types.twDataTypes import SelfThread, Tweet  # noqa: E402

PROBE_TARGET = "elonmusk"  # public, always-active account to test the replies endpoint
REPLY_ATTEMPTS = 3


def tracked_counts() -> dict[str, int]:
    db_path = os.path.join(os.getenv("DATA_PATH", "./data"), "tracked_accounts.db")
    counts = {}
    try:
        con = sqlite3.connect(db_path)
        for client_used, n in con.execute(
            "SELECT client_used, COUNT(*) FROM user WHERE enabled = 1 GROUP BY client_used"
        ):
            counts[client_used] = n
        con.close()
    except Exception as e:
        print(f"(could not read tracked counts: {e})")
    return counts


def _flatten(items) -> list:
    out = []
    for it in items:
        if isinstance(it, SelfThread):
            out.extend(it.tweets)
        elif isinstance(it, Tweet):
            out.append(it)
    return out


async def check(name: str, token: str, tracking: int) -> dict:
    row = {"burner": name, "tracking": tracking, "auth": "?", "identity": "-",
           "feed": "-", "replies": "-", "notes": ""}

    app = Twitter(name)
    try:
        await app.load_auth_token(token)
        row["auth"] = "OK"
    except Exception as e:
        row["auth"] = "FAIL"
        row["notes"] = f"auth: {type(e).__name__}: {str(e)[:60]}"
        return row

    try:
        me = app.me
        row["identity"] = f"@{getattr(me, 'username', '?')}"
    except Exception as e:
        row["identity"] = "ERR"
        row["notes"] = f"identity: {str(e)[:50]}"

    try:
        feed = await asyncio.wait_for(asyncio.to_thread(app.get_tweet_notifications), timeout=90)
        n = len(list(feed))
        row["feed"] = str(n)
        if n == 0:
            row["notes"] = (row["notes"] + " | feed EMPTY (follows nobody, or flagged)").strip(" |")
    except asyncio.TimeoutError:
        row["feed"] = "TIMEOUT"
    except Exception as e:
        row["feed"] = "FAIL"
        row["notes"] = (row["notes"] + f" | feed: {str(e)[:50]}").strip(" |")

    last_err = None
    for _ in range(REPLY_ATTEMPTS):
        try:
            items = await asyncio.wait_for(app.get_tweets(PROBE_TARGET, replies=True, pages=1), timeout=45)
            row["replies"] = f"OK ({len(_flatten(items))})"
            last_err = None
            break
        except Exception as e:
            last_err = e
            await asyncio.sleep(3)
    if last_err is not None:
        row["replies"] = "FAIL"
        row["notes"] = (row["notes"] + f" | replies: {type(last_err).__name__}: {str(last_err)[:40]}").strip(" |")

    return row


async def main():
    raw = os.getenv("TWITTER_TOKEN") or ""
    entries = [e for e in raw.split(",") if e.strip()]
    if not entries:
        print("TWITTER_TOKEN is empty")
        return

    counts = tracked_counts()
    print(f"checking {len(entries)} burner(s) — the replies probe is retried, so this takes a minute\n")

    rows = []
    for entry in entries:
        name, token = entry.split(":", 1)
        name = name.strip()
        rows.append(await check(name, token.strip(), counts.get(name, 0)))
        await asyncio.sleep(2)  # gentle pacing between burners

    header = f"{'burner':10} {'auth':5} {'identity':20} {'feed':8} {'replies':10} {'tracks':7} notes"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(f"{r['burner']:10} {r['auth']:5} {r['identity']:20} {r['feed']:8} "
              f"{r['replies']:10} {r['tracking']:<7} {r['notes']}")

    healthy_feed = [r["burner"] for r in rows if r["feed"].isdigit() and int(r["feed"]) > 0]
    reply_ok = [r["burner"] for r in rows if r["replies"].startswith("OK")]
    print(f"\nfeed-healthy burners  : {healthy_feed or 'NONE'}")
    print(f"reply-capable burners : {reply_ok or 'NONE'}")
    total = sum(counts.values())
    if healthy_feed:
        print(f"tracked accounts: {total} across {len(counts)} burner(s); "
              f"even split over feed-healthy burners would be ~{total // len(healthy_feed)} each")


if __name__ == "__main__":
    asyncio.run(main())
