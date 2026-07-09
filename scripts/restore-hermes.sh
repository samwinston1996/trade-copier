#!/bin/bash
set -euo pipefail
cd ~/trade-copier
echo "Fixing Hermes data permissions..."
docker exec -u root hermes chown -R hermes:hermes /opt/data 2>/dev/null || true
echo "Recreating Hermes stack..."
docker compose up -d hermes
echo "Waiting for Hermes dashboard healthcheck..."
docker compose up -d hermes-dashboard-proxy
sleep 3
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://127.0.0.1:9119/)
echo "Dashboard HTTP: $code"