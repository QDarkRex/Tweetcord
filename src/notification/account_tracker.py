import asyncio
import os
import random
import sys
import re
import aiohttp
from datetime import datetime, timezone, timedelta

import aiosqlite
import discord
from discord.ext import commands
from tweety import Twitter

from core.classes import ParsedTweet
from configs.load_configs import configs, IS_TRANSLATION_ENABLED
from src.i18n import t
from src.log import setup_logger
from src.notification.display_tools import gen_embed, get_action
from src.notification.get_tweets import get_tweets
from src.notification.reply_fetcher import get_user_replies
from src.notification.utils import is_match_media_type, is_match_type, replace_emoji, get_parsed_tweet
from src.utils import get_accounts, get_lock, get_utcnow
from src.db_function.readonly_db import connect_readonly
from src.db_function.init_db import init_latest_tweet_on_startup

EMBED_TYPE: str = configs['embed']['type']
SERVICE: str = configs['embed']['proxy']['service']
DOMAIN_NAME: str = configs['embed']['proxy']['domain_name']

log = setup_logger(__name__)
lock = get_lock()

# A notifications-feed fetch that never returns used to freeze that burner's feed
# FOREVER: the task stays "alive" (so the monitor's task-existence check reported
# it healthy) while self.tweets[burner] silently went stale, so every account
# tracked by that burner stopped delivering with no error anywhere. Observed
# 2026-07-25: I_KathrinaJKT48 stuck 11h while a fresh manual fetch showed newer
# tweets. Cap the fetch, and treat "no successful fetch in a while" as dead.
FEED_FETCH_TIMEOUT = 90  # seconds for one get_tweet_notifications call
FEED_STALE_SECONDS = 300  # no successful fetch this long => restart the updater

# X rate-limits the per-account replies endpoint per burner. Measured 2026-07-25:
# which burners can reach it FLIPS between runs (1/3/6 ok, then 2/3/4 ok) and
# tracks which ones were recently used — i.e. it's a quota, not a permanently
# read-limited account. So never classify a burner as reply-incapable for good:
# spread reply fetches over ALL burners and rest one briefly when it 404s.
REPLY_COOLDOWN_SECONDS = 900

class AccountTracker():
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.accounts_data = get_accounts()
        self.db_path = os.path.join(os.getenv('DATA_PATH'), 'tracked_accounts.db')
        self.tweets = {account_name: [] for account_name in self.accounts_data.keys()}
        # Replies, keyed by TRACKED USERNAME (not burner) — X has no shared replies
        # feed, so each tracked account is polled individually. See reply_fetcher.py.
        self.reply_tweets: dict[str, list] = {}
        self.apps: dict[str, Twitter] = {}
        # Burners currently resting after the replies endpoint rate-limited them,
        # mapped to the time they can be used again. Reply data is public, so ANY
        # burner can fetch ANY tracked account's replies — spreading the load is
        # what keeps every burner under its quota.
        self.reply_cooldown: dict[str, datetime] = {}
        # When each burner's feed last fetched SUCCESSFULLY — the real liveness
        # signal (a task hung mid-request still counts as "alive").
        self.feed_last_ok: dict[str, datetime] = {}
        self.session = None
        # Responsible for processing queries and writing timestamps
        self.db_write_queue = asyncio.Queue()
        self.latest_tweet_timestamps = {}
        self.timestamps_ready = asyncio.Event()

        self.tasksMonitorLogAt = datetime.now(timezone.utc) - timedelta(hours=configs['tasks_monitor_log_period'])
        # Kill-switch: reply_check_period <= 0 disables ALL reply polling (and the
        # Chromium/Playwright transaction-id sourcing it needs). Lets you turn the
        # feature off with a config edit + restart — no rebuild — if it ever
        # misbehaves, while tweets/retweets/quotes keep working normally.
        self.replies_enabled = configs.get('reply_check_period', 0) > 0
        if not self.replies_enabled:
            log.warning('reply notifications DISABLED (reply_check_period <= 0)')
        bot.loop.create_task(self.setup_tasks())

    async def setup_tasks(self):
        self.session = aiohttp.ClientSession()
        if configs['init_latest_tweet_on_startup']:
            await init_latest_tweet_on_startup(self.db_path)

        # Start the core database workers first
        self.bot.loop.create_task(self.timestamp_updater()).set_name('TimestampUpdater')
        self.bot.loop.create_task(self.db_writer()).set_name('DBWriter')

        # Wait for the initial timestamp load
        await self.timestamps_ready.wait()

        async def authenticate_account(account_name, account_token):
            app = Twitter(account_name)
            max_attempts = configs['auth_max_attempts']
            for attempt in range(max_attempts):
                try:
                    await app.load_auth_token(account_token)
                    return app
                except Exception as e:
                    log.error(f"authentication failed for account: {account_name} [Attempt {attempt + 1}/{max_attempts}]")
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(5)
                    else:
                        log.error(f"persistent authentication failure for account {account_name}")
                        raise
        
        for account_name, account_token in self.accounts_data.items():
            try:
                app = await authenticate_account(account_name, account_token)
                self.apps[account_name] = app
                # Seed the liveness clock so a burner that NEVER completes a fetch
                # is still detected as stalled by tasksMonitor.
                self.feed_last_ok[account_name] = datetime.now(timezone.utc)
                self.bot.loop.create_task(self.tweetsUpdater(app)).set_name(f'TweetsUpdater_{account_name}')
            except Exception:
                sys.exit(1)

        # No startup reply-capability probe: which burners the replies endpoint
        # accepts is a moving quota, not a fixed trait, so probing once both
        # delayed startup and produced a stale answer that concentrated load on
        # whichever burners happened to pass. repliesUpdater spreads across all
        # burners and rests any that get rate-limited (see REPLY_COOLDOWN_SECONDS).

        # Initial user list for notification + replies tasks
        for (username, client_used), _ in self.latest_tweet_timestamps.items():
            self.bot.loop.create_task(self.notification(username, client_used)).set_name(username)
            if self.replies_enabled:
                self.bot.loop.create_task(self.repliesUpdater(username)).set_name(f'RepliesUpdater_{username}')

        self.bot.loop.create_task(self.tasksMonitor()).set_name('TasksMonitor')

    async def timestamp_updater(self):
        """Periodically reads all user timestamps from the DB into a shared dictionary."""
        while True:
            try:
                async with connect_readonly(self.db_path) as db:
                    async with db.execute('SELECT username, client_used, latest_tweet FROM user WHERE enabled = 1') as cursor:
                        new_timestamps = {}
                        async for row in cursor:
                            new_timestamps[(row[0], row[1])] = row[2]
                        self.latest_tweet_timestamps = new_timestamps
                
                if not self.timestamps_ready.is_set():
                    self.timestamps_ready.set()
                    log.info("initial tweet timestamps loaded")

            except Exception as e:
                log.error(f"error in timestamp_updater: {e}")

            # After careful consideration, it was decided to keep it hard-coded, as it makes little sense to allow users to customize this value.
            await asyncio.sleep(60)

    async def db_writer(self):
        """Singleton task to handle all database write operations."""
        while True:
            try:
                username, new_timestamp = await self.db_write_queue.get()
                async with lock:
                    async with aiosqlite.connect(self.db_path, timeout=10) as db:
                        await db.execute('UPDATE user SET latest_tweet = ? WHERE username = ?', (str(new_timestamp), username))
                        await db.commit()
                self.db_write_queue.task_done()
            except Exception as e:
                log.error(f"error in db_writer: {e}")

    async def notification(self, username: str, client_used: str):
        while True:
            await asyncio.sleep(configs['tweets_check_period'])

            last_tweet_at = self.latest_tweet_timestamps.get((username, client_used))
            if not last_tweet_at:
                # This can happen if a user is removed right after the sleep.
                log.warning(f"no timestamp for {username}, task will terminate.")
                break

            # self.tweets[client_used] is a tweety TweetNotifications object (a dict
            # subclass, iterable but NOT a list — no __add__), so list(...) it before
            # concatenating with the plain-list replies.
            latest_tweets = await get_tweets(list(self.tweets[client_used]) + self.reply_tweets.get(username, []), username, last_tweet_at)
            if not latest_tweets:
                continue
            
            newest_timestamp = latest_tweets[-1].created_on
            # Update local cache immediately to prevent re-notification
            self.latest_tweet_timestamps[(username, client_used)] = str(newest_timestamp)
            # Queue the database update
            await self.db_write_queue.put((username, newest_timestamp))

            user = None
            notifications = []
            try:
                async with connect_readonly(self.db_path) as db:
                    db.row_factory = aiosqlite.Row
                    async with db.cursor() as cursor:
                        await cursor.execute('SELECT id FROM user WHERE username = ?', (username,))
                        user = await cursor.fetchone()
                        if user:
                            if IS_TRANSLATION_ENABLED:
                                await cursor.execute('''
                                    SELECT n.*, suc.translate AS server_translate
                                    FROM notification n
                                    JOIN channel c ON n.channel_id = c.id
                                    LEFT JOIN server_user_config suc ON c.server_id = suc.server_id AND n.user_id = suc.user_id
                                    WHERE n.user_id = ? AND n.enabled = 1
                                ''', (user['id'],))
                            else:
                                await cursor.execute('SELECT * FROM notification WHERE user_id = ? AND enabled = 1', (user['id'],))
                            notifications = await cursor.fetchall()
            except aiosqlite.OperationalError as e:
                if "database is locked" in str(e):
                    log.warning(f"database locked while reading notification settings for {username}, this is unexpected but handled.")
                else:
                    raise
            
            if not user:
                continue

            for tweet in latest_tweets:
                log.info(f'find a new tweet from {username}')
                
                content_cache: dict[str, tuple[list[discord.Embed], discord.ui.View, ParsedTweet]] = {}
                
                def gen_view(label: str, url: str):
                    view = discord.ui.View(timeout=5)
                    view.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url))
                    return view
                
                view = None
                if EMBED_TYPE == 'proxy' and configs['embed']['proxy']['original_url_button']:
                    view = gen_view(t('display.button.view_original'), tweet.url)

                for data in notifications:
                    channel = self.bot.get_channel(int(data['channel_id']))
                    if channel is None or not is_match_type(tweet, data['enable_type']):
                        continue
                    
                    lang = (data['server_translate'] or configs['embed']['trans_default_lang']) if IS_TRANSLATION_ENABLED else None
                    
                    if lang not in content_cache:
                        p_tweet, embeds = None, None
                        
                        if EMBED_TYPE == 'built_in':
                            p_tweet = await get_parsed_tweet(tweet, self.session, lang=lang)
                            embeds = gen_embed(tweet, p_tweet)
                            if view is None and p_tweet.media.type == 'video' and configs['embed']['built_in']['video_link_button']:
                                button_url = p_tweet.media.video_link or tweet.url
                                view = gen_view(t('display.button.view_video'), button_url)
                        
                        content_cache[lang] = (embeds, view, p_tweet)

                    current_embeds, current_view, current_p_tweet = content_cache[lang]

                    if not is_match_media_type(current_p_tweet if current_p_tweet else tweet, data['enable_media_type']):
                        continue

                    try:
                        url = tweet.url
                        if EMBED_TYPE == 'proxy':
                            url = url.replace('twitter', DOMAIN_NAME)
                            if IS_TRANSLATION_ENABLED:
                                url += f"/{lang}"

                        mention = f"{channel.guild.get_role(int(data['role_id'])).mention} " if data['role_id'] else ''
                        author, action = tweet.author.name, get_action(tweet)
                        
                        if not data['customized_msg']: msg = configs['default_message']
                        else: msg = re.sub(r":(\w+):", lambda match: replace_emoji(match, channel.guild), data['customized_msg']) if configs['emoji_auto_format'] else data['customized_msg']
                        msg = msg.format(mention=mention, author=author, action=action, url=url)

                        if EMBED_TYPE == 'proxy':
                            await channel.send(msg, view=current_view)
                        else:
                            footer = 'twitter.png' if configs['embed']['built_in']['legacy_logo'] else 'x.png'
                            file = discord.File(f'images/{footer}', filename='footer.png')
                            await channel.send(msg, file=file, embeds=current_embeds, view=current_view)

                    except Exception as e:
                        if not isinstance(e, discord.errors.Forbidden):
                            log.error(f'an error occurred at {channel.mention} while sending notification: {e}')

    def _pick_reply_app(self, username: str) -> tuple[str | None, Twitter | None]:
        """Round-robin (keyed by username) over every burner not currently resting
        off a replies rate-limit, so the reply load is spread evenly instead of
        concentrated on a few burners until they hit their quota."""
        now = datetime.now(timezone.utc)
        available = [(n, a) for n, a in self.apps.items()
                     if self.reply_cooldown.get(n, now) <= now]
        if not available:
            return None, None
        return available[hash(username) % len(available)]

    async def repliesUpdater(self, username: str):
        # Phase-spread initial polls across the period so many tracked accounts
        # don't all fire their replies request in the same instant.
        await asyncio.sleep(random.uniform(0, configs['reply_check_period']))
        while True:
            name, app = self._pick_reply_app(username)
            if app is None:
                # Every burner is resting off a rate-limit; wait and retry rather
                # than giving up on this account permanently.
                await asyncio.sleep(60)
                continue
            try:
                self.reply_tweets[username] = await asyncio.wait_for(
                    get_user_replies(app, username), timeout=60
                )
            except Exception as e:
                # Rest this burner briefly instead of retrying it immediately —
                # the failure is usually its replies quota, and another burner can
                # cover this account on the next pass.
                self.reply_cooldown[name] = (datetime.now(timezone.utc)
                                             + timedelta(seconds=REPLY_COOLDOWN_SECONDS))
                log.warning(f'replies via {name} failed for {username} '
                            f'({type(e).__name__}: {str(e)[:45]}); resting it '
                            f'{REPLY_COOLDOWN_SECONDS // 60}m')

            await asyncio.sleep(configs['reply_check_period'])

    async def tweetsUpdater(self, app: Twitter):
        updater_name = asyncio.current_task().get_name().split('_', 1)[1]
        while True:
            try:
                # Run the potentially blocking library call in a separate thread,
                # capped so one hung request can't stall this burner's feed forever
                # (see FEED_FETCH_TIMEOUT).
                self.tweets[updater_name] = await asyncio.wait_for(
                    asyncio.to_thread(app.get_tweet_notifications),
                    timeout=FEED_FETCH_TIMEOUT,
                )
                self.feed_last_ok[updater_name] = datetime.now(timezone.utc)
            except asyncio.TimeoutError:
                log.warning(f"feed fetch for {updater_name} timed out after {FEED_FETCH_TIMEOUT}s; "
                            f"keeping previous feed and retrying next cycle")
            except KeyError as e:
                # Handle the error thrown by `tweety-ns` mentioned in issue#59
                log.warning(f"handled KeyError in {updater_name}: {e}. This is likely a temporary API response issue from Twitter. Skipping this check.")
            except Exception as e:
                log.error(f'{e} (task : tweets updater {updater_name})')
                log.error(f"an unexpected error occurred, try again in {configs['tweets_updater_retry_delay']} minutes")
                await asyncio.sleep(configs['tweets_updater_retry_delay'] * 60)
                continue
            
            await asyncio.sleep(configs['tweets_check_period'])

    async def tasksMonitor(self):
        """Dynamically monitors tasks based on the live timestamp cache."""
        while True:
            await asyncio.sleep(configs['tasks_monitor_check_period'] * 60)

            running_tasks = {task.get_name() for task in asyncio.all_tasks()}
            users_in_cache = {username for username, _ in self.latest_tweet_timestamps.keys()}
            
            alive_tasks = running_tasks & users_in_cache

            if alive_tasks != users_in_cache:
                dead_tasks = list(users_in_cache - alive_tasks)
                if dead_tasks:
                    log.warning(f'dead tasks : {dead_tasks}')
                    for dead_task_username in dead_tasks:
                        # Find the corresponding client_used from the cache
                        client_used = None
                        for u, c in self.latest_tweet_timestamps.keys():
                            if u == dead_task_username:
                                client_used = c
                                break
                        
                        if client_used:
                            self.bot.loop.create_task(self.notification(dead_task_username, client_used)).set_name(dead_task_username)
                            log.info(f'restart {dead_task_username} successfully using {client_used}')

            if self.replies_enabled:
                reply_task_names = {f'RepliesUpdater_{u}' for u in users_in_cache}
                dead_reply_tasks = reply_task_names - running_tasks
                for task_name in dead_reply_tasks:
                    username = task_name.removeprefix('RepliesUpdater_')
                    self.bot.loop.create_task(self.repliesUpdater(username)).set_name(task_name)
                    log.info(f'restart {task_name} successfully')

            # Restart feed updaters that are dead OR stalled. Task existence alone
            # is not liveness: a task hung inside its fetch stays "alive" while its
            # feed goes stale and every account on that burner silently stops.
            now = datetime.now(timezone.utc)
            for client in self.accounts_data.keys():
                task_name = f'TweetsUpdater_{client}'
                is_dead = task_name not in running_tasks
                last_ok = self.feed_last_ok.get(client)
                stale_for = (now - last_ok).total_seconds() if last_ok else None
                is_stalled = stale_for is not None and stale_for > FEED_STALE_SECONDS

                if not (is_dead or is_stalled):
                    continue

                reason = 'dead' if is_dead else f'stalled ({stale_for:.0f}s since last successful fetch)'
                log.warning(f'tweets updater {client} : {reason} — restarting')

                if not is_dead:
                    for task in asyncio.all_tasks():
                        if task.get_name() == task_name:
                            task.cancel()
                            break

                app = self.apps.get(client)
                if app is None:
                    log.error(f'cannot restart tweets updater {client}: no authenticated client')
                    continue
                # Reset the clock so a slow restart isn't immediately re-flagged.
                self.feed_last_ok[client] = now
                self.bot.loop.create_task(self.tweetsUpdater(app)).set_name(task_name)

            if (datetime.now(timezone.utc) - self.tasksMonitorLogAt).total_seconds() / 3600 >= configs['tasks_monitor_log_period']:
                log.info(f'alive tasks : {list(alive_tasks)}')
                for client in self.accounts_data.keys():
                    if f'TweetsUpdater_{client}' in running_tasks:
                        log.info(f'tweets updater {client} : alive')
                self.tasksMonitorLogAt = datetime.now(timezone.utc)


    async def addTask(self, username: str, client_used: str):
        """Adds a new user to the live cache and starts their notification + replies tasks."""
        # Add to live cache first
        self.latest_tweet_timestamps[(username, client_used)] = get_utcnow()

        # Start the tasks
        self.bot.loop.create_task(self.notification(username, client_used)).set_name(username)
        if self.replies_enabled:
            self.bot.loop.create_task(self.repliesUpdater(username)).set_name(f'RepliesUpdater_{username}')
        log.info(f'new task {username} added successfully using {client_used}')

    async def removeTask(self, username: str):
        """Removes a user from the live cache and cancels their notification + replies tasks."""
        key_to_remove = None
        # Create a copy of keys for safe iteration
        for u, c in list(self.latest_tweet_timestamps.keys()):
            if u == username:
                key_to_remove = (u, c)
                break

        # Remove from cache so the monitor doesn't restart it
        if key_to_remove and key_to_remove in self.latest_tweet_timestamps:
            del self.latest_tweet_timestamps[key_to_remove]

        self.reply_tweets.pop(username, None)

        # Cancel the running tasks (notification + repliesUpdater)
        target_names = {username, f'RepliesUpdater_{username}'}
        for task in asyncio.all_tasks():
            if task.get_name() in target_names:
                task.cancel()
                log.info(f'task {task.get_name()} has been cancelled')

    async def close(self):
        """Closes the persistent session."""
        if self.session:
            await self.session.close()
            log.info("account tracker session closed")
