# Polymarket Bot — Restart Guide

## Current Status: LIVE TRADING ACTIVE

$49.39 pUSD is in the deposit wallet's CLOB balance. Exchange approvals are set. The live
trader is running on VPS in loop mode (PID 72393, scans every 5 min).

### What Was Completed (This Session)

1. Wrote `live/bridge_transfer.js` — submits relayer batch to transfer USDC from deposit wallet to bridge
2. Transferred $49.39 native USDC → bridge EVM address (TX: `0xb55ba...5f4e`, block 86403939, SUCCESS)
3. Bridge auto-converted native USDC → pUSD (confirmed on-chain: 49.39 pUSD at deposit wallet)
4. Set pUSD approvals for CTF Exchange V2 and NegRisk Exchange V2 (TX: `0xc2e76...4a99`)
5. Set pUSD approval for NegRisk Adapter `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` (TX: `0xf37b9...f249`)
6. Fixed live_trader_node.js balance check — replaced broken `/balances` endpoint with `getBalanceAllowance()`
7. Verified CLOB balance: $49.39 with all approvals set
8. Started live trader: `node live_trader_node.js --live --loop --interval=300 --position-size=1`

### Current Market Conditions

- No markets currently in the 0.95-0.99 YES price window (thin conditions)
- Bot is scanning every 5 minutes and will auto-execute when qualifying markets appear
- Only 2 markets above 0.85 YES with volume >5K as of the last check

## Key Discovery: Native USDC vs USDC.e

| Token | Address | Paused on Onramp | Can Wrap? |
|-------|---------|-------------------|-----------|
| USDC (native) | 0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359 | YES | NO |
| USDC.e (bridged) | 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174 | NO | YES |
| pUSD (collateral) | 0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB | N/A | N/A |

The deposit wallet holds native USDC. We need to either:
- **Option A (RECOMMENDED)**: Use Bridge API to send native USDC → auto-converts to pUSD
- **Option B**: Swap native USDC → USDC.e via DEX, then wrap via CollateralOnramp

## Bridge API Approach (Step by Step)

### How the Bridge Works

The Polymarket Bridge API accepts deposits from many chains/assets and auto-converts
everything to pUSD on Polygon. For Polygon-based USDC, the flow is:

1. Get your deposit address via `POST /deposit`
2. Transfer USDC to that deposit address (same-chain, no gas for cross-chain)
3. Bridge detects the transfer, swaps USDC → pUSD, credits to your wallet
4. pUSD appears in your CLOB balance after sync

### Step 1: Get Bridge Deposit Address

```bash
curl -X POST https://bridge.polymarket.com/deposit \
  -H "Content-Type: application/json" \
  -d '{"address": "0xf277e98adFE6DD4670c2Bb871941DF628A8E0932"}'
```

Returns:
```json
{
  "address": {
    "evm": "0x21f6F035C913C9fE0525BF44B3555453867DCe2B",
    "svm": "HhTKUanXXwfnrrGLrgJowKh6HeS2R9YU2hDc1Eers5sV",
    "btc": "bc1qs7n8cuju0tath9ealas3wtdzk78pws34evutje",
    "tron": "TU7WZ4ojjCii4DWpsmc8h9o6XHgbmrYQUh"
  }
}
```

The `evm` address is where we send Polygon USDC.

### Step 2: Transfer USDC to Bridge Deposit Address

This is the tricky part — the deposit wallet is a smart contract, not an EOA. We need
to submit a relayer batch to call `USDC.transfer(bridgeEvmAddress, amount)`.

```javascript
// On VPS, run a relayer batch script
const calls = [{
  target: USDC_NATIVE,  // 0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359
  value: "0",
  data: encodeFunctionData({
    abi: [{ name: "transfer", type: "function",
      inputs: [{ name: "to", type: "address" }, { name: "amount", type: "uint256" }],
      outputs: [{ type: "bool" }] }],
    args: ["0x21f6F035C913C9fE0525BF44B3555453867DCe2B", 49400000n]
  })
}];
const deadline = Math.floor(Date.now() / 1000 + 3600).toString();
await relayClient.executeDepositWalletBatch(calls, DEPOSIT_WALLET, deadline);
```

### Step 3: Wait for Bridge Processing

- Monitor at: `GET https://bridge.polymarket.com/status/0xf277e98adFE6DD4670c2Bb871941DF628A8E0932`
- Polygon deposits typically process in minutes
- Minimum deposit: $2 for Polygon assets

### Step 4: Sync CLOB Balance

After pUSD arrives at deposit wallet:
```javascript
const clob = new ClobClient({
  host: "https://clob.polymarket.com",
  chain: 137,
  signer: walletClient,
  creds: apiKey,
  signatureType: SignatureTypeV2.POLY_1271,
  funderAddress: "0xf277e98adFE6DD4670c2Bb871941DF628A8E0932",
});
await clob.updateBalanceAllowance({ asset_type: AssetType.COLLATERAL });
```

### Step 5: Go Live

```bash
node live/live_trader_node.js --live --loop --interval=300 --position-size=1
```

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
| Deposit wallet (ERC-1967) | 0xf277e98adFE6DD4670c2Bb871941DF628A8E0932 | Deployed, holds $49.40 native USDC |
| Bridge EVM deposit addr | 0x21f6F035C913C9fE0525BF44B3555453867DCe2B | Send USDC here for auto pUSD conversion |
| Proxy/funder wallet | 0xD47142b12ff69fa02f94f9b0f867E1a40027637F | $0 (already withdrawn) |
| USDC native on Polygon | 0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359 | PAUSED on onramp |
| USDC.e on Polygon | 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174 | Can be wrapped |
| pUSD (collateral) | 0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB | CLOB trading token |
| CollateralOnramp | 0x93070a847efEf7F70739046A929D47a521F5B8ee | Wraps USDC.e → pUSD |
| CTF Exchange V2 | 0xE111180000d2663C0091e4f400237545B87B996B | Conditional tokens |
| DepositWalletFactory | 0x00000000000Fb5C9ADea0298D729A0CB3823Cc07 | Creates deposit wallets |
| DepositWalletImpl | 0x58CA52ebe0DadfdF531Cde7062e76746de4Db1eB | Implementation contract |

## VPS Details

- Host: 76.13.251.154 (Hostinger, Lithuania)
- User: polymarket
- Repo: /home/polymarket/polymarket-bot/
- Node: via nvm (source ~/.nvm/nvm.sh && nvm use 22)
- Python venv: ~/polymarket-bot/venv (has py-clob-client-v2, web3, etc.)
- .env: POLYMARKET_PRIVATE_KEY, BUILDER_API_KEY, BUILDER_SECRET, BUILDER_PASS_PHRASE, etc.
- npm packages: @polymarket/clob-client-v2, @polymarket/builder-relayer-client,
  @polymarket/builder-signing-sdk, viem, axios, dotenv
- SOCKS proxy: `ssh -D 1080 -N polymarket@76.13.251.154`

## SDK Pitfalls (All Sessions)

1. **Native USDC is PAUSED on CollateralOnramp** — must use USDC.e or Bridge API
2. **BuilderApiKeyCreds is NOT a class** — plain object `{key, secret, passphrase}`
3. **No setApiCreds method** — assign directly: `clobClient.creds = apiKey`
4. **BuilderConfig takes localBuilderCreds** (plain object)
5. **createOrDeriveApiKey** tries create first (fails with 400 for existing keys), then derives
6. **Balance-allowance endpoint broken in v2** — "Invalid asset type" with COLLATERAL param
7. **nvm is required** — apt locked by Hostinger background processes
8. **Python SDK is dead end** — py-clob-client (order_version_mismatch) and
   py-clob-client-v2 (signer address mismatch) both broken
9. **Relayer deadline must be string with 1hr+ window** — 4min causes "deadline too soon"
10. **Relayer checks deployed status with wrong type by default** — use type=WALLET
11. **Relayer simulates batches** — if any call reverts, whole batch rejected with "batch would revert"
12. **ClobClient v2 constructor** uses object param: `{ host, chain, signer, creds, signatureType, funderAddress }`

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
| package.json | ~/polymarket-bot/ | Node.js dependencies |

## GitHub

Repo: https://github.com/WilliamLust/polymarket-bot

## Remaining Work (Priority Order)

1. **Monitor first live trades** — check positions.json after qualifying markets appear
2. **Compare fill rates to backtest** — actual slippage vs modeled slippage
3. **Build systemd service** for Node.js bot (auto-restart, logging)
4. **Scale position size** — after 50+ successful live positions, increase from $1

## Polymarket Account

URL: https://polymarket.com/@0xd47142b12ff69fa02f94f9b0f867e1a40027637f-1757694862978
Email: williamjameslust@gmail.com
