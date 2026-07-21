FROM python:3.11.13
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