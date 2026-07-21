"""
Fetches a tracked account's REPLIES.

Why this exists: tweety's `get_tweet_notifications()` (the shared, cheap,
per-burner feed the rest of this bot's tweet detection is built on) never
contains replies — X simply doesn't put them there. The only way to get a
user's replies is X's per-account "posts & replies" timeline
(`UserTweetsAndReplies` GraphQL query), which requires:
  1. a REAL `x-client-transaction-id` (see reply_transaction.py — the dummy
     one from transaction_patch.py is rejected for this specific endpoint), and
  2. flattening tweety's response shape: X groups a reply together with the
     tweet it's replying to into one "conversation module" timeline entry,
     which tweety parses into a `SelfThread` wrapper object (NOT a flat
     `Tweet`) — so the real reply is one level deeper than tweety's other
     timeline calls. Verified 2026-07-19 against live data.

Both quirks are handled here so the rest of the bot (is_match_type, gen_embed,
get_action, the dedup/timestamp filtering in get_tweets.py) can keep treating
a reply as just another `Tweet` object — no changes needed there beyond
checking `tweet.is_reply`.

NOTE — the GraphQL query id below (`_REPLIES_QUERY_ID`) is X's CURRENT id for
UserTweetsAndReplies as of 2026-07-19. X periodically rotates these (this is
the same class of breakage as transaction_patch.py's issue, just a different
symptom: a stale id 404s with an EMPTY response body, vs. a stale/dummy
transaction-id 404s with the "elevated authorization" message). If replies
start failing again, check both independently:
  - stale query id -> capture a fresh one the same way reply_transaction.py
    captures the transaction id (sniff a real browser's request to
    x.com/<user>/with_replies and read the id out of the request URL), or
    read it out of X's main JS bundle (search for `operationName:"UserTweetsAndReplies"`).
  - stale/rejected transaction id -> reply_transaction.force_refresh().
"""

import logging

from tweety import Twitter
from tweety.builder import UrlBuilder
from tweety.types.twDataTypes import Tweet, SelfThread

import reply_transaction

log = logging.getLogger(__name__)

_REPLIES_QUERY_ID = "klja8a2iJX_3to5RdfVlgw"
UrlBuilder.URL_USER_TWEETS_WITH_REPLIES = f"https://x.com/i/api/graphql/{_REPLIES_QUERY_ID}/UserTweetsAndReplies"


def _flatten(items) -> list[Tweet]:
    out = []
    for it in items:
        if isinstance(it, SelfThread):
            out.extend(it.tweets)
        elif isinstance(it, Tweet):
            out.append(it)
    return out


def _is_real_reply(tweet: Tweet) -> bool:
    # tweety's own `is_reply` flag is unreliable on this endpoint's SelfThread-
    # wrapped entries (see module docstring); check the raw field directly.
    return bool(getattr(tweet, "_original_tweet", {}).get("in_reply_to_status_id_str"))


async def get_user_replies(app: Twitter, username: str, pages: int = 1) -> list[Tweet]:
    """Returns the tracked user's recent REAL replies as normal Tweet objects
    (is_reply == True), newest included. Retries once with a freshly-sourced
    transaction id if the request is rejected."""
    await reply_transaction.ensure_fresh()

    for attempt in range(2):
        try:
            items = await app.get_tweets(username, replies=True, pages=pages)
            return [t for t in _flatten(items) if _is_real_reply(t)]
        except Exception as e:
            if attempt == 0:
                log.warning(f"replies fetch for {username} failed ({e}); refreshing transaction id and retrying once")
                refreshed = await reply_transaction.force_refresh()
                if not refreshed:
                    raise
                continue
            raise
