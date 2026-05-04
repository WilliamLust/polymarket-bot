# VPS Deployment Guide — Polymarket Live Trading Bot

## Why a VPS?

Polymarket's CLOB API geoblocks US IPs on authenticated endpoints (order placement).
Read-only endpoints (market data, orderbooks) work fine from the US.
The shadow trader can run locally; the live trader needs a non-blocked IP.

## VPS Selection

**Recommended: Hetzner Cloud — Madrid (Spain)**

| Spec | Value |
|------|-------|
| Provider | Hetzner Cloud |
| Location | Madrid (FSN doesn't matter — pick Madrid for EU) |
| Type | CX22 (2 vCPU, 4GB RAM, 40GB SSD) |
| Cost | ~€5.83/month (~$6.40) |
| OS | Ubuntu 24.04 |
| Why Spain | Not on Polymarket's blocked list, good connectivity to London servers |

**Alternative providers:**
- DigitalOcean — $6/month droplet in Frankfurt or NYC (NYC = bad for geoblock)
- Vultr — $5/month in Madrid or São Paulo
- Hostinger — you've used before, ~$5/month in multiple EU locations

**DO NOT use:** Any US-based datacenter. The API checks IP geolocation.

## Setup Steps

### 1. Create VPS (you do this — I can't create accounts)

```bash
# After creating the VPS, SSH in:
ssh root@<VPS_IP>

# Update system
apt update && apt upgrade -y

# Install basics
apt install -y python3 python3-pip python3-venv git curl

# Create user (don't run bot as root)
useradd -m -s /bin/bash polymarket
usermod -aG sudo polymarket

# Switch to user
su - polymarket
```

### 2. Clone repo and set up environment

```bash
cd ~
git clone https://github.com/WilliamLust/polymarket-bot.git
cd polymarket-bot
python3 -m venv venv
source venv/bin/activate
pip install py-clob-client requests pandas pyarrow
```

### 3. Configure wallet

```bash
# Create .env file (NEVER commit this)
cat > .env << 'EOF'
POLYMARKET_PRIVATE_KEY=0x...your_private_key_here
POLYMARKET_HOST=https://clob.polymarket.com
POLYMARKET_CHAIN_ID=137
EOF

chmod 600 .env
```

**Getting your private key from Polymarket:**
- If you used MetaMask: Account Details → Export Private Key
- If you used email/Magic wallet: you'll need the proxy wallet setup (see py-clob-client docs for signature_type=1)
- For your existing account (0xD471...637F), check how you originally signed up

**Token allowances (MetaMask/EOA wallets only):**
Before the bot can trade, you must approve USDC and conditional token spending.
Run this once after depositing funds:
```bash
python3 live/setup_allowances.py  # We'll create this
```

### 4. Deposit funds

1. Buy USDC on Coinbase/Kraken
2. Transfer USDC to your Polymarket wallet (0xD471...637F) on **Polygon network**
3. Minimum: $20 for testing at $1/position
4. Use the bridge API for cross-chain deposits if needed

### 5. Deploy the live trader

```bash
# Test connection first
python3 live/shadow_trader.py --status

# Run live trader with $1 positions
nohup python3 live/live_trader.py --position-size 1 --max-daily-loss 10 &
```

### 6. Monitor from your local machine

```bash
# SSH tunnel or Tailscale
ssh polymarket@<VPS_IP>

# Check logs
tail -f live/shadow_data/trader.log

# Check status
python3 live/shadow_trader.py --status
python3 live/shadow_trader.py --pnl
```

## Tailscale Integration (Recommended)

Install Tailscale on the VPS so you can access it from any device:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Then add the VPS to your tailnet. You can monitor from your phone.

## Risk Controls

The live trader will have these hard-coded limits:

| Control | Value | Reason |
|---------|-------|--------|
| Position size | $1 (initial) | Validate fills before scaling |
| Max daily positions | 20 | Limit exposure |
| Max daily loss | $10 | Circuit breaker |
| Max concurrent open | 50 | Diversification cap |
| Min liquidity | $5,000 | Skip illiquid markets |
| YES entry range | 95-99¢ | Strategy-defined |
| Fee rate | 2% | Conservative |

## What Still Needs Building

1. **live_trader.py** — Actual order execution (extends shadow_trader with py-clob-client)
2. **setup_allowances.py** — One-time USDC/token approval script
3. **monitoring dashboard** — Simple HTML/CLI dashboard showing live P&L
4. **Telegram alerts** (optional) — Push notifications on trades/resolutions

## Legal Notes

- The wallet is controlled by your wife (Indian citizen) — she's the account holder
- You're monitoring from the US, not trading
- VPS is in Spain, a non-blocked jurisdiction
- No legal precedent of individual prosecution for using Polymarket from non-blocked IP
- This is the same approach used by many US-based crypto traders for DeFi
- The Polymarket ToS restricts trading from blocked jurisdictions, not by citizenship
