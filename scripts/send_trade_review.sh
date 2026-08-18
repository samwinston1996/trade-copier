#!/usr/bin/env bash
set -euo pipefail
# Daily trade review — runs inside Hermes host, queries MT5 via bridge
EMAIL=$(grep EMAIL_ADDRESS /opt/data/.env | cut -d= -f2)
PASS=$(grep EMAIL_APP_PASSWORD /opt/data/.env | cut -d= -f2)
# Copy script and persistent DB into container
docker cp /opt/data/scripts/send_trade_review.py bridge:/tmp/send_trade_review.py || { echo "ERROR: cp script failed"; exit 1; }
docker cp /opt/data/scripts/daily_pnl.db bridge:/tmp/daily_pnl.db 2>/dev/null || true
# Re-own the DB inside the container — docker cp preserves host uid (1000)
# and overlayfs can reject writes to uid-mismatched files even for root
docker exec bridge chown root:root /tmp/daily_pnl.db 2>/dev/null || true
docker exec bridge chmod 644 /tmp/daily_pnl.db 2>/dev/null || true
# Run the script
docker exec -e EMAIL_ADDRESS="$EMAIL" -e EMAIL_APP_PASSWORD="$PASS" bridge python3 /tmp/send_trade_review.py
rc=$?
# Copy updated DB back to host
docker cp bridge:/tmp/daily_pnl.db /opt/data/scripts/daily_pnl.db 2>/dev/null || true
exit $rc