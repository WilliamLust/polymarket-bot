# Polymarket Bot — Restart Guide

## Current Status: ONE BLOCKER FROM LIVE TRADING

The Node.js live trader is deployed on VPS and working in dry-run mode. The only remaining
step before live trading is moving $49.40 USDC from the proxy wallet's CLOB balance to the
deposit wallet's CLOB balance. This requires a manual withdraw + re-deposit via Polymarket UI.

## What Just Happened (This Session)

1. Read RESTART.md, attempted to continue from prior context
2. Installed Node.js v22.22.2 + npm 10.9.7 on VPS via nvm (apt was locked by background process)
3. Wrote live_trader_node.js — Node.js live trader using @polymarket/clob-client-v2 with deposit wallet
4. Fixed SDK API issues: BuilderApiKeyCreds doesn't exist (use plain object), no setApiCreds method
   (assign to clobClient.creds directly), signatureType: 3 = POLY_1271, funderAddress = deposit wallet
5. Deployed to VPS, tested dry-run: deposit wallet derived, API key created, geoblock OK (LT),
   4 qualifying markets found
6. Balance check returns errors because deposit wallet has $0 — expected, not a code bug

## Architecture

```
LOCAL (US server)                    VPS (Lithuania 76.13.251.154)
┌─────────────────────┐              ┌──────────────────────────┐
│ Backtesting          │              │ Node.js Live Trader      │
│ Shadow Trader (paper)│              │ live_trader_node.js      │
│ Data: quant.parquet  │              │ nvm + node v22           │
│   36GB / 568M rows   │              │ npm packages installed   │
│ Data: markets.parquet│              │ .env with keys           │
│   735K markets       │              │ Deposit wallet deployed  │
└─────────────────────┘              └──────────────────────────┘
```

## Key Addresses

| Entity | Address | Notes |
|--------|---------|-------|
| EOA (private key signer) | 0x82d4A17d77E3f948Fe0319d71314fFdee7Afb7b3 | Derived from PK in .env |
| Deposit wallet (ERC-1967) | 0xf277e98adFE6DD4670c2Bb871941DF628A8E0932 | Deployed, approved, $0 balance |
| Proxy/funder wallet | 0xD47142b12ff69fa02f94f9b0f867E1a40027637F | $49.40 in CLOB balance (stuck) |
| USDC.e on Polygon | 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174 | 6 decimals |

## VPS Details

- Host: 76.13.251.154 (Hostinger, Lithuania)
- User: polymarket
- Repo: /home/polymarket/polymarket-bot/
- Node: via nvm (source ~/.nvm/nvm.sh && nvm use 22)
- Python venv: ~/polymarket-bot/venv (has py-clob-client-v2, web3, etc.)
- .env: POLYMARKET_PRIVATE_KEY, BUILDER_API_KEY, BUILDER_SECRET, BUILDER_PASS_PHRASE, etc.
- npm packages: @polymarket/clob-client-v2, @polymarket/builder-relayer-client,
  @polymarket/builder-signing-sdk, viem, axios, dotenv

## The Blocker: Moving $49.40 to Deposit Wallet

The proxy wallet has $49.40 in Polymarket's CLOB smart contracts. The deposit wallet has $0.
CLOB v2 only works with deposit wallets (signature_type=3 / POLY_1271). The proxy wallet
flow (signature_type=1) is broken by `order_version_mismatch`.

**Withdraw + Re-deposit procedure:**
1. Open SOCKS proxy: `ssh -D 1080 -N polymarket@76.13.251.154`
2. Firefox → Settings → Network → SOCKS5 → 127.0.0.1:1080
3. Go to https://polymarket.com/portfolio
4. Click Withdraw → withdraw USDC to an external wallet address
5. Then Deposit → send USDC to deposit wallet: 0xf277e98adFE6DD4670c2Bb871941DF628A8E0932
6. Alternatively: withdraw to external wallet, then send USDC.e on Polygon directly

**Alternative approach (untested):** Use the Polymarket UI to deposit directly to the deposit
wallet address without withdrawing first (if the UI allows specifying a different destination).

## After the Transfer

1. SSH to VPS, run balance check to confirm funds arrived
2. Run live trader with $1 positions:
   ```bash
   ssh polymarket@76.13.251.154
   source ~/.nvm/nvm.sh && nvm use 22
   cd ~/polymarket-bot
   node live/live_trader_node.js --live --loop --interval=300 --position-size=1
   ```
3. Type "yes" if prompted (not currently implemented as confirmation gate)
4. Monitor: positions written to live/shadow_data/positions.json

## SDK Pitfalls Learned This Session

1. **BuilderApiKeyCreds is NOT a class** — it's a plain object `{key, secret, passphrase}`
2. **No setApiCreds method** — assign directly: `clobClient.creds = apiKey`
3. **BuilderConfig takes localBuilderCreds** (plain object, not BuilderSigner instance)
4. **BuilderSigner is separate** — only needed if you're manually constructing builder headers
5. **createOrDeriveApiKey** tries create first (fails with 400 for existing keys), then derives
6. **Balance-allowance endpoint broken in v2** — "Invalid asset type" with COLLATERAL param
7. **nvm is required** — apt was locked by Hostinger background processes, nvm bypasses root
8. **PYTHONUNBUFFERED=1** needed for live Python output in background processes
9. **Python SDK is dead end** — both py-clob-client (order_version_mismatch) and
   py-clob-client-v2 (signer address mismatch) fail for proxy wallets and deposit wallets respectively

## Live Trader Commands (VPS)

```bash
# SSH in
ssh polymarket@76.13.251.154

# Activate Node
source ~/.nvm/nvm.sh && nvm use 22

# Dry run (default)
cd ~/polymarket-bot && node live/live_trader_node.js

# Live trading with loop
node live/live_trader_node.js --live --loop --interval=300 --position-size=1

# Pull latest code
cd ~/polymarket-bot && git pull

# Check Python balance (legacy)
source venv/bin/activate && python live/check_balances.py
```

## Shadow Trader (LOCAL)

Running as background process. Check with:
```bash
# List processes
ps aux | grep shadow_trader

# Check process via Hermes
# proc_e728aef6303f was the last known session ID
```

## Strategy Parameters

- Threshold: BUY_NO at YES >= 95¢ (YES range 0.95-0.99)
- Position size: $1 (conservative start)
- Max daily positions: 20
- Backtest results: 59,353 pos, 9% WR, +$48,980 at $10/pos, MaxDD $2,868, Sharpe 0.295
- Key risk: slippage at 5¢ wipes nearly all edge (+$25 PnL)

## Files

| File | Location | Purpose |
|------|----------|---------|
| live_trader_node.js | ~/polymarket-bot/live/ | Node.js live trader (WORKING in dry-run) |
| live_trader.py | ~/polymarket-bot/live/ | Python live trader (BROKEN, SDK bugs) |
| shadow_trader.py | ~/polymarket-bot/live/ | Paper trader, running on local |
| check_balances.py | ~/polymarket-bot/live/ | Web3.py balance checker |
| .env | VPS: ~/polymarket-bot/.env | Private key, builder keys |
| realistic_slippage_backtest.py | ~/polymarket-bot/backtesting/ | Orderbook fill model |
| strategy_optimizer.py | ~/polymarket-bot/backtesting/ | Threshold optimizer |

## GitHub

Repo: https://github.com/WilliamLust/polymarket-bot
Latest commit: 12d5b90 "Add check_balances.py + shadow trader data"
All changes pushed.

## Remaining Work (Priority Order)

1. **Move $49.40 to deposit wallet** (manual — withdraw via SOCKS proxy + re-deposit)
2. **Test live order placement** — run live_trader_node.js --live with $1 positions
3. **Build systemd service** for Node.js bot (auto-restart, logging)
4. **Monitor fill rates** — compare actual fills to backtest assumptions
5. **Scale position size** — after 50+ successful live positions, increase from $1
6. **Update VPS_DEPLOYMENT.md** — add Node.js/nvm instructions, deposit wallet flow

## Polymarket Account

URL: https://polymarket.com/@0xd47142b12ff69fa02f94f9b0f867e1a40027637f-1757694862978
Email: williamjameslust@gmail.com
