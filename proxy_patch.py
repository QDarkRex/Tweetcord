"""
Optional per-burner proxy support for Tweetcord.

Routes each X/Twitter burner account through its own proxy, so that multiple
burners running from a single server IP aren't correlated / co-flagged by X.

INERT BY DEFAULT: if the `TWITTER_PROXY` env var is unset or empty, this module
changes nothing — every `Twitter()` client is built exactly as before, with no
proxy. So it is safe to ship even when no proxies are configured.

Configure in .env, mirroring the TWITTER_TOKEN format (name:value, comma-separated):

    TWITTER_PROXY=burner1:socks5://user:pass@host1:1080,burner2:http://user:pass@host2:3128

- The name before the FIRST ':' must match the account name used in TWITTER_TOKEN.
- The value is a full proxy URL; schemes are whatever httpx supports
  (http://, https://, socks5://). httpx[socks] must be installed for socks5 —
  tweety already pulls it in.
- A burner with no entry here simply runs with no proxy.

How it works: monkeypatches `tweety.Twitter.__init__` to inject the matching
proxy when the caller didn't pass one. tweety's constructor already accepts a
`proxy=` argument (verified: `Twitter(session_name, proxy=..., ...)`), and
Tweetcord constructs clients as `Twitter(account_name)` everywhere, so this one
patch covers every call site without editing them.
"""

import os
import logging

from tweety import Twitter as _Twitter

log = logging.getLogger(__name__)

_orig_twitter_init = _Twitter.__init__
_proxy_map = None  # parsed lazily, AFTER dotenv has loaded the environment


def _parse_proxy_map():
    mapping = {}
    raw = (os.getenv("TWITTER_PROXY") or "").strip()
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        # Split on the FIRST ':' only, so the proxy URL's own colons survive.
        name, _sep, proxy = entry.partition(":")
        name, proxy = name.strip(), proxy.strip()
        if name and proxy:
            mapping[name] = proxy
    if mapping:
        # Log only the burner names that got a proxy, never the proxy URLs
        # (they may contain credentials).
        log.warning("per-burner proxy patch active for: %s", ", ".join(sorted(mapping)))
    return mapping


def _get_proxy_map():
    global _proxy_map
    if _proxy_map is None:
        _proxy_map = _parse_proxy_map()
    return _proxy_map


def _patched_twitter_init(self, session_name, proxy=None, *args, **kwargs):
    # Only inject when the caller didn't already specify a proxy explicitly.
    if proxy is None and isinstance(session_name, str):
        proxy = _get_proxy_map().get(session_name)
    return _orig_twitter_init(self, session_name, proxy, *args, **kwargs)


_Twitter.__init__ = _patched_twitter_init
