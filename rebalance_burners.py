"""
Even out tracked accounts across burners.

Tweetcord assigns each tracked account to ONE burner (`user.client_used`), whose
notifications feed is polled for that account's tweets. If the distribution is
lopsided (e.g. one burner tracking 13 accounts while another tracks 0), one
burner does all the work and a problem with it takes down a large slice of
notifications at once.

Moving an account is NOT just a database edit: the new burner must FOLLOW the
account and turn on its bell, or that account's tweets never enter the new
burner's feed. That is a bulk-follow, which X flags if done quickly — so moves
are throttled and capped by default.

USAGE (inside the running container):

    # 1. see the plan without touching anything (default)
    docker compose exec Tweetcord python rebalance_burners.py

    # 2. actually move, slowly — start small
    docker compose exec Tweetcord python rebalance_burners.py --apply --max-moves 5

    # spread the rest over following days, e.g. 10-15/day at most
    docker compose exec Tweetcord python rebalance_burners.py --apply --max-moves 10 --delay 120

Options:
    --apply            perform the moves (without it: dry run, prints the plan only)
    --max-moves N      stop after N moves (default 5)
    --delay SECONDS    wait between moves (default 90)
    --only-healthy     skip burners whose feed check fails (default on)

The bot picks up the reassignment on its own within ~2 minutes (it reloads
client_used from the database and restarts that account's task) — no restart
needed. Run burner_check.py first to see which burners are healthy.
"""

import argparse
import asyncio
import os
import sqlite3

from dotenv import load_dotenv

load_dotenv()

from tweety import Twitter  # noqa: E402


def db_path() -> str:
    return os.path.join(os.getenv("DATA_PATH", "./data"), "tracked_accounts.db")


def load_distribution() -> dict[str, list[str]]:
    con = sqlite3.connect(db_path())
    dist: dict[str, list[str]] = {}
    for username, client_used in con.execute(
        "SELECT username, client_used FROM user WHERE enabled = 1 ORDER BY username"
    ):
        dist.setdefault(client_used, []).append(username)
    con.close()
    return dist


def plan_moves(dist: dict[str, list[str]], burners: list[str]) -> list[tuple[str, str, str]]:
    """Greedy: repeatedly move one account from the most-loaded burner to the
    least-loaded one until the spread is at most 1. Returns (username, from, to)."""
    load = {b: list(dist.get(b, [])) for b in burners}
    moves = []
    while True:
        heaviest = max(load, key=lambda b: len(load[b]))
        lightest = min(load, key=lambda b: len(load[b]))
        if len(load[heaviest]) - len(load[lightest]) <= 1:
            break
        username = load[heaviest].pop()
        load[lightest].append(username)
        moves.append((username, heaviest, lightest))
    return moves


async def feed_healthy(name: str, token: str) -> bool:
    try:
        app = Twitter(name)
        await app.load_auth_token(token)
        feed = await asyncio.wait_for(asyncio.to_thread(app.get_tweet_notifications), timeout=90)
        return len(list(feed)) > 0
    except Exception as e:
        print(f"  burner {name}: unhealthy ({type(e).__name__}: {str(e)[:50]})")
        return False


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually perform the moves")
    ap.add_argument("--max-moves", type=int, default=5)
    ap.add_argument("--delay", type=int, default=90, help="seconds between moves")
    ap.add_argument("--include-unhealthy", action="store_true",
                    help="also balance onto burners whose feed check fails")
    args = ap.parse_args()

    tokens = {}
    for entry in (os.getenv("TWITTER_TOKEN") or "").split(","):
        if entry.strip():
            n, t = entry.split(":", 1)
            tokens[n.strip()] = t.strip()
    if not tokens:
        print("TWITTER_TOKEN is empty")
        return

    dist = load_distribution()
    print("current distribution:")
    for b in tokens:
        print(f"  {b:10} {len(dist.get(b, [])):3} accounts")
    orphans = set(dist) - set(tokens)
    if orphans:
        print(f"  (accounts assigned to unknown burners, left alone: {sorted(orphans)})")

    burners = list(tokens)
    if not args.include_unhealthy:
        print("\nchecking burner feeds (only healthy burners receive accounts)...")
        healthy = []
        for b in burners:
            if await feed_healthy(b, tokens[b]):
                healthy.append(b)
            await asyncio.sleep(2)
        print(f"  healthy: {healthy}")
        if not healthy:
            print("no healthy burners — aborting")
            return
        burners = healthy

    moves = plan_moves(dist, burners)
    if not moves:
        print("\nalready balanced — nothing to do")
        return

    moves = moves[: args.max_moves]
    print(f"\nplanned moves (showing {len(moves)}, capped by --max-moves):")
    for username, src, dst in moves:
        print(f"  {username:20} {src} -> {dst}")

    if not args.apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply to perform these moves.")
        print("Tip: move a few per day; each move makes the new burner FOLLOW the account,")
        print("and fast bulk-following is what gets a burner flagged.")
        return

    print(f"\napplying, {args.delay}s between moves...")
    clients: dict[str, Twitter] = {}
    done = 0
    for username, src, dst in moves:
        try:
            if dst not in clients:
                app = Twitter(dst)
                await app.load_auth_token(tokens[dst])
                clients[dst] = app
            app = clients[dst]

            user = await app.get_user_info(username)
            await app.follow_user(user)
            ok = await app.enable_user_notification(user)
            if not ok:
                print(f"  {username}: followed but could NOT enable notifications — skipping DB change")
                continue

            con = sqlite3.connect(db_path())
            con.execute("UPDATE user SET client_used = ? WHERE username = ? COLLATE NOCASE", (dst, username))
            con.commit()
            con.close()
            done += 1
            print(f"  {username}: moved {src} -> {dst} ({done}/{len(moves)})")
        except Exception as e:
            print(f"  {username}: FAILED ({type(e).__name__}: {str(e)[:60]}) — left on {src}")
        await asyncio.sleep(args.delay)

    print(f"\ndone: {done} account(s) moved. The bot reassigns their tasks within ~2 minutes.")
    print("The old burners still follow these accounts; that's harmless (their feed items")
    print("are simply ignored). Unfollow manually later if you want to tidy up.")


if __name__ == "__main__":
    asyncio.run(main())
