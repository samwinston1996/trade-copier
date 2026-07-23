# Trade Copier — System Blueprint

> **Multi-container MT5 trade copier:** Telegram signal channel → Listener → Router (LLM parse) → Bridge (risk gates) → MT5 execution.
>
> Version: 4.0 · Last updated: July 23, 2026

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture](#2-architecture)
3. [Component: Listener](#3-component-listener)
4. [Component: Router](#4-component-router)
5. [Component: Bridge](#5-component-bridge)
6. [Component: MT5 (MetaTrader 5)](#6-component-mt5)
7. [Signal Processing Pipeline](#7-signal-processing-pipeline)
8. [Risk Gates & Guards](#8-risk-gates--guards)
9. [Trade Lifecycle](#9-trade-lifecycle)
10. [Known Issues & Edge Cases](#10-known-issues--edge-cases)
11. [Fixes Applied (v4.0)](#11-fixes-applied-v40)
12. [Debugging Guide](#12-debugging-guide)
13. [Configuration Reference](#13-configuration-reference)

---

## 1. System Overview

### Purpose

Automatically copy trade signals from Telegram channels to a MetaTrader 5 (MT5) trading account. The system monitors multiple Telegram channels, parses text and image signals using LLMs (DeepSeek, Gemini), validates them through risk gates, and executes trades on MT5.

### Key Design Principles

- **Race for price:** New trade is placed BEFORE cleaning up old trades (minimizes slippage)
- **Per-channel state machine:** Each channel independently tracks its latest signal, enabling reconciliation on delete/edit
- **LLM-first with regex fallback:** Text signals use regex pre-filter (zero cost) with LLM fallback; image signals use Gemini Vision
- **Deterministic risk gates:** Bridge enforces ALL risk rules — LLM cannot bypass them
- **Least privilege:** Router only sees parsed signals; Bridge only sees trade requests; neither knows the Telegram API credentials

### Tech Stack

| Component | Language | Framework | Key Libraries |
|-----------|----------|-----------|---------------|
| Listener | Python 3.10 | Telethon | httpx, asyncio |
| Router | Python 3.10 | FastAPI | httpx, Pydantic |
| Bridge | Python 3.10 | FastAPI | mt5linux, Pydantic |
| MT5 | Wine + MT5 | — | gmag11/metatrader5_vnc:2.3 |

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        TELEGRAM CHANNELS                                │
│  BotX, Trade.inn FOREX, GOLD_GBP_KILLER, AK Capital VIP, star fx ...   │
└──────────┬──────────────────────────────────────────────────────────────┘
           │ NewMessage / MessageEdited / MessageDeleted events
           ▼
┌──────────────────────────────────────────────────────────────────────┐
│  LISTENER (Telethon userbot)                                         │
│  • Watches TG_CHANNELS via events.NewMessage()                       │
│  • Downloads images ONLY for IMAGE_CHANNEL_IDS                       │
│  • Text hash dedup + 3-second debounce (prevents duplicate pushes)   │
│  • 3-retry push with exponential backoff → dead-letter queue         │
│  • REST webhook POST to Router                                       │
└──────────────────────────┬───────────────────────────────────────────┘
                           │ POST /webhook (JSON payload)
                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│  ROUTER (FastAPI :8080)                                              │
│  • Routes to VISION or TEXT parser based on IMAGE_CHANNEL_IDS       │
│  • Regex pre-filter for known formats (zero LLM cost)               │
│  • LLM fallback: DeepSeek (text) / Gemini (vision)                  │
│  • Signal validation: SL check, confidence check, SL distance guard │
│  • Per-channel state machine (JSON-persisted)                       │
│  • Sends trade to Bridge → extracts tickets → conditionally closes  │
│  • Sends Telegram alerts on execution                               │
└──────────────────────────┬───────────────────────────────────────────┘
           │ POST /trade  │ POST /close_channel
           ▼              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  BRIDGE (FastAPI :8002, network_mode: service:mt5)                  │
│  • Atomic duplicate detection (threading.Lock + signal_id set)      │
│  • Per-channel MAX_PER_CHANNEL_TRADES cap                           │
│  • Global MAX_GLOBAL_TRADES safety net                              │
│  • Daily loss check (MAX_DAILY_LOSS_PERCENT)                        │
│  • 20-pip rule: ≥20 pips → LIMIT/STOP, <20 pips → MARKET           │
│  • SL direction validation (SL must be on correct side of entry)    │
│  • Stale signal check (market moved >0.5% since signal)             │
│  • order_send.py subprocess (with fallback to direct mt5 call)      │
│  • /close_channel: closes positions + cancels pending orders        │
│  • Safe rpyc iteration (iterate_safe with EOFError retry)           │
└──────────────────────────┬───────────────────────────────────────────┘
                           │ mt5.order_send()
                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│  MT5 (Wine + MetaTrader 5, :3000 mt5linux RPC)                     │
│  • Headless terminal via Xvfb                                       │
│  • VNC access on :3001 (for debugging)                              │
│  • mt5linux RPC server on :3000                                     │
└──────────────────────────────────────────────────────────────────────┘
```

### Container Network Layout

```
┌───────────┐     ┌───────────┐     ┌───────────┐     ┌───────────┐
│  Listener  │────▶│  Router   │────▶│  Bridge   │────▶│    MT5    │
│  (bridge)  │     │  (bridge) │     │(service:  │     │  (bridge) │
└───────────┘     └───────────┘     │  mt5)     │     └───────────┘
                                    └─────┬─────┘           │
                                          │                 │
                                          │ network_mode:   │
                                          │ service:mt5     │
                                          └─────────────────┘
```

---

## 3. Component: Listener

### 3.1 Purpose

Acts as a Telegram userbot — logs in as Sam's account, watches specified channels, and pushes every new/edited/deleted message to the Router via webhook.

### 3.2 Startup

1. Connects to Telegram using `TG_API_ID` + `TG_API_HASH` + session file at `/data/tg_session`
2. Logs in as the user
3. Registers event handlers for all channels in `TG_CHANNELS`
4. If `TG_CHANNELS` is empty, enters **discovery mode** — logs all incoming messages for channel identification

### 3.3 Event Handlers

#### `events.NewMessage(chats=CHANNELS)`

```python
async def on_new(event):
    # 1. Debounce check: skip if same msg_id processed within 3 seconds
    # 2. Build payload: text, media, image_data (if image channel)
    # 3. Push to Router via POST /webhook
    # 4. On success: store (chat_id, msg_id) → (text_hash, timestamp)
```

#### `events.MessageEdited(chats=CHANNELS)`

```python
async def on_edit(event):
    # 1. Debounce check: skip if same msg_id processed within 3 seconds
    # 2. Text hash check: skip if text unchanged (Telegram auto-formatting)
    # 3. Build payload with current message text
    # 4. Push to Router via POST /webhook
    # 5. On success: update stored hash
```

#### `events.MessageDeleted(chats=CHANNELS)`

```python
async def on_delete(event):
    # Push delete event to Router (no hash tracking)
    # Router handles: revert channel state, close all positions for this channel
```

### 3.4 Image Download

- **Only for IMAGE_CHANNEL_IDS** — defined in `.env`
- Downloads via `m.download_media(bytes)`
- Max 512KB (configurable via `MAX_IMAGE_KB`)
- Base64-encodes and includes as `image_data` in payload
- Non-image channels with media: `image_data` is None, Router routes to TEXT parser

### 3.5 Push Mechanism

- 3 attempts with exponential backoff (2s, 4s, 8s)
- On failure: dead-letter to `/data/failed_pushes.jsonl`
- Hash stored ONLY on successful push (prevents signal loss on transient failures)

### 3.6 Debounce (Fix D — v4.0)

- `DEBOUNCE_SECONDS = 3` — skips subsequent events for the same (chat_id, msg_id) within 3 seconds
- Prevents double-push when both `NewMessage` and `MessageEdited` fire for the same message
- Works alongside the existing text hash dedup (which catches unchanged edits)

---

## 4. Component: Router

### 4.1 Purpose

Receives webhook payloads from the Listener, determines the parser (text vs image), parses the signal, validates it, and forwards to the Bridge.

### 4.2 API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Health check |
| `/webhook` | POST | Receive signal payload from Listener |

### 4.3 Webhook Handler Flow

```
POST /webhook
  │
  ├─ DELETE event → revert channel_state → /close_channel
  │
  ├─ IMAGE channel (has_image + is_image_channel)
  │   └─ Gemini Vision LLM → parse signal
  │
  ├─ TEXT channel (ev.text.strip())
  │   ├─ Regex pre-filter → direct parse (conf=1.0, zero LLM cost)
  │   └─ LLM fallback → DeepSeek text model
  │
  └─ Empty message → "ignored"

  ── Post-parse guards ──
  ├─ Action == IGNORE → return
  ├─ SL missing → reject (conf < 0.5)
  ├─ Confidence < 0.5 → reject
  ├─ Distinct Forex single-TP → reject (marketing)
  ├─ Image signal SL < 50 pips → reject (results screenshot)

  ── Forward to Bridge ──
  ├─ Build trade body (action, symbol, entry, sl, tp, ...)
  ├─ Image signals: prepend 100-pip safety TP
  ├─ Entry-specified signals: convert order_type to "limit"
  ├─ POST /trade → extract ticket IDs
  │   ├─ If duplicate_ignored → skip close_channel (Fix B)
  │   ├─ If tickets empty → skip close_channel (Fix B, safety net)
  │   └─ If tickets exist → check if close needed (Fix C)
  ├─ Conditional POST /close_channel:
  │   ├─ Skip if same direction + same entry (repost)
  │   ├─ Close if direction changed (reversal)
  │   └─ Close if same direction, different entry (new signal)
  └─ Update channel_state

  ── Alert ──
  ├─ Bridge rejected → alert with error
  └─ Bridge accepted → alert with execution result
```

### 4.4 Parsers

#### Text Parser (DeepSeek)

- System prompt: Instructions for parsing Telegram trade signals
- Temperature: 0 (deterministic)
- Returns JSON: `{action, symbol, order_type, entry, sl, tp, confidence, manage_op}`
- Handles: BUY/SELL signals, CLOSE/MODIFY commands, IGNORE for non-signals

#### Image Parser (Gemini Vision)

- Model: `google/gemini-3.1-flash-lite-preview` (configurable)
- System prompt: Zone-based chart parsing instructions
- Returns JSON: `{action, symbol: "XAUUSD", order_type: "limit", entry, sl, tp, confidence}`
- Keywords: Must contain "BULLISH" or "BEARISH" directional indicator
- SL must be ≥50 pips from entry (otherwise treat as results screenshot)

#### Regex Pre-Filter

Zero-cost text parsing for known signal formats:

**Distinct Forex format:**
```
#XAUUSD BUY 4000
TP 4004
TP 4008
TP 4012
TP 4016
SL 3990
```

**BotX format:**
```
BUY XAUUSD
ENTRY: 4120
SL: 4110
TP: 4137
```

### 4.5 Per-Channel State Machine

**File:** `/data/channel_state.json` (persisted via Docker volume)

**Schema:**
```json
{
  "-1001766321748": {
    "current": {
      "msg_id": 7090,
      "channel_title": "Trade.inn FOREX 💯",
      "action": "SELL",
      "entry": 4130.843,
      "sl": 4140.813,
      "tp": [4120.84, 4114.922],
      "order_type": "limit",
      "from_image": true,
      "timestamp": "2026-07-23T05:42:10+00:00"
    },
    "previous": { ... } | null,
    "channel_title": "Trade.inn FOREX 💯"
  }
}
```

**Key methods:**
- `update(channel_id, signal)` → pushes current → previous, sets new current
- `remove_signal_id(channel_id, msg_id)` → reverts to previous on delete
- `current_signal(channel_id)` → returns current signal dict or None
- `get_channel_title(channel_id)` → returns last known title (survives revert)

**Thread safety:** All mutations protected by `threading.Lock()`. Atomic write via temp file → `os.replace`.

### 4.6 Conditional Close Channel (Fix C — v4.0)

The Router no longer calls `/close_channel` for every signal. Instead:

| Previous Signal | New Signal | Action |
|----------------|------------|--------|
| BUY 4120 | BUY 4120 (same) | Skip close — likely repost |
| BUY 4120 | SELL 4120 (opposite) | Close old trades — reversal |
| BUY 4120 | BUY 4150 (different price) | Close old trades — new signal |
| BUY 4120 | IGNORE | No action |

---

## 5. Component: Bridge

### 5.1 Purpose

The ONLY component that interacts with MT5. Enforces all risk gates, manages trade execution, and handles position cleanup.

### 5.2 API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | MT5 connection + account info |
| `/trade` | POST | Execute a trade (BUY/SELL) |
| `/close_channel` | POST | Close all positions + cancel pending orders for a channel |
| `/manage` | POST | Close/modify a specific position (by signal_id in comment) |
| `/cancel_pending` | POST | Legacy — delegates to `/close_channel` |

### 5.3 Trade Execution Flow

```
POST /trade
  │
  ├─ Atomic duplicate detection (signal_id in executed_signals set)
  ├─ Validate TP required, SL required
  ├─ Check global trade cap (MAX_GLOBAL_TRADES)
  ├─ Check per-channel trade cap (MAX_PER_CHANNEL_TRADES)
  ├─ Check daily loss cap (MAX_DAILY_LOSS_PERCENT)
  ├─ Resolve symbol (GOLD→XAUUSD, etc.)
  ├─ 20-pip rule: determine MARKET vs LIMIT vs STOP
  ├─ SL direction check (SL on correct side of entry)
  ├─ Stale signal check (market moved >0.5%)
  ├─ Calculate lot size (balance-based formula)
  ├─ Execute each TP target:
  │   ├─ order_send.py subprocess (primary)
  │   ├─ └─ on failure → direct mt5.order_send() (fallback)
  │   └─ Log retcode
  └─ Return result with all ticket IDs
```

### 5.4 20-Pip Rule

```
distance = abs(entry - current_market_price)
pip_size = PIP_SIZES[symbol]  # XAUUSD=0.10, US30=1.0, NAS100=1.0

if distance >= 20 * pip_size:
    → PENDING ORDER (LIMIT if entry is behind market, STOP if ahead)
else:
    → MARKET ORDER (immediate execution at current price)
```

**PIP_SIZES map:**
- XAUUSD = 0.10
- US30 = 1.0
- NAS100 = 1.0

### 5.5 Lot Sizing

```python
step = max(int(balance / 100), 0)
lot = 0.01 + step * 0.01
lot_each = round(max(volume_min, lot / len(tps)), 2)
```

| Balance | Lot (1 TP) | Lot (2 TPs) | Lot (4 TPs) |
|---------|-----------|-------------|-------------|
| $500    | 0.06      | 0.03        | 0.02        |
| $2,000  | 0.21      | 0.11        | 0.05        |
| $5,000  | 0.51      | 0.26        | 0.13        |
| $10,000 | 1.01      | 0.51        | 0.25        |

### 5.6 order_send.py Subprocess

**Purpose:** Bypasses rpyc RPC issues by running MT5 order_send in a separate process.

**Flow:**
1. Bridge serializes the order dict to JSON
2. Spawns `python3 /app/order_send.py <order_json> <login> <password> <server>`
3. Subprocess connects to mt5linux, sends order, prints `OK:retcode:order` to stdout
4. Bridge parses stdout → if not `OK:`, falls back to direct `mt5.order_send()`

**v4.0 Fix (Fix A):** File was root-owned with 600 permissions → Docker COPY created an empty file inside the container. Fixed by restoring content from git and fixing permissions.

### 5.7 Safe rpyc Iteration

```python
def iterate_safe(mt5_collection, max_retries=3):
    """Safely iterate rpyc proxy objects, retrying on EOFError."""
```

Used for ALL MT5 collection iteration: `positions_get()`, `orders_get()`. Prevents `EOFError: stream has been closed` crashes.

---

## 6. Component: MT5 (MetaTrader 5)

### 6.1 Purpose

The actual trading platform. Runs under Wine with a virtual display (Xvfb). Provides:
- mt5linux RPC server on port 3000
- VNC access on port 3001 (for visual debugging)

### 6.2 Key Details

- **Image:** `gmag11/metatrader5_vnc:2.3`
- **Network:** Separate bridge network, shared with Bridge via `network_mode: service:mt5`
- **Data:** Persistent volume at `/data/` for MT5 profiles and settings
- **Startup:** `/mt5/start.sh` — handles Wine initialization, MT5 launch, and mt5linux RPC server

---

## 7. Signal Processing Pipeline

### 7.1 Text Signal (e.g., AK Capital VIP)

```
1. Telegram posts: "xauusd sell now 4124-4126 sl 4133"
2. Listener: NewMessage event → builds payload → pushes to Router
3. Router: Routes to TEXT parser → regex no match → DeepSeek LLM
4. LLM returns: {action: "SELL", symbol: "XAUUSD", entry: 4125, sl: 4133, confidence: 0.20}
5. Guard: confidence 0.20 < 0.5 → REJECTED (low confidence signal)
```

### 7.2 Image Signal (e.g., Trade.inn FOREX)

```
1. Telegram posts: chart image + "XAUUSD SETUP‼️\nView bearish ✅"
2. Listener: NewMessage → downloads image (85KB) → builds payload with image_data
3. Router: is_image_channel=True → Gemini Vision LLM
4. Vision returns: {action: "SELL", symbol: "XAUUSD", entry: 4130.843, sl: 4140.813, tp: [4114.922], confidence: 1.0}
5. Guard: SL distance = |4130.843 - 4140.813| / 0.10 = 99.7 pips ≥ 50 → OK
6. Router: Prepends 100-pip safety TP at 4130.843 - 10.0 = 4120.84
7. Router: POST /trade → Bridge executes → 2 pending orders (SELL LIMIT)
8. Router: POST /close_channel with exclude_tickets → cleanup old same-channel orders
9. Router: Updates channel_state, sends Telegram alert
```

### 7.3 Regex Pre-Filter (e.g., BotX)

```
1. Telegram posts: "BUY XAUUSD\nENTRY: 4120\nSL: 4110\nTP: 4137"
2. Listener: pushes to Router
3. Router: regex matches BotX format
4. Direct parse: {action: "BUY", symbol: "XAUUSD", entry: 4120, sl: 4110, tp: [4137], confidence: 1.0}
5. Zero LLM cost — no API call needed
```

### 7.4 Delete Event

```
1. Channel admin deletes a signal message
2. Listener: MessageDeleted event → pushes delete payload to Router
3. Router: calls channel_state.remove_signal_id() → reverts to previous signal
4. Router: POST /close_channel → closes ALL positions for that channel
5. Cleanup: pending orders cancelled, positions closed
```

### 7.5 Edit (Duplicate Repost) — v4.0 Behavior

```
1. Channel posts signal, then auto-edits (same text)
2. Listener: NewMessage fires → pushes msg 17526 (text hash A stored)
3. Listener: MessageEdited fires → debounce check → same msg_id, < 3 seconds → SKIPPED
4. (With old code: both "new" and "edit" would push, causing duplicate trade + self-cancellation)
```

---

## 8. Risk Gates & Guards

### 8.1 Router-Level Guards

| Guard | Condition | Action |
|-------|-----------|--------|
| Action IGNORE | LLM returns action=IGNORE | Return silently |
| Missing SL | BUY/SELL with sl=null | Reject, log warning |
| Low confidence | confidence < 0.5 | Reject, log warning |
| Distinct Forex single-TP | channel=-1001547583661, len(tp)≤1 | Reject as marketing |
| Image SL too tight | \|entry-sl\| < 50 pips | Reject as results screenshot |
| Result post guard | Text contains HIT/✅/❌/😄/💯/PROFIT | Fall through to LLM (not regex) |

### 8.2 Bridge-Level Guards (Deterministic)

| Guard | Condition | Action |
|-------|-----------|--------|
| Missing TP | tp list empty or all zeros | HTTP 400 |
| Missing SL | sl is None or 0 | HTTP 400 |
| Wrong-side SL | BUY: sl ≥ entry, SELL: sl ≤ entry | HTTP 400 |
| Global trade cap | positions_get() ≥ MAX_GLOBAL_TRADES (25) | HTTP 429 |
| Per-channel cap | channel positions ≥ MAX_PER_CHANNEL_TRADES (5) | HTTP 429 |
| Daily loss cap | daily P&L < -(balance × MAX_DAILY_LOSS_PCT%) | HTTP 423 |
| Stale signal | market moved >0.5% since signal entry | HTTP 400 |
| Duplicate signal | signal_id already in executed_signals set | 200 "duplicate_ignored" |

### 8.3 Close Channel Guards (v4.0 — Fix B + C)

| Condition | Action |
|-----------|--------|
| Ticket IDs empty | Skip close_channel entirely (safety net) |
| Bridge returned "duplicate_ignored" | Skip close_channel entirely |
| Same direction + same entry as previous | Skip close (repost) |
| Direction changed from previous | Close old trades (reversal) |
| Same direction, different entry | Close old trades (new signal) |

---

## 9. Trade Lifecycle

### 9.1 Normal Flow

```
Signal arrives
  → Listener pushes to Router
  → Router parses, validates
  → Router POST /trade (race for price)
  → Bridge validates, executes
  → Router extracts tickets
  → Router POST /close_channel (old trades, with exclude_tickets)
  → Router updates channel_state
  → Router sends Telegram alert
```

### 9.2 Supersession Flow

```
Channel posts Signal A (BUY 4120)
  → Trade placed with tickets [111, 222, 333]
  → channel_state: current = Signal A

Channel posts Signal B (SELL 4110) — direction change!
  → Trade placed with tickets [444, 555, 666]
  → Router checks: prev_action=BUY, new_action=SELL → REVERSAL
  → POST /close_channel with exclude_tickets=[444, 555, 666]
  → Bridge: closes Signal A's positions (111, 222, 333), keeps Signal B's orders
  → channel_state: current = Signal B, previous = Signal A
```

### 9.3 Self-Cancellation Prevention (v4.0)

```
Router receives signal → POST /trade
  → Bridge: order_send subprocess fails → fallback succeeds → tickets returned
  → Router: ticket_ids = [111, 222, 333] (not empty)
  → Router: POST /close_channel with exclude_tickets=[111, 222, 333]
  → Bridge: skips excluded tickets, closes only other channel orders
  ✅ Trade survives

Router receives duplicate signal → POST /trade
  → Bridge: signal_id already in executed_signals → "duplicate_ignored"
  → Router: is_duplicate=True → skip close_channel entirely
  ✅ Original trade survives
```

---

## 10. Known Issues & Edge Cases

### 10.1 Telethon Event Handler Goes Stale

**Symptom:** Listener stops receiving events from specific channels after 6-8 hours, while maintaining connectivity.

**Fix:** Restart the listener container:
```bash
docker compose restart listener
```

**Manual reprocess of missed signal:**
```bash
docker compose exec listener python3 -c "
import asyncio, os, json, base64, httpx
from telethon import TelegramClient
async def repush():
    api_id = int(os.environ['TG_API_ID'])
    api_hash = os.environ['TG_API_HASH']
    token = os.environ.get('HERMES_WEBHOOK_TOKEN', '')
    webhook = os.environ.get('HERMES_WEBHOOK_URL', 'http://router:8080/webhook')
    client = TelegramClient('/data/tg_session', api_id, api_hash)
    await client.start()
    entity = await client.get_entity(-1001766321748)  # Trade.inn
    msg = await client.get_messages(entity, ids=MESSAGE_ID)
    image_b64 = None
    if msg.media:
        raw = await msg.download_media(bytes)
        if raw and len(raw)/1024 <= int(os.environ.get('MAX_IMAGE_KB', 512)):
            image_b64 = base64.b64encode(raw).decode('ascii')
    payload = {
        'event': 'new', 'channel_id': -1001766321748,
        'channel_title': 'Trade.inn FOREX 💯', 'message_id': msg.id,
        'reply_to_id': None, 'text': msg.message or '',
        'has_media': msg.media is not None, 'image_data': image_b64,
        'date': msg.date.isoformat(),
    }
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.post(webhook, json=payload, headers={'Authorization': f'Bearer {token}'})
        print(f'Status: {r.status_code}\\nResponse: {r.text[:500]}')
asyncio.run(repush())
"
```

### 10.2 Docker Build: Root-Owned Files

**Symptom:** File exists on host (667 bytes) but inside container it's 0 bytes.

**Cause:** Docker COPY skips files with 600 permissions owned by root.

**Fix:** Ensure all source files are world-readable:
```bash
docker run --rm -v /opt/data/trade-copier:/mnt alpine chmod -R a+r /mnt/bridge/ /mnt/router/
```

### 10.3 Docker-in-Docker: Volume Path Resolution

**Symptom:** `docker compose` inside Hermes container fails with `Error: not a directory`.

**Cause:** Relative paths in `volumes:` resolve from the container's filesystem, not the host.

**Fix:** Use absolute host paths or run `docker compose` from the host directly.

### 10.4 MT5 Comment Field

- Max 31 characters (MT5 limitation)
- No pipe `|` character (causes `order_send` to silently return None)
- Unicode characters normalized to ASCII via NFKC (`sanitize_comment()`)
- Pipe causes `order_send` to return error code -2

### 10.5 rpyc EOFError on MT5 Collections

**Symptom:** `EOFError: stream has been closed` when iterating `positions_get()` or `orders_get()`.

**Fix:** Use `iterate_safe()` wrapper that catches EOFError and retries up to 3 times.

---

## 11. Fixes Applied (v4.0)

### Fix A — order_send.py Subprocess

**Problem:** `order_send.py` was root-owned with 600 permissions on the host (667 bytes). Docker's COPY command couldn't read it, creating an empty 0-byte file inside the container. Every subprocess call returned `None`.

**Impact:** The subprocess never worked for ANY order. The fallback to direct `mt5.order_send()` always recovered, but the subprocess failure added 1-2 seconds of latency per TP and caused the `tickets: []` issue when the bridge's response timing was affected.

**Fix:** Restored file content from git history, fixed permissions to 644.

### Fix B — Guard: Skip close_channel on Empty Tickets

**Problem:** When the bridge returned `tickets: []` (due to subprocess failure) or `status: "duplicate_ignored"`, the router still called `/close_channel` with **no exclude_tickets**, which cancelled ALL orders matching the channel comment — including the one just placed.

**Fix:** Two guards in router.py:
- Skip close_channel entirely if `ticket_ids` is empty
- Skip close_channel entirely if bridge returned `"duplicate_ignored"`

### Fix C — Conditional Close Channel

**Problem:** The router called `/close_channel` for EVERY signal forwarded to the bridge. When a channel posted two identical signals in quick succession (repost), the second signal's close_channel cancelled the first signal's orders.

**Fix:** Before calling close_channel, check `channel_state.current_signal()`:
- **Same direction + same entry** → skip close (repost/duplicate)
- **Direction changed** → close (reversal)
- **Same direction, different entry** → close (new signal)

### Fix D — Listener Debounce

**Problem:** When a channel auto-edited a message (e.g., formatting, adding emoji), both `NewMessage` and `MessageEdited` events fired. The edit detection only checked text hash, but the race condition between "new" and "edit" events sometimes caused both to push before the hash was stored.

**Fix:** Added a 3-second debounce window in the listener. If the same `(chat_id, msg_id)` was processed within the last 3 seconds, subsequent events are skipped regardless of event type.

---

## 12. Debugging Guide

### 12.1 Quick Health Check

```bash
# All containers running?
docker compose ps

# Bridge health (MT5 connection + balance)
docker compose exec bridge curl -s http://localhost:8002/health
```

### 12.2 Log Analysis

```bash
# Full scope: check ALL containers simultaneously
docker compose logs --tail=50 router bridge listener mt5

# Focus on specific issues
docker compose logs router | grep -E "Rejected|WARNING|ERROR"
docker compose logs bridge | grep -E "retcode=|ERROR|WARNING"
docker compose logs listener | grep -E "Pushed|Downloaded|WARNING"
```

### 12.3 Common Issues

| Symptom | Likely Cause | Check |
|---------|-------------|-------|
| Signal not processed | Listener missed event (stale handler) | Restart listener, check logs for "Pushed" |
| Trade placed but immediately cancelled | Empty tickets → close_channel self-cancellation | Check router logs for "tickets: []" |
| order_send subprocess fails | order_send.py empty in container | `docker compose exec bridge cat /app/order_send.py` |
| Bridge returns 422 | TP type mismatch (float vs list) | Check router TP coercion |
| MT5 not connecting | MT5 container restarting | Check `docker compose logs mt5` |
| Duplicate trades | Listener pushing both "new" and "edit" | Check listener debounce, router "duplicate_ignored" |
| No image parsing | IMAGE_CHANNEL_IDS misconfigured | Check `.env` IMAGE_CHANNEL_IDS |

### 12.4 Manual Signal Test

```bash
# Push a test signal directly to the router
docker compose exec listener python3 -c "
import asyncio, os, httpx
async def test():
    token = os.environ.get('HERMES_WEBHOOK_TOKEN', '')
    webhook = os.environ.get('HERMES_WEBHOOK_URL', 'http://router:8080/webhook')
    payload = {
        'event': 'new', 'channel_id': -1002045136062,
        'channel_title': 'BotX', 'message_id': 99999,
        'text': 'BUY XAUUSD\nENTRY: 4120\nSL: 4110\nTP: 4137',
        'has_media': False, 'date': '2026-07-23T00:00:00+00:00'
    }
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(webhook, json=payload, headers={'Authorization': f'Bearer {token}'})
        print(f'Status: {r.status_code}\\n{r.text[:500]}')
asyncio.run(test())
"
```

---

## 13. Configuration Reference

### 13.1 Environment Variables (`.env`)

| Variable | Example | Purpose |
|----------|---------|---------|
| `TG_API_ID` | `123456` | Telegram API ID for userbot |
| `TG_API_HASH` | `abc123...` | Telegram API hash |
| `TG_CHANNELS` | `-1002045136062,-1001766321748,...` | Comma-separated channel IDs to monitor |
| `IMAGE_CHANNEL_IDS` | `-1002045136062,-1001766321748` | Channels that process image signals |
| `HERMES_WEBHOOK_URL` | `http://router:8080/webhook` | Router webhook endpoint |
| `HERMES_WEBHOOK_TOKEN` | `secret123` | Auth token for webhook |
| `BRIDGE_URL` | `http://mt5:8002` | Bridge endpoint (resolves via Docker DNS) |
| `BRIDGE_TOKEN` | `secret456` | Auth token for bridge |
| `OPENROUTER_API_KEY` | `sk-...` | OpenRouter API key for LLM calls |
| `TEXT_MODEL` | `deepseek/deepseek-v4-flash` | LLM model for text signal parsing |
| `VISION_MODEL` | `google/gemini-3.1-flash-lite-preview` | Vision model for chart image parsing |
| `MT5_LOGIN` | `1234567` | MT5 account login |
| `MT5_PASSWORD` | `password` | MT5 account password |
| `MT5_SERVER` | `OctaFX-Demo` | MT5 server name |
| `MAX_IMAGE_KB` | `512` | Max image size for download (KB) |
| `MAX_DAILY_LOSS_PERCENT` | `4.0` | Max daily loss % of balance |
| `MAX_GLOBAL_TRADES` | `25` | Max total open trades |
| `MAX_PER_CHANNEL_TRADES` | `5` | Max open trades per channel |
| `ALERT_BOT_TOKEN` | `7059652154:...` | Telegram bot token for alerts |
| `ALERT_CHAT_ID` | `641927046` | Chat ID for alerts |

### 13.2 Docker Compose Services

| Service | Build Context | Ports | Dependencies |
|---------|--------------|-------|-------------|
| `listener` | `./listener/` | — | router |
| `router` | `./router/` | — | bridge |
| `bridge` | `./bridge/` | — | mt5 |
| `mt5` | `./mt5/` (image) | 3000, 3001 | — |

### 13.3 Docker Volumes

| Volume | Mount | Purpose |
|--------|-------|---------|
| `listener_data` | `/data/` | Telegram session, dead-letter queue |
| `router_data` | `/data/` | Channel state JSON |
| `mt5_data` | `/data/` | MT5 profiles, settings |

---

## Appendix: Channel Reference

| Channel ID | Name | Type | IMAGE_CHANNEL? |
|------------|------|------|----------------|
| `-1002045136062` | BotX | Text | Yes |
| `-1001766321748` | Trade.inn FOREX 💯 | Image | Yes |
| `-1001916021658` | star fx | Text | No |
| `-1002110392082` | XAU/USD ANALYSIS TEAM | Text | No |
| `-1001777167655` | GOLD_GBP_KILLER | Text | No |
| `-1001918089511` | Paul GOLD Dominator | Text | No |
| `-1001519298737` | AK Capital VIP 👑 | Text | No |
| `-1001327204060` | GOLD PIPS HUNTER | Text | No |
| `-1002639766136` | Gold vision | Text | No |
| `-1001547583661` | Distinct Forex *(monitoring only)* | Text | No |

---

## Appendix: Git Repository

- **Remote:** `https://github.com/samwinston1996/trade-copier`
- **Branch:** `master`
- **Workflow:** Commit → Push after every verified fix