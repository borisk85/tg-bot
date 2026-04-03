# Personal AI Agent — Telegram Bot

A personal AI assistant built on Claude API, running 24/7 in Telegram. Handles calendar, email, tasks, crypto prices, reminders, voice messages, image generation, and more — all through natural conversation.

## What it does

You talk to it like a personal assistant. It figures out what tool to use:

- "What's on my calendar this week?" → Google Calendar
- "Read my emails from Amazon" → Gmail
- "Remind me to call dentist at 3pm" → Reminder (Redis)
- "What's the price of BTC?" → CoinGecko / Binance
- "Generate an image of a futuristic city" → fal.ai FLUX
- "Transcribe this" → sends a voice message → Groq Whisper

## Stack

| Layer | Tech |
|---|---|
| Language | Python 3.12 |
| Bot framework | python-telegram-bot v21 |
| AI | Anthropic Claude (claude-sonnet-4-6) |
| Memory / Reminders | Redis |
| Deployment | Railway (auto-deploy on git push) |
| Voice transcription | Groq Whisper Large V3 |
| Image generation | fal.ai FLUX dev |

## Tools available to the agent

**Google Workspace**
- `calendar_list_events` / `calendar_create_event` / `calendar_delete_event`
- `gmail_search` / `gmail_read` / `gmail_send` / `gmail_trash` / `gmail_mark_spam` / `gmail_unsubscribe`
- `tasks_list` / `tasks_create` / `tasks_complete` / `tasks_update` / `tasks_delete`
- `drive_search` / `drive_read` / `drive_create_doc` / `drive_create_sheet` / `drive_create_folder` / `drive_move_file` / `drive_delete`

**Market data**
- `get_crypto_prices` — BTC/ETH/SOL and other major coins (CoinGecko + Binance fallback)
- `search_token` — any token by name, ticker, or contract address (DexScreener)
- `get_market_price` — stocks, indices (NASDAQ, S&P500), metals (gold, silver), oil (yfinance)
- `alert_price_set` / `alert_price_list` / `alert_price_cancel` — price alerts with background job every 5 min

**Utilities**
- `web_search` — Brave Search
- `get_weather` — OpenWeatherMap (current + 5-day forecast)
- `reminder_set` / `reminder_list` / `reminder_cancel` — reminders stored in Redis
- `generate_image` — text-to-image via fal.ai FLUX dev
- `memory_save` / `memory_list` / `memory_delete` — long-term memory per user (injected into system prompt)
- `get_current_datetime` — current date/time in user's timezone

**Document handling**
- Read PDF and Word files sent to chat
- Upload any file or photo to Google Drive
- Attach files from chat to Gmail messages

## Architecture

```
User → Telegram
            → bot.py
                → Claude API (tool_use agent loop)
                    → execute_tool()
                        → Google APIs / Redis / yfinance / DexScreener / ...
                → reply → User
```

The agent loop runs until Claude stops requesting tools or hits the iteration limit. All conversation history is stored in Redis (last 30 messages, TTL 7 days). Long-term facts about the user are stored separately and injected into every system prompt.

## Automated features

- **Morning digest** — daily at 11:00 Almaty: weather + calendar events + tasks
- **Competitive radar** `/ai_agents_digest` — every Monday at 12:00 Almaty: AI agent market news filtered by niche (Brave Search + HackerNews)
- **Price alerts** — background job every 5 minutes checks all active price alerts and notifies when triggered

## Voice messages

Send a voice message → bot transcribes via Groq Whisper Large V3 → passes text to Claude as if typed. Free tier: 7200 min/day.

## Setup

### Environment variables

```
TELEGRAM_TOKEN          — from @BotFather
ANTHROPIC_API_KEY       — Anthropic API key
GOOGLE_CLIENT_ID        — Google OAuth2 client ID
GOOGLE_CLIENT_SECRET    — Google OAuth2 client secret
GOOGLE_REFRESH_TOKEN    — obtained via auth_google.py
REDIS_URL               — Redis connection URL
BRAVE_API_KEY           — Brave Search API
OPENWEATHER_API_KEY     — OpenWeatherMap API
FAL_API_KEY             — fal.ai
GROQ_API_KEY            — Groq (voice transcription)
```

### Google OAuth2

```bash
pip install -r requirements.txt
python auth_google.py   # opens browser, saves refresh token
```

Copy the printed `GOOGLE_REFRESH_TOKEN` to your environment variables.

> Note: keep your Google Cloud Console app in **Published** mode (not Testing) to prevent refresh tokens from expiring every 7 days.

### Run locally

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your keys
python bot.py
```

### Deploy to Railway

1. Fork this repo
2. Create a Railway project, connect your GitHub repo
3. Add all environment variables in Railway dashboard
4. Push to `master` — Railway deploys automatically

```bash
git push origin master
```

## Adding a new tool

1. Add tool definition to `TOOLS` list in `bot.py`
2. Add handler case in `execute_tool()` in `bot.py`
3. Add any new dependencies to `requirements.txt`
4. `git push` — Railway deploys automatically

## Key technical notes

- **Agent loop**: Claude runs tools in a loop until it decides to respond. Max iterations prevent infinite loops.
- **Orphan tool_result fix**: When conversation history is trimmed to 30 messages, orphaned `tool_result` blocks at the start are removed — otherwise Claude API returns 400.
- **Media groups (albums)**: Buffered for 1.5s via `_media_group_buffer` + asyncio before processing.
- **Pending attachments**: Files/photos sent to chat are stored in `_pending_attachments[user_id]` and automatically attached to the next `gmail_send` call.
- **Voice messages**: `message.text` is read-only in python-telegram-bot v21 — voice handler calls `run_agent()` directly instead of mutating the message object.
- **Local imports inside execute_tool**: Use `import X as _X` for local imports — a plain `import re` inside any branch makes `re` local to the entire function in Python, breaking regex in other branches.

## License

MIT
