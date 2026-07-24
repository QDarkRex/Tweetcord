"""
Fetches a tracked account's REPLIES.

Why this exists: tweety's `get_tweet_notifications()` (the shared, cheap,
per-burner feed the rest of this bot's tweet detection is built on) never
contains replies — X simply doesn't put them there. The only way to get a
user's replies is X's per-account "posts & replies" timeline
(`UserTweetsAndReplies`), which needs one extra bit of handling: X groups a
reply together with the tweet it's replying to into a single "conversation
module" timeline entry, which tweety parses into a `SelfThread` wrapper object
(NOT a flat `Tweet`). So the real reply is one level deeper than tweety's
other timeline calls — we flatten those wrappers back out here.

That's the ONLY special handling needed: with the Yuuzi261 tweety fork (see
requirements.txt) the endpoint's `x-client-transaction-id` is computed for
real, so the request just succeeds — no dummy-id patch, no headless browser.
The rest of the bot (is_match_type, gen_embed, get_action, dedup/timestamp
filtering) treats a reply as just another `Tweet` with `is_reply == True`.
"""

from tweety import Twitter
from tweety.types.twDataTypes import Tweet, SelfThread


def _flatten(items) -> list[Tweet]:
    out = []
    for it in items:
        if isinstance(it, SelfThread):
            out.extend(it.tweets)
        elif isinstance(it, Tweet):
            out.append(it)
    return out


def _is_real_reply(tweet: Tweet) -> bool:
    # tweety's own `is_reply` flag is unreliable on some timeline shapes; the raw
    # in_reply_to_status_id_str being set is the definitive signal.
    return bool(getattr(tweet, "_original_tweet", {}).get("in_reply_to_status_id_str"))


async def get_user_replies(app: Twitter, username: str, pages: int = 1) -> list[Tweet]:
    """Returns the tracked user's recent REAL replies as normal Tweet objects
    (is_reply == True)."""
    items = await app.get_tweets(username, replies=True, pages=pages)
    return [t for t in _flatten(items) if _is_real_reply(t)]
