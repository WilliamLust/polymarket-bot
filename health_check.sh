#!/bin/bash
# Trader Health Check — runs via cron every 5 minutes
# Checks: (1) screen session alive, (2) trader log recent

TRADER_LOG="/home/polymarket/polymarket-bot/live/shadow_data/trader.log"
ALERT_LOG="/home/polymarket/polymarket-bot/logs/health_alerts.log"
SCREEN_NAME="trader"

mkdir -p /home/polymarket/polymarket-bot/logs

STATUS="OK"
MESSAGES=""

# Check 1: Screen session exists
if ! screen -list 2>/dev/null | grep -q "$SCREEN_NAME"; then
  STATUS="DOWN"
  MESSAGES="$MESSAGES Screen session '$SCREEN_NAME' NOT FOUND."
fi

# Check 2: Log file exists and was modified within 15 minutes
if [ -f "$TRADER_LOG" ]; then
  LAST_MOD=$(stat -c %Y "$TRADER_LOG" 2>/dev/null)
  NOW=$(date +%s)
  AGE=$(( NOW - LAST_MOD ))
  if [ "$AGE" -gt 900 ]; then
    STATUS="STALE"
    MESSAGES="$MESSAGES Trader log stale ($((AGE/60)) min old)."
  fi
else
  STATUS="DOWN"
  MESSAGES="$MESSAGES Trader log file not found."
fi

# Check 3: Circuit breaker status
CB_FILE="/home/polymarket/polymarket-bot/live/shadow_data/circuit_breaker.json"
if [ -f "$CB_FILE" ]; then
  HALTED=$(python3 -c "import json; print(json.load(open('$CB_FILE')).get('halted', False))" 2>/dev/null)
  if [ "$HALTED" = "True" ]; then
    STATUS="HALTED"
    MESSAGES="$MESSAGES Circuit breaker is HALTED."
  fi
fi

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

if [ "$STATUS" != "OK" ]; then
  echo "[$TIMESTAMP] ALERT [$STATUS]:$MESSAGES" >> "$ALERT_LOG"
  echo "ALERT [$STATUS]:$MESSAGES"
  exit 1
fi

echo "[$TIMESTAMP] OK: trader alive, log recent, circuit breaker clear"
exit 0
