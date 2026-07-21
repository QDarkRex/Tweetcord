import os

import aiosqlite

from src.log import setup_logger
from src.utils import get_utcnow

log = setup_logger(__name__)


async def init_db():
    data_path = os.getenv('DATA_PATH')
    if not os.path.exists(data_path):
        os.mkdir(data_path)

    db_path = os.path.join(data_path, 'tracked_accounts.db')
    db_exists = os.path.exists(db_path)
    
    if db_exists: return

    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS user (id TEXT PRIMARY KEY, username TEXT, latest_tweet TEXT, client_used TEXT, enabled INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS channel (id TEXT PRIMARY KEY, server_id TEXT);
            CREATE TABLE IF NOT EXISTS notification (user_id TEXT, channel_id TEXT, role_id TEXT, enabled INTEGER DEFAULT 1, enable_type TEXT DEFAULT 111, enable_media_type TEXT DEFAULT 11, customized_msg TEXT DEFAULT NULL, FOREIGN KEY (user_id) REFERENCES user (id), FOREIGN KEY (channel_id) REFERENCES channel (id), PRIMARY KEY(user_id, channel_id));
            CREATE TABLE IF NOT EXISTS server_user_config (server_id TEXT, user_id TEXT, translate TEXT, PRIMARY KEY(server_id, user_id), FOREIGN KEY (user_id) REFERENCES user (id));
        """)
        await db.commit()

    log.info('database file not found, a blank database file has been created')


async def migrate_db(db_path: str):
    """Extends any pre-'reply-type' `enable_type` values (2 chars: retweet,
    quote) to the current 3-char shape (retweet, quote, reply) by APPENDING
    the reply bit as enabled ('1'). This is additive-only — existing
    retweet/quote settings and every other notifier setting are left exactly
    as they were, so nobody has to re-run `/add notifier`."""
    if not os.path.exists(db_path):
        return

    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT rowid, enable_type FROM notification WHERE LENGTH(enable_type) < 3") as cursor:
            rows = await cursor.fetchall()

        if not rows:
            return

        for rowid, enable_type in rows:
            new_value = (enable_type or '').ljust(3, '1')
            await db.execute('UPDATE notification SET enable_type = ? WHERE rowid = ?', (new_value, rowid))

        await db.commit()

    log.info(f"migrated {len(rows)} notifier row(s) to the 3-bit enable_type shape "
             f"(reply notifications enabled by default, no other settings changed)")


async def init_latest_tweet_on_startup(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute('UPDATE user SET latest_tweet = ?', (get_utcnow(),))
        await db.commit()

    log.info('all latest_tweet timestamps have been updated to current time')
