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
        # Subset of self.apps whose accounts can actually reach X's replies
        # endpoint (some burners are read-limited: auth + notifications feed work,
        # but UserTweetsAndReplies 404s). Determined once at startup. Reply data
        # is public, so ANY capable burner can fetch ANY tracked account's replies.
        self.reply_capable_apps: list[Twitter] = []
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
                self.bot.loop.create_task(self.tweetsUpdater(app)).set_name(f'TweetsUpdater_{account_name}')
            except Exception:
                sys.exit(1)

        # Probe which burners can reach the replies endpoint before starting any
        # reply tasks; disable replies entirely if none can.
        if self.replies_enabled:
            await self._probe_reply_capable()
            if not self.reply_capable_apps:
                self.replies_enabled = False
                log.warning('reply notifications DISABLED — no burner can access the replies endpoint')

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

    async def _probe_reply_capable(self):
        """One-time check of which burners can reach the replies endpoint. Some
        accounts are read-limited: notifications feed works but
        UserTweetsAndReplies 404s ('elevated authorization'). Probed SEQUENTIALLY
        (concurrent probing self-induces timeouts and gives false negatives) with
        one retry, so a transient failure doesn't wrongly exclude a healthy burner."""
        self.reply_capable_apps = []
        capable = []
        for name, app in self.apps.items():
            last_err = None
            for attempt in range(2):
                try:
                    await asyncio.wait_for(get_user_replies(app, 'elonmusk'), timeout=40)
                    self.reply_capable_apps.append(app)
                    capable.append(name)
                    break
                except Exception as e:
                    last_err = e
                    await asyncio.sleep(2)
            else:
                log.warning(f"burner {name} can't access the replies endpoint "
                            f"({type(last_err).__name__}: {str(last_err)[:40]}); excluded from reply polling")
            await asyncio.sleep(1)  # small gap so probing doesn't self-rate-limit
        log.info(f"reply-capable burners: {capable if capable else 'NONE'}")

    def _pick_reply_app(self, username: str) -> Twitter | None:
        # Round robin (keyed by username) over ONLY the reply-capable burners,
        # spreading the per-account replies polling load across them. Reply data
        # is public so any capable burner works for any tracked account.
        if not self.reply_capable_apps:
            return None
        return self.reply_capable_apps[hash(username) % len(self.reply_capable_apps)]

    async def repliesUpdater(self, username: str):
        # Phase-spread initial polls across the period so many tracked accounts
        # don't all fire their replies request in the same instant.
        await asyncio.sleep(random.uniform(0, configs['reply_check_period']))
        while True:
            app = self._pick_reply_app(username)
            if app is None:
                return  # no reply-capable burner; stop quietly (won't be restarted)
            try:
                self.reply_tweets[username] = await get_user_replies(app, username)
            except Exception as e:
                log.error(f'{e} (task: replies updater {username})')
                await asyncio.sleep(configs['tweets_updater_retry_delay'] * 60)
                continue

            await asyncio.sleep(configs['reply_check_period'])

    async def tweetsUpdater(self, app: Twitter):
        updater_name = asyncio.current_task().get_name().split('_', 1)[1]
        while True:
            try:
                # Run the potentially blocking library call in a separate thread
                self.tweets[updater_name] = await asyncio.to_thread(app.get_tweet_notifications)
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

            for client in self.accounts_data.keys():
                if f'TweetsUpdater_{client}' not in running_tasks:
                    log.warning(f'tweets updater {client} : dead')

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
