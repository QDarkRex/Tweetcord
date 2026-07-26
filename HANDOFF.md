# Handoff — state of this fork (branch `reply-notifications`)

Context for anyone (human or a fresh Claude session) picking this up.

## What this fork adds over upstream Tweetcord

1. **Reply notifications** — forwarding tweets that are *replies*, which upstream
   never supported.
2. **Per-burner proxy support** (`proxy_patch.py`) — inert unless `TWITTER_PROXY` is set.
3. **Reliability fixes** to feed polling (timeouts + real liveness detection).
4. **Operational tools** — `burner_check.py`, `rebalance_burners.py`.

## Deployment (important: NOT git-based)

Production is a Docker container on server `x99` (`ssh telecomadmin@100.101.33.93`,
over Tailscale), in `~/tweetcord`. Deploys are done by shipping a tarball, not by
pulling git on the server:

```bash
# on the dev machine
git archive --format=tar.gz -o /path/tweetcord-deploy.tar.gz HEAD
scp /path/tweetcord-deploy.tar.gz telecomadmin@100.101.33.93:~/

# on the server
cd ~/tweetcord
tar -xzf ~/tweetcord-deploy.tar.gz -C ~/tweetcord
docker compose up -d --build
```

`.env`, `configs.yml` and `data/` live only on the server and are **not** in the
tarball (they're gitignored), so they are never overwritten.

⚠️ `configs.yml` on the server may lack a trailing newline — never append with
`echo x >> configs.yml`, it concatenates onto the last line and breaks the YAML
(the bot then won't boot). Use an editor or `printf '\nkey: value\n' >>`.

## How replies work

X's notifications feed (what drives normal tweet/RT/quote detection, one cheap
request per burner) **never contains replies**. Replies must be fetched from X's
per-account "posts & replies" timeline, which means:

- **one request per tracked account** (no shared feed) — see `reply_check_period`
  in `configs.yml`; `0` disables replies entirely (tweets keep working).
- X groups a reply with the tweet it answers into one timeline entry, which
  tweety returns as a `SelfThread` wrapper rather than a flat `Tweet` — so
  `src/notification/reply_fetcher.py` flattens those before filtering.
- `enable_type` in the DB grew from 2 chars (retweet, quote) to 3 (+ reply).
  `migrate_db()` upgrades old rows on boot with reply defaulted **on**, so
  existing notifiers gain replies without being re-added.

### The tweety fork matters
`requirements.txt` pins **Yuuzi261's tweety fork**, which fixes upstream issue
[#295](https://github.com/mahrtayyab/tweety/issues/295) (X's web-client rewrite
broke `x-client-transaction-id` computation). This is what makes the replies
endpoint work: with a *dummy* transaction id it returns 404 ("elevated
authorization"), with a properly computed one it returns 200.

History worth not repeating: an earlier attempt sourced a real transaction id by
driving headless Chromium (Playwright) from inside the bot. It worked, but it
**froze the asyncio event loop** in production — container "Up", 0% CPU, no logs,
zombie processes, and *no notifications of any kind* delivered. That whole
approach was deleted. Do not reintroduce an in-process browser.

### The replies endpoint is quota-limited per burner
Measured on the live server with `burner_check.py`: the set of burners the
replies endpoint accepts **changes between runs** (once 1/3/6 worked and 2/4/5
404'd; a later run was the reverse) while all six had healthy auth and feeds.
The failures track *recent use*, so this is a per-burner quota, not permanently
read-limited accounts.

Consequence: never classify a burner as reply-incapable permanently. An earlier
version probed once at startup and then routed every account's reply polling
through the few that passed — which exhausted exactly those. Now reply fetches
round-robin across **all** burners, and a burner that fails is rested for
`REPLY_COOLDOWN_SECONDS` while others cover its accounts. Reply data is public,
so any burner can fetch any account's replies.

If replies fail broadly, raise `reply_check_period` (fewer requests/minute)
rather than reducing the burner pool.

## Reliability fixes (feed polling)

A notifications-feed fetch had **no timeout**. One hung request froze that
burner's feed permanently: the task still existed so the monitor reported it
`alive`, while every account tracked by that burner silently stopped delivering
(observed: an account stuck for 11h while a fresh manual fetch showed new tweets).

- `FEED_FETCH_TIMEOUT` caps each fetch.
- `feed_last_ok` records the last *successful* fetch per burner — task existence
  is not liveness.
- `tasksMonitor` now **restarts** updaters that are dead *or* stalled
  (`FEED_STALE_SECONDS`), instead of only logging `tweets updater X : dead`.

## Tools

```bash
# health of every burner: auth, identity, feed size, reply capability, load
docker compose exec Tweetcord python burner_check.py

# even out accounts across burners (dry run by default)
docker compose exec Tweetcord python rebalance_burners.py
docker compose exec Tweetcord python rebalance_burners.py --apply --max-moves 5
```

⚠️ Rebalancing makes the receiving burner **follow + bell** each moved account.
Fast bulk-following is what gets burners flagged — move a handful per day
(historical guidance from setup: ~10–15/day max). The bot reassigns tasks from
the DB on its own within ~2 minutes; no restart needed.

## Known open items

- **Reply delivery not yet confirmed end-to-end in Discord.** Fetching replies
  works (verified against real accounts), and the pipeline forwards them, but a
  reply had not yet been observed landing in a Discord channel at handoff time.
- **Load is uneven** (one burner had 0 accounts, another 13) — `rebalance_burners.py`
  exists to fix this but had not been run yet. Verified plan: 54 accounts → 9 per
  burner in 9 moves.
- `burner_check.py` reports reply capability as a snapshot; because of the quota
  behaviour above, a `FAIL` there means "rate-limited right now", not "broken".
  All six burners were auth-healthy with 48–60 feed items at handoff — none flagged.
- `configs.yml` has no `reply_check_period` by default, so the checker logs a
  warning and falls back to the value in `configs.example.yml`.
