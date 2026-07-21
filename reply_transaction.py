"""
STOPGAP PATCH #2 — real x-client-transaction-id for the UserTweetsAndReplies
endpoint (needed for the "reply" notification type).

Problem
-------
`transaction_patch.py` already works around tweety's broken transaction-id
computation (upstream issue #295) by sending a syntactically-valid DUMMY id.
X accepts that dummy for most endpoints (login, home timeline, notifications
feed) — but NOT for `UserTweetsAndReplies` (the "posts & replies" tab, which
is the only way to fetch a user's REPLIES; X's notifications feed never
contains real replies). A dummy id there gets rejected with an HTTP 404
("Page not Found. Most likely you need elevated authorization").

Verified 2026-07-19: swapping in a REAL transaction id (captured once from an
authenticated browser session) makes the exact same request succeed (HTTP
200), and it stayed valid across repeated reuse minutes apart — so it is not
single-use/per-request, just needs to be a REAL computed value, not a random
one.

Fix
---
Source ONE real transaction id by driving a headless, logged-in browser
(Playwright, using a burner's `auth_token` cookie — no interactive login
needed) to a public profile's "with_replies" page once, and sniffing the
`x-client-transaction-id` header off the real request the page itself makes.
Cache it in memory and reuse it for every UserTweetsAndReplies call made via
tweety/httpx (cheap — no browser per request). Refresh it periodically and
immediately on a 404/403 from that endpoint.

This is a temporary measure, same spirit as transaction_patch.py
------------------------------------------------------------------
If tweety upstream ever restores real transaction-id computation (#295), or
ships proper support for the replies endpoint, this whole module (and the
Playwright dependency it requires) can be deleted.
"""

import asyncio
import time

import tweety.transaction as _transaction

from src.log import setup_logger

log = setup_logger(__name__)

# How long a sourced transaction id is trusted before a proactive refresh.
_TTL_SECONDS = 2 * 60 * 60  # 2 hours — conservative; refreshed sooner on failure anyway.
_TARGET_USERNAME = "elonmusk"  # any public account works; this one always exists.

_state = {"id": None, "fetched_at": 0.0}
_refresh_lock = asyncio.Lock()
_warm_token: str | None = None  # the burner auth_token used to source the id


def set_source_token(auth_token: str) -> None:
    """Call once at startup with any ONE burner's auth_token (any authenticated
    burner works — the sourced id is not observed to be burner-specific)."""
    global _warm_token
    _warm_token = auth_token


async def _fetch_real_transaction_id(auth_token: str) -> str | None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.error("playwright is not installed — cannot source a real x-client-transaction-id "
                   "for the replies endpoint. Reply notifications will keep failing. "
                   "Add 'playwright' to requirements.txt and run 'playwright install --with-deps chromium'.")
        return None

    found = {}
    try:
        async with async_playwright() as p:
            # --no-sandbox: Chromium's sandbox needs kernel privileges a Docker
            # container running as root usually doesn't have; without this flag
            # the launch can fail/hang silently in exactly that environment.
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
            try:
                ctx = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
                await ctx.add_cookies([{
                    "name": "auth_token", "value": auth_token,
                    "domain": ".x.com", "path": "/", "httpOnly": True, "secure": True,
                }])
                page = await ctx.new_page()

                def on_request(req):
                    if "UserTweetsAndReplies" in req.url and "x-client-transaction-id" in req.headers:
                        found["id"] = req.headers["x-client-transaction-id"]

                page.on("request", on_request)
                try:
                    await page.goto(f"https://x.com/{_TARGET_USERNAME}/with_replies",
                                     wait_until="networkidle", timeout=25000)
                except Exception:
                    pass  # we only need the request to have FIRED, not the page to fully settle
                await page.wait_for_timeout(1500)
            finally:
                await browser.close()
    except Exception as e:
        log.error(f"failed to source a real x-client-transaction-id via headless browser: {e}")
        return None

    return found.get("id")


async def force_refresh() -> bool:
    """Force a re-source of the transaction id. Returns True on success."""
    if not _warm_token:
        log.error("reply_transaction.force_refresh called before set_source_token — no burner token available")
        return False
    async with _refresh_lock:
        # Hard timeout: everything inside _fetch_real_transaction_id has its own
        # timeouts too (page.goto=25s), but Chromium in a resource-starved
        # container (e.g. too-small /dev/shm) can hang somewhere those don't
        # cover (launch, close). Without this, one stuck launch wedges the
        # shared lock and every other tracked account's replies queue forever
        # with NO log output at all — exactly what was observed 2026-07-21.
        try:
            new_id = await asyncio.wait_for(_fetch_real_transaction_id(_warm_token), timeout=60)
        except asyncio.TimeoutError:
            log.error("timed out (60s) sourcing a real x-client-transaction-id — Chromium likely hung "
                      "(check compose.yml shm_size; Docker's 64MB default is too small for Chromium)")
            new_id = None
        if new_id:
            _state["id"] = new_id
            _state["fetched_at"] = time.monotonic()
            log.info("sourced a fresh x-client-transaction-id for the replies endpoint")
            return True
        log.warning("could not source a real x-client-transaction-id (replies endpoint will keep failing)")
        return False


async def ensure_fresh() -> None:
    """Source an id if we don't have one yet, or it's past its TTL. Safe to call often (no-op when fresh)."""
    if _state["id"] is None or (time.monotonic() - _state["fetched_at"]) > _TTL_SECONDS:
        await force_refresh()


def get_cached_id() -> str | None:
    return _state["id"]


# --- apply the patch: use the real cached id ONLY for UserTweetsAndReplies,
# fall through to whatever generator is already installed (the transaction_patch.py
# dummy generator) for every other endpoint. Import this AFTER transaction_patch. ---
_fallback_generate = _transaction.TransactionGenerator.generate_transaction_id


def _patched_generate_transaction_id(self, method, path, response=None,
                                      key=None, animation_key=None, time_now=None):
    if "UserTweetsAndReplies" in path and _state["id"]:
        return _state["id"]
    return _fallback_generate(self, method, path, response=response,
                               key=key, animation_key=animation_key, time_now=time_now)


_transaction.TransactionGenerator.generate_transaction_id = _patched_generate_transaction_id

log.warning("Applied reply-transaction-id patch (stopgap #2, requires playwright at runtime).")
