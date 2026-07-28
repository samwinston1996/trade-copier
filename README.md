# Trade Copier

Docker-based trade copier stack for MetaTrader 5 — part of the **Jarvis** AI trading ecosystem. Runs 24/7 on a VPS, connecting MT5 terminals to an automated copy-trading pipeline.

## Architecture

```
┌──────────┐     ┌──────────┐     ┌──────────┐     ┌────────┐
│  Router   │────▶│  Bridge   │────▶│ Listener │────▶│  MT5   │
└──────────┘     └──────────┘     └──────────┘     └────────┘
```

| Service | Role |
|---|---|
| **router** | Routes incoming trade signals to the appropriate bridge instance |
| **bridge** | Translates signals into MT5-compatible commands, manages connection state |
| **listener** | Monitors MT5 terminal(s) for account events, positions, and execution results |
| **mt5** | MetaTrader 5 terminal running in a Wine/Windows environment |

## Stack

- **Python 3.11** — all services (router, bridge, listener)
- **MetaTrader 5** — via official Python package (`MetaTrader5`)
- **Docker Compose** — orchestration with health checks and restart policies
- **Hermes Agent** — AI-driven monitoring and orchestration (see `JARVIS-BLUEPRINT.md`)

## Quick Start

```bash
# Clone and navigate
git clone https://github.com/samwinston1996/trade-copier.git
cd trade-copier

# Copy environment file (never commit the real .env)
cp .env.save .env
# Edit .env with your actual credentials

# Start the stack
docker compose up -d

# Check logs
docker compose logs -f
```

## Deployment Stages

This stack follows a strict staged rollout via the Jarvis ecosystem:

| Stage | Status | Description |
|---|---|---|
| **Observe** | ✅ Current | Monitor MT5 connections, log signals — no action taken |
| **Paper** | ⏳ Next | Execute trades in a simulated/demo environment |
| **Live** | 🔒 Manual gate | Real trading with position caps and kill switch |

## Guardrails

- **Observe-only by default** — no live orders without explicit per-trade confirmation
- **Kill switch** — cuts the connection immediately on anomaly detection
- **Position size limits** — hard caps enforced at the bridge level

## Signal Parsing

The router supports **two parsing pathways** to handle both text and image-based signals:

| Pathway | Model | When |
|---------|-------|------|
| **Text** | `deepseek/deepseek-v4-flash` (configurable) | Plain text messages from the channel |
| **Image** | `google/gemini-3.1-flash-lite-preview` (configurable) | Messages with chart screenshots showing coloured SL/TP levels |

The listener automatically downloads images from Telegram messages, base64-encodes them, and sends them to the router. The router detects the presence of image data and routes to the appropriate model with the correct prompt.

## Extra Added Features

Beyond the core pipeline, the system has been hardened and extended with value-added capabilities that differentiate it from basic copier solutions:

### 🔄 Dual Delivery Redundancy
**Telethon event handler + 30-second polling loop.** Signals are delivered via real-time events AND a periodic polling fallback that catches any messages the event handler may have missed (Telethon subscription fade, reconnection gaps). Duplicate prevention via msg_id comparison and debounce tracking ensures zero LLM cost when no messages are missed. — *Commit `7b1b161`*

### 🧠 Zero-LLM-Cost Regex Pre-Filter
Common signal formats (Distinct Forex, BotX, structured Entry:/SL:/TP: patterns) are parsed by regex before any AI model is invoked — zero inference cost, zero latency. Only unfamiliar formats fall through to the LLM. The pre-filter returns `None` on partial matches so the LLM fallback is never blocked. — *Commits `51d184d`, `42b1a2b`*

### 🔍 Dual AI Parser (Text + Vision)
Signals are parsed by two independent AI models: **DeepSeek V4 Flash** for text-based signals and **Gemini Flash Lite** for chart screenshots. The router auto-detects the input type and routes to the correct model with a domain-specific prompt. Image parsing uses a zone-based algorithm that reads entry/SL/TP from coloured chart zones and right-panel price boxes — no manual entry needed. — *Commits `946f40c`, `6908fe5`, `38d97b7`, `c66517a`*

### 🧩 Per-Channel State Machine
Each signal source operates on its own independent state machine. Tracks the current and previous signal per channel including entry price, SL, TP array, order type, and msg_id. When a signal is deleted in the source channel, the state automatically reverts to the previous signal and triggers a cleanup of the corresponding MT5 orders. Out-of-order signals (msg_id ≤ current) are rejected at the router level. — *Commits `51d184d`, `069b07c`*

### 🛡️ Position Integrity Guards
- **Exclude-Tickets on Trade Rotation:** When a new signal replaces an old one, the freshly placed order's ticket IDs are passed as `exclude_tickets` to `close_channel`, preventing the newly opened trade from being self-cancelled. — *Commit `be48777`*
- **Atomic Duplicate Detection:** A thread-safe `executed_signals` set with lock guarantees that identical signals are never executed twice, even under concurrent delivery. — *Commit `51d184d`*
- **50-Pip Minimum SL:** Image-sourced signals require at least 50 pips between entry and SL — preventing dangerously tight stops from being placed. — *Commit `1df8b0f`*

### 🧹 TP3 Auto-Cleanup
When a channel announces that TP3 was hit (e.g., "TP 3 HIT! 120+ PIPS PROFIT ✅"), the system automatically cancels all pending orders for that channel. Uses a zero-LLM-cost regex pre-filter that matches real-world patterns across all monitored channels. The `pending_only` mode on `close_channel` ensures only pending orders are cancelled — open positions in profit are left untouched. Tested against 21 real channel messages with 100% accuracy. — *Commit `6d03bca`*

### 🔗 Emoji-Safe Cross-Channel Matching
Channel names containing emoji (💯, 👑) or Unicode formatting (bold, italic) are handled correctly across the entire pipeline. MT5 silently strips non-ASCII characters from order comments, so the system uses ASCII-stripped comparison keys for comment matching. This ensures `close_channel`, `cancel_pending`, and position management work reliably regardless of Telegram channel naming conventions. — *Commits `a4a537b`, `6531d97`*

### 🔄 Self-Healing Listener
A cron job restarts the Telegram listener every 4 hours to prevent Telethon subscription fade — a known issue where long-running Telegram clients gradually lose event delivery on specific channels. The restart is a no-agent script that runs silently in the background. — *Commit `069b07c`*

### 📐 Market-Aware Order Placement
The bridge evaluates the current market price against the signal entry and automatically selects the correct order type:
- **LIMIT** when entry is beyond market (price will come to you)
- **STOP** when entry is past market (price will go through your level)
- **MARKET** when entry is within 20 pips of current price
- 20-pip threshold ensures orders are placed efficiently without unnecessary pending orders. — *Commits `181cb04`, `d1c0768`, `d52c37e`*

### Configuration

All sensitive config is managed via `.env` (never committed).

**Required:**

| Variable | Description |
|----------|-------------|
| `TG_API_ID` | Telegram API ID (from my.telegram.org) |
| `TG_API_HASH` | Telegram API hash |
| `HERMES_WEBHOOK_URL` | Router webhook URL (`http://router:8000/webhook`) |
| `HERMES_WEBHOOK_TOKEN` | Shared secret for listener→router auth |
| `HERMES_API_URL` | Hermes/OpenRouter API endpoint |
| `HERMES_API_KEY` | Hermes/OpenRouter API key |
| `BRIDGE_TOKEN` | Shared secret for router→bridge auth |
| `MT5_LOGIN` | MT5 account login |
| `MT5_PASSWORD` | MT5 account password |
| `MT5_SERVER` | MT5 broker server |

**Optional:**

| Variable | Default | Description |
|----------|---------|-------------|
| `VISION_MODEL` | `google/gemini-3.1-flash-lite-preview` | Model for image signal parsing |
| `TEXT_MODEL` | `deepseek/deepseek-v4-flash` | Model for text signal parsing |
| `MIN_CONFIDENCE` | `0.8` | Minimum confidence to auto-forward |
| `MAX_IMAGE_KB` | `512` | Max image size to download (listener) |
| `ALERT_BOT_TOKEN` | — | Telegram bot token for alerts |
| `ALERT_CHAT_ID` | — | Chat ID for alert delivery |
| `RISK_PERCENT` | `1.0` | Risk per trade (bridge) |
