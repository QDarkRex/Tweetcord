
# Pinned to -bookworm (Debian 12) rather than the untagged `python:3.11.13`.
# The untagged tag has floated onto Debian trixie (13); Playwright's
# `--with-deps` doesn't recognize trixie yet and falls back to a stale
# Ubuntu-20.04 package list (ttf-ubuntu-font-family / ttf-unifont, both
# renamed/removed on trixie) which fails to install. bookworm is a release
# Playwright's dependency mapping does know.
FROM python:3.11.13-bookworm
LABEL org.opencontainers.image.source="https://github.com/Yuuzi261/Tweetcord"
LABEL org.opencontainers.image.description="A Discord bot for Twitter notifications, using tweety-ns module."
LABEL org.opencontainers.image.licenses="MIT"
WORKDIR /bot
COPY requirements.txt /bot/
RUN pip install -r requirements.txt
# chromium + its OS deps, needed by reply_transaction.py to source a real
# x-client-transaction-id for reply notifications (headless, launched briefly
# every ~2h or on failure — not kept running).
RUN playwright install --with-deps chromium
COPY . /bot
CMD ["python", "bot.py"]