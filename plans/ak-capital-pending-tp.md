# AK Capital VIP Two-Phase TP Mechanism

## Problem
AK Capital sends signals in two messages:
1. Entry + SL (no TP) — e.g. `xauusd sell 4482-4484 | sl 4492`
2. TP follow-up (1-10 min later) — e.g. `tp1 4455 | tp2 4435`

Current system rejects Phase 1 because TP is missing.

## Solution
1. Phase 1: Open trade with given SL + temporary 100-pip TP, mark `pending_tp: true`
2. Phase 2: Modify open position's TP from temp to real TP1, open sub-position for TP2

## Changes
- `bridge/bridge.py`: Add modify_tp op to /manage, pass comment for position matching
- `router/router.py`: Add AK Capital regex handlers + processing logic
- `router/channel_state.py`: Add pending_tp + tickets fields