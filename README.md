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
