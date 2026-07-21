"""
STOPGAP PATCH — tweety vs X's June 2026 web-client rewrite.

Problem
-------
Around 2026-06-23 X migrated its web client from the old webpack build
(`abs.twimg.com/responsive-web/client-web/...`) to a new Vite build
(`abs.twimg.com/x-web/x-web/assets/...`). In doing so it removed the
`ondemand.s` chunk and the inline webpack chunk-mapping that tweety parses to
compute the `x-client-transaction-id` request header. As a result tweety's
`TransactionGenerator` raises `Exception: Couldn't get animation key indices`
and authentication / tweet fetching fails for every tweety user.
Upstream tracking issue: https://github.com/mahrtayyab/tweety/issues/295

Why this works
--------------
Testing shows X's servers currently still ACCEPT requests that carry a
syntactically-valid but otherwise dummy `x-client-transaction-id`. So instead
of computing the (now-unavailable) real value, we bypass the broken parsing and
send a random dummy id. Verified working for login and timeline reads.

This is a temporary measure
---------------------------
Remove this module (and its import in bot.py) once tweety upstream fixes #295,
which will restore the real, more robust transaction-id computation.
"""

import base64
import random
import logging

import tweety.transaction as _transaction

log = logging.getLogger(__name__)


def _patched_init(self, home_page_html):
    # Skip the home-page parsing that now fails; the real key/animation values
    # are not needed because we emit a dummy transaction id below.
    self.home_page_html = None
    self.key = None
    self.animation_key = None


def _patched_generate_transaction_id(self, method, path, response=None,
                                     key=None, animation_key=None, time_now=None):
    # 64 random bytes, base64 without padding — same shape X expects.
    return base64.b64encode(bytes(random.randint(0, 255) for _ in range(64))).decode().rstrip("=")


_transaction.TransactionGenerator.__init__ = _patched_init
_transaction.TransactionGenerator.generate_transaction_id = _patched_generate_transaction_id

log.warning("Applied tweety x-client-transaction-id bypass patch "
            "(stopgap for upstream issue #295).")
