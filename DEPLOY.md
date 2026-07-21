# Tweetcord — Deploy to a 24/7 Linux server

This is the same setup that runs locally, packaged for a Linux server over SSH.
It uses Docker with `restart: unless-stopped`, so it recovers from crashes and reboots.

> **Run only ONE instance per bot/token.** The Discord `BOT_TOKEN` and the X
> `auth_token` must not be used by two running bots at once (duplicate posts,
> gateway conflicts, and a higher chance of X flagging the account). When you go
> live on the server, **stop the local one** first: `docker compose stop`.

---

## 0. What you need
- SSH access to the server: `user@SERVER_IP`
- `sudo` on the server
- Your two secrets: the Discord **BOT_TOKEN** and the X **auth_token**

## 1. Copy the bundle to the server
From your Windows machine (PowerShell), in the folder that holds `tweetcord-deploy.tar.gz`:
```
scp tweetcord-deploy.tar.gz user@SERVER_IP:~/
```

## 2. SSH in and unpack
```
ssh user@SERVER_IP
mkdir -p ~/tweetcord && tar -xzf ~/tweetcord-deploy.tar.gz -C ~/tweetcord
cd ~/tweetcord
```

## 3. Install Docker (skip if already installed)
```
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER      # lets you run docker without sudo
sudo systemctl enable --now docker # start Docker now + on every boot
```
Then **log out and back in** (or run `newgrp docker`) so the group change applies.
Verify:
```
docker --version && docker compose version
```

## 4. Create the .env (secrets — never committed)
```
cp .env.example .env
nano .env
```
Fill in your real values, then save (in nano: `Ctrl+O`, `Enter`, `Ctrl+X`):
```
BOT_TOKEN=<your discord bot token>
TWITTER_TOKEN=burner1:<your X auth_token>
DATA_PATH=./data
```

## 5. Start it
```
docker compose up -d --build
```
First build pulls Python 3.11 + installs dependencies (~1–2 min). Later starts are instant.

## 6. Confirm it's working
```
docker compose ps              # STATUS should say "Up"
docker compose logs -f         # Ctrl+C to stop watching (bot keeps running)
```
In the logs you want to see:
- `Applied tweety x-client-transaction-id bypass patch ...`
- `Tweetcord#.... is online`
- `tweets updater burner1 : alive`
- and **no** `authentication failed` / `animation key indices` lines.

## 7. Use it
In Discord: `/add notifier <username> #channel` (handle only, no `@`).
It forwards tweets posted **after** you add the notifier, polling every
`tweets_check_period` seconds (set in `configs.yml`, currently 12).

---

## Everyday management
| Action | Command (run inside `~/tweetcord`) |
|---|---|
| View logs | `docker compose logs -f` |
| Restart (e.g. after editing `configs.yml`) | `docker compose restart` |
| Stop | `docker compose stop` |
| Start | `docker compose up -d` |
| Update poll interval | edit `configs.yml` → `docker compose restart` |

`configs.yml` is mounted, so config edits only need a **restart**. Code changes
(e.g. removing the patch) need a **rebuild**: `docker compose up -d --build`.

## About the stopgap patch (important)
`transaction_patch.py` works around upstream tweety issue #295 — X rewrote its
web client (webpack → Vite) and broke tweety's `x-client-transaction-id`
computation. The patch makes the bot send a dummy id, which X currently accepts.

When tweety fixes #295 upstream:
1. `git pull` (or update tweety), **delete `transaction_patch.py`**, and remove
   its `import transaction_patch` line from `bot.py`.
2. `docker compose up -d --build`.

Tracking: https://github.com/mahrtayyab/tweety/issues/295

## Moving your existing notifiers (optional)
Notifier subscriptions live in `data/tracked_accounts.db`. This bundle ships
**without** local data (fresh start). To carry over what you set up locally,
copy your local `data/` folder into `~/tweetcord/data/` on the server before
step 5, or just re-run `/add notifier` on the server.
