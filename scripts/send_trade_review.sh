#!/usr/bin/env bash
# Daily trade review — runs inside Hermes host, queries MT5 via bridge
EMAIL=$(grep EMAIL_ADDRESS /opt/data/.env | cut -d= -f2)
PASS=$(grep EMAIL_APP_PASSWORD /opt/data/.env | cut -d= -f2)
docker cp /opt/data/scripts/send_trade_review.py bridge:/tmp/send_trade_review.py 2>/dev/null
docker exec -e EMAIL_ADDRESS="$EMAIL" -e EMAIL_APP_PASSWORD="$PASS" bridge python3 /tmp/send_trade_review.py