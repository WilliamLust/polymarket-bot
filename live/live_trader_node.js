/**
 * Polymarket Live Trader — Node.js (Deposit Wallet / POLY_1271)
 *
 * Runs on VPS in non-blocked jurisdiction.
 * Uses TypeScript CLOB v2 client with signature_type=3 (POLY_1271).
 * Implements BUY_NO at YES>=95% strategy with real money.
 */

const { ClobClient } = require("@polymarket/clob-client-v2");
const { RelayClient } = require("@polymarket/builder-relayer-client");
const { BuilderConfig, BuilderSigner } = require("@polymarket/builder-signing-sdk");
const { createWalletClient, http } = require("viem");
const { privateKeyToAccount } = require("viem/accounts");
const { polygon } = require("viem/chains");
const axios = require("axios");
const fs = require("fs");
const path = require("path");

// ── Configuration ──────────────────────────────────────────
const GAMMA_API = "https://gamma-api.polymarket.com";
const CLOB_HOST = "https://clob.polymarket.com";
const RELAYER_URL = "https://relayer-v2.polymarket.com";
const CHAIN_ID = 137;
const YES_MIN = 0.95;
const YES_MAX = 0.99;
const DEFAULT_POSITION_SIZE = 1.0;
const MAX_DAILY_POSITIONS = 20;
const DATA_DIR = path.join(__dirname, "shadow_data");

// ── Load env ───────────────────────────────────────────────
require("dotenv").config({ path: path.join(__dirname, "..", ".env") });

const PK = process.env.POLYMARKET_PRIVATE_KEY;
const BUILDER_KEY = process.env.BUILDER_API_KEY;
const BUILDER_SECRET = process.env.BUILDER_SECRET;
const BUILDER_PASSPHRASE = process.env.BUILDER_PASS_PHRASE;

if (!PK || PK === "PASTE_YOUR_PRIVATE_KEY_HERE") {
  console.error("POLYMARKET_PRIVATE_KEY not set in .env");
  process.exit(1);
}
if (!BUILDER_KEY || !BUILDER_SECRET || !BUILDER_PASSPHRASE) {
  console.error("BUILDER_API_KEY, BUILDER_SECRET, BUILDER_PASS_PHRASE required in .env");
  console.error("Run: node -e \"const{deriveApiKey}=require('@polymarket/builder-signing-sdk'); ...\" to derive them");
  process.exit(1);
}

// ── Data helpers ───────────────────────────────────────────
function loadData(filename) {
  const p = path.join(DATA_DIR, filename);
  if (fs.existsSync(p)) return JSON.parse(fs.readFileSync(p, "utf8"));
  return [];
}

function saveData(filename, data) {
  fs.mkdirSync(DATA_DIR, { recursive: true });
  fs.writeFileSync(path.join(DATA_DIR, filename), JSON.stringify(data, null, 2));
}

// ── Market discovery ───────────────────────────────────────
async function getHighYesMarkets() {
  const markets = [];
  let offset = 0;
  const limit = 100;

  while (true) {
    const resp = await axios.get(`${GAMMA_API}/markets`, {
      params: { closed: "false", active: "true", limit, offset, order: "volume", ascending: "false" },
      timeout: 15000,
    });

    const batch = resp.data;
    if (!batch || batch.length === 0) break;

    for (const m of batch) {
      try {
        let prices = m.outcomePrices;
        if (typeof prices === "string") prices = JSON.parse(prices);
        if (!prices || prices.length < 1) continue;

        const yesPrice = parseFloat(prices[0]);
        const volume = parseFloat(m.volume || 0);
        if (volume < 5000) continue;

        let tokenIds = m.clobTokenIds;
        if (typeof tokenIds === "string") tokenIds = JSON.parse(tokenIds);
        if (!tokenIds || tokenIds.length < 2) continue;

        if (yesPrice >= YES_MIN && yesPrice < YES_MAX) {
          markets.push({
            id: m.id,
            question: m.question,
            slug: m.slug,
            yes_price: yesPrice,
            volume,
            yes_token_id: tokenIds[0],
            no_token_id: tokenIds[1],
            category: m.category || "other",
            condition_id: m.conditionId || "",
          });
        }
      } catch {}
    }

    offset += limit;
    if (batch.length < limit || offset >= 500) break;
  }

  markets.sort((a, b) => b.yes_price - a.yes_price);
  console.log(`Found ${markets.length} markets with ${YES_MIN}<=YES<${YES_MAX}`);
  return markets;
}

// ── Main ───────────────────────────────────────────────────
async function main() {
  const args = process.argv.slice(2);
  const dryRun = !args.includes("--live");
  const loop = args.includes("--loop");
  const interval = parseInt(args.find(a => a.startsWith("--interval="))?.split("=")[1] || "300") * 1000;
  const positionSize = parseFloat(args.find(a => a.startsWith("--position-size="))?.split("=")[1] || `${DEFAULT_POSITION_SIZE}`);

  // 1. Setup viem wallet (EOA signer — the private key owner)
  const account = privateKeyToAccount(PK.startsWith("0x") ? PK : `0x${PK}`);
  const walletClient = createWalletClient({ account, chain: polygon, transport: http() });
  console.log(`Owner (EOA) address: ${account.address}`);

  // 2. Setup builder credentials for L2 auth + builder headers
  const builderCreds = { key: BUILDER_KEY, secret: BUILDER_SECRET, passphrase: BUILDER_PASSPHRASE };
  const builderSigner = new BuilderSigner(builderCreds);
  const builderConfig = new BuilderConfig({ localBuilderCreds: builderCreds });

  // 3. Derive deposit wallet address from EOA
  const relayClient = new RelayClient(RELAYER_URL, CHAIN_ID, walletClient, builderConfig);
  let depositWallet;
  try {
    depositWallet = await relayClient.deriveDepositWalletAddress();
    console.log(`Deposit wallet: ${depositWallet}`);
  } catch (e) {
    console.error(`Failed to derive deposit wallet: ${e.message || e}`);
    console.error("Make sure the deposit wallet was deployed via deploy_deposit_wallet.py first.");
    process.exit(1);
  }

  // 4. Check geoblock
  const geoResp = await axios.get("https://polymarket.com/api/geoblock", { timeout: 10000 });
  if (geoResp.data.blocked) {
    console.error(`GEOBLOCKED in ${geoResp.data.country}!`);
    process.exit(1);
  }
  console.log(`Geoblock: OK (${geoResp.data.country})`);

  // 5. Initialize CLOB client with deposit wallet (POLY_1271)
  //    - signer = walletClient (for L1 auth / order signing)
  //    - signatureType = 3 (POLY_1271 — deposit wallet signs via ERC-1271)
  //    - funderAddress = deposit wallet (the proxy contract)
  //    - builderConfig = builder creds (for builder fee headers)
  const clobClient = new ClobClient({
    host: CLOB_HOST,
    chain: CHAIN_ID,
    signer: walletClient,
    signatureType: 3, // POLY_1271
    funderAddress: depositWallet,
    builderConfig,
  });

  // 6. Create or derive API key (L2 auth)
  let apiKey;
  try {
    apiKey = await clobClient.createOrDeriveApiKey();
    console.log(`API key: ${apiKey.key}`);
  } catch (e) {
    console.error(`API key creation failed: ${e.message || e}`);
    console.error("This usually means the EOA doesn't match the builder API key.");
    console.error(`Expected EOA: ${account.address}`);
    process.exit(1);
  }

  // 7. Set API creds on the client (for L2 auth on subsequent calls)
  clobClient.creds = apiKey;

  // 8. Check balance
  let balanceUsd = 0;
  try {
    await clobClient.updateBalanceAllowance({ assetType: "COLLATERAL" });
    const bal = await clobClient.getBalanceAllowance({ assetType: "COLLATERAL" });
    balanceUsd = parseInt(bal.balance) / 1e6;
    console.log(`CLOB balance: $${balanceUsd.toFixed(2)}`);
  } catch (e) {
    console.error(`Balance check failed: ${e.message || e}`);
    // Non-fatal — continue in dry-run if balance unknown
  }

  if (balanceUsd === 0 && !dryRun) {
    console.error("\nNo CLOB balance! Need to deposit USDC to deposit wallet first.");
    console.error(`Deposit wallet address: ${depositWallet}`);
    console.error("Use Polymarket UI (Deposit button) via SOCKS proxy.");
    console.error("Or transfer USDC.e on Polygon directly to that address.");
    process.exit(1);
  }

  console.log(`\nLive Trader — ${dryRun ? "DRY RUN" : "*** LIVE TRADING ***"}`);
  console.log(`  Position size: $${positionSize}`);
  console.log(`  Strategy: BUY_NO at YES >= ${YES_MIN}`);
  console.log(`  Deposit wallet: ${depositWallet}`);

  // ── Track daily positions ──────────────────────────────────
  function todayKey() {
    return new Date().toISOString().slice(0, 10);
  }

  // ── Scan and trade ─────────────────────────────────────────
  async function scanAndTrade() {
    const markets = await getHighYesMarkets();
    if (markets.length === 0) {
      console.log("No qualifying markets found.");
      return;
    }

    const positions = loadData("positions.json");
    const existingIds = new Set(positions.map(p => p.id));
    const today = todayKey();
    const todayPositions = positions.filter(p => p.entry_time && p.entry_time.startsWith(today)).length;

    if (todayPositions >= MAX_DAILY_POSITIONS) {
      console.log(`Daily limit reached: ${todayPositions}/${MAX_DAILY_POSITIONS}`);
      return;
    }

    for (const market of markets.slice(0, 15)) {
      if (existingIds.has(market.id)) continue;
      if (todayPositions + positions.filter(p => p.entry_time && p.entry_time.startsWith(today)).length >= MAX_DAILY_POSITIONS) break;

      const noPrice = 1 - market.yes_price;
      const shares = Math.round(positionSize / noPrice);

      if (dryRun) {
        console.log(`  [DRY RUN] Would BUY NO @ YES=${market.yes_price.toFixed(3)} NO=$${noPrice.toFixed(3)} size=${shares} shares | ${market.question.slice(0, 65)}`);
        positions.push({
          id: market.id,
          question: market.question,
          entry_time: new Date().toISOString(),
          yes_price_at_entry: market.yes_price,
          no_price_at_entry: noPrice,
          position_size: positionSize,
          no_token_id: market.no_token_id,
          status: "dry_run",
        });
        saveData("positions.json", positions);
        continue;
      }

      // Live order
      try {
        console.log(`  BUYING NO @ YES=${market.yes_price.toFixed(3)} NO=$${noPrice.toFixed(3)} size=${shares} | ${market.question.slice(0, 65)}`);

        // Get tick size and negRisk for this market
        const tickSize = await clobClient.getTickSize(market.no_token_id);
        const negRisk = await clobClient.getNegRisk(market.no_token_id);

        const order = await clobClient.createAndPostOrder({
          tokenID: market.no_token_id,
          price: noPrice,
          size: shares,
          side: "BUY",
        }, {
          tickSize,
          negRisk,
        });

        console.log(`  ORDER OK: id=${order.orderID || order.id || "submitted"}`);

        positions.push({
          id: market.id,
          question: market.question,
          entry_time: new Date().toISOString(),
          yes_price_at_entry: market.yes_price,
          no_price_at_entry: noPrice,
          position_size: positionSize,
          no_token_id: market.no_token_id,
          status: "open",
          order_id: order.orderID || order.id || "",
        });
        saveData("positions.json", positions);

      } catch (e) {
        const errMsg = e.response?.data || e.message || String(e);
        console.error(`  Order failed: ${errMsg}`);
        // Don't add to positions on failure
      }
    }
  }

  // ── Run loop or once ───────────────────────────────────────
  if (loop) {
    let scanCount = 0;
    while (true) {
      scanCount++;
      console.log(`\n${"─".repeat(70)}`);
      console.log(`Scan #${scanCount} — ${new Date().toISOString().slice(11, 19)}`);
      try {
        await scanAndTrade();
      } catch (e) {
        console.error(`Scan error: ${e.message || e}`);
      }
      await new Promise(r => setTimeout(r, interval));
    }
  } else {
    await scanAndTrade();
  }
}

main().catch(e => {
  console.error("Fatal:", e);
  process.exit(1);
});