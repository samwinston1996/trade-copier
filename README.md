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
