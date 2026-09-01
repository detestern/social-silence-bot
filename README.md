<p align="center">
  <img src="logo.JPG" alt="logo" width="400">
</p>

<h1 align="center">Social silence bot</h1>

<p align="center">
  A Gemini-powered filter for work chats: reads hundreds of messages a day and surfaces only what actually matters.
</p>

---

## Why

If your job means being in a dozen work-related Telegram group chats (a teacher, a manager, anyone juggling several class/team chats), you physically can't read everything — yet you're afraid of missing the one message that's actually addressed to you. The usual fix is scrolling through all of it manually, hours a day.

This bot takes a different approach: it reads all your chats through your own Telegram account, runs every message through Gemini, and notifies you **only** about what's genuinely important — with a direct link to the message or a forward of the original. Everything else is quietly archived and rolled up once a day into a short summary.

## Features

- **Private chat selection** — the bot only reads chats you've explicitly allowed, nothing else
- **Three priority tiers** — direct replies/mentions/DMs are flagged instantly with no AI call; messages with urgency signals ("urgent", "today") get a single out-of-order AI check; everything else is batched and processed every N minutes — saves both money and time
- **Voice messages** — transcribed to text and processed like any other message
- **Attachments** — photos and files are analyzed by content, not just by filename
- **Fragment merging** — if someone types their thought across several one-word messages, the bot merges them before analysis
- **Priority rules** — explicitly tell it "the 10th-grade trip is important" with an expiry date and scope (a specific chat group or everywhere)
- **Daily digest** — a short recap of what happened in each chat, with repetitive replies (yes/no/I'll be there) collapsed into one sentence
- **Two bots** — one for configuration only, one for notifications only, so the settings UI never gets mixed with pushes
- **Resilient by design** — reconnects on network drops, rotates multiple Gemini keys, lets you add emergency keys straight through the bot
- **In-bot login** — no server access needed to connect a Telegram account

## How it works

```
Telegram account (Telethon userbot)
        │
        ▼
  adapters/telegram.py  ──> normalizes events into a common format
        │
        ▼
  core/prefilter.py  ──> instant / escalated / batched
        │
        ▼
  core/classifier.py  ──> Gemini: important or not (aware of profile & rules)
        │
        ▼
  core/notify.py  ──> alert bot: message link or forwarded original
```

Two separate Telegram bots (aiogram) plus one Telethon client (a real human account, not the Bot API — that's the only way to read group chats as a member). Storage is SQLite, plenty for a single person.

The architecture is built for multiple sources from day one — `adapters/base.py` defines the contract any adapter must follow, so Pachca/Gmail/Yandex Mail can be added without touching the rest of the pipeline.

## Tech stack

Python 3.12 · aiogram 3 · Telethon · SQLAlchemy (async) + SQLite · Google Gemini API

## Quick start

### 1. Clone and install dependencies

```bash
git clone <your-repo-url>
cd triage-bot
python3.12 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Get credentials

- **TG_API_ID / TG_API_HASH** — [my.telegram.org](https://my.telegram.org) → API development tools
- **TG_BOT_TOKEN** and **TG_ALERT_BOT_TOKEN** — two separate bots via [@BotFather](https://t.me/BotFather) (`/newbot`)
- **GEMINI_API_KEY** — [aistudio.google.com](https://aistudio.google.com/apikey); you can list several keys comma-separated, they're rotated automatically

### 3. Configure `.env`

```bash
cp .env.example .env
```

Fill in the values from step 2. `ALLOWED_TG_USER_IDS` — the Telegram user IDs of everyone allowed to use the bots (get yours from [@userinfobot](https://t.me/userinfobot)), comma-separated, no spaces.

### 4. Run it

```bash
python main.py
```

### 5. Authorize chat reading

In the settings bot:

```
/login
```

The bot will ask for a phone number, then the code Telegram sends (and a 2FA password if you have one) — right in the chat, no server access required.

### 6. Set up what to monitor

```
/chats      — pick which chats to watch
/profile    — a couple of lines about yourself
```

In the alert bot — send `/start` once, so it knows where to send notifications.

## Commands

| Command | What it does |
|---|---|
| `/login` | Authorize chat reading (in-chat dialogue) |
| `/platform` | Choose which platform your settings apply to |
| `/chats` | Pick chats to monitor |
| `/find text` | Find a chat by name |
| `/monitored` | Stop monitoring a chat |
| `/tag text` | Assign a group tag to some chats |
| `/tags` | Show all tags and what's in them |
| `/profile [text]` | View/edit your description |
| `/add_rule text` | Add a priority rule |
| `/rules` | List active rules |
| `/stats` | Today's summary |
| `/settings` | Batch interval, daily digest time and on/off |
| `/api key1,key2` | Add backup Gemini keys on the fly |
| `/fresh`, `/daily` | Force-run the batch/digest now (for testing) |

## Project structure

```
adapters/       — source adapter contract + Telegram implementation (Telethon)
bot/            — settings and alert bot handlers (aiogram)
core/           — the logic: prefilter, classifier, listener, scheduler, digest
db/             — SQLAlchemy models and DB session
scripts/        — fallback console-based login
main.py         — entry point, starts both bots and background tasks
```

## Privacy

The bot only reads chats explicitly allowed via `/chats` — everything else never reaches the database or the AI. The Telethon session (`*.session`) is full access to a Telegram account, and `.env` holds every token and key; both are already in `.gitignore` and must never be committed.
