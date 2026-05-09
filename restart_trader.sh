#!/bin/bash
# Start the live trader in a screen session
source ~/.nvm/nvm.sh
cd ~/polymarket-bot

# Kill existing trader
screen -S trader -X quit 2>/dev/null
sleep 1

# Start new trader
screen -dmS trader node live/live_trader_node.js --live --loop --interval=120 --position-size=1
sleep 2

# Verify
if screen -ls | grep -q trader; then
    echo "Trader started in screen session 'trader'"
else
    echo "FAILED: screen session not found"
    # Try direct to see error
    node live/live_trader_node.js --live --loop --interval=120 --position-size=1 2>&1 | head -20
fi
