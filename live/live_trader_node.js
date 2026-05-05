/**
 * Polymarket Live Trader — Node.js (Deposit Wallet / POLY_1271)
 *
 * Runs on VPS in non-blocked jurisdiction.
 * Uses TypeScript CLOB v2 client with signature_type=3 (POLY_1271).
 * Implements BUY_NO at YES>=95% strategy with real money.
 *
 * v2 features:
 *   - Orderbook depth screen (skip thin books)
 *   - Per-category flip rates (from backtest)
 *   - Exit strategy (close positions on price movement)
 *   - Kelly position sizing (vary by category edge)
 *   - Whale watching signal (smart-money NO-buyer confirmation)
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
const MAX_POSITIONS_PER_CATEGORY = 3;
const DATA_DIR = path.join(__dirname, "shadow_data");

// ── #2: Orderbook depth screen ─────────────────────────────
const MIN_NO_DEPTH_SHARES = 50;

// ── #1: Exit strategy ──────────────────────────────────────
const EXIT_PROFIT_PCT = 0.50;
const EXIT_MAX_HOLD_HOURS = 6;
const EXIT_CHECK_ENABLED = true;

// ── #4: Kelly position sizing ──────────────────────────────
const FLIP_RATES_PATH = path.join(__dirname, "..", "backtesting", "category_flip_rates.json");
const KELLY_FRACTION = 0.25;

// ── #5: Whale watching signal ─────────────────────────────
const { WhaleChecker } = require("./whale_checker");

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

// ── Category normalizer ───────────────────────────────────
function normalizeCategory(raw) {
  const c = (raw || "other").toLowerCase().trim();
  if (["weather", "temperature"].includes(c)) return "weather";
  if (["crypto", "cryptocurrency", "bitcoin", "ethereum"].includes(c)) return "crypto";
  if (["sports", "sports-betting", "nba", "nfl", "mlb", "nhl", "soccer", "tennis", "golf", "mma", "boxing"].includes(c)) return "sports";
  if (["politics", "politics-us", "u.s. politics", "politics-world", "government"].includes(c)) return "politics";
  if (["entertainment", "pop culture", "tv", "movies", "music", "awards", "celebrity"].includes(c)) return "entertainment";
  if (["science", "tech", "technology", "ai", "space"].includes(c)) return "tech";
  if (["finance", "economics", "markets"].includes(c)) return "finance";
  return "other";
}

// ── #3: Load per-category flip rates ──────────────────────
let flipRates = null;
function loadFlipRates() {
  if (flipRates) return flipRates;
  try {
    if (fs.existsSync(FLIP_RATES_PATH)) {
      flipRates = JSON.parse(fs.readFileSync(FLIP_RATES_PATH, "utf8"));
      console.log(`Loaded flip rates (generated ${flipRates.generated || "?"})`);
      return flipRates;
    }
  } catch (e) {
    console.log(`Flip rates load failed: ${e.message}`);
  }
  return null;
}

// ── #4: Kelly position sizing ──────────────────────────────
function kellyPositionSize(category, bankroll) {
  const rates = loadFlipRates();
  if (!rates || !rates.categories || !rates.categories[category]) {
    return DEFAULT_POSITION_SIZE;
  }
  const cat = rates.categories[category];
  const p = cat.win_rate;
  const q = 1 - p;
  const noCost = cat.avg_no_cost;
  const b = (1 - noCost) / noCost;
  const fullKelly = (b * p - q) / b;
  if (fullKelly <= 0) return 0;
  const quarterKelly = fullKelly * KELLY_FRACTION;
  const size = Math.max(0.5, Math.min(bankroll * quarterKelly, 10));
  return Math.round(size * 100) / 100;
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
            category: normalizeCategory(m.category),
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

// ── #2: Orderbook depth check ─────────────────────────────
async function checkNoDepth(noTokenId, positionSize) {
  try {
    const resp = await axios.get(`${CLOB_HOST}/book`, {
      params: { token_id: noTokenId },
      timeout: 8000,
    });
    const asks = resp.data.asks || [];
    if (asks.length === 0) {
      return { bestAsk: null, totalDepth: 0, depthOk: false };
    }
    const bestAsk = parseFloat(asks[0].price);
    const totalDepth = asks.reduce((sum, a) => sum + parseFloat(a.size), 0);
    const sharesNeeded = Math.round(positionSize / (1 - bestAsk));
    const depthOk = totalDepth >= Math.max(sharesNeeded, MIN_NO_DEPTH_SHARES);
    return { bestAsk, totalDepth, depthOk, sharesNeeded };
  } catch (e) {
    console.log(`    Depth check failed (${e.message?.slice(0, 50)}), proceeding without screen`);
    return { bestAsk: null, totalDepth: null, depthOk: true };
  }
}

// ── #1: Exit strategy — check open positions for early exit ──
async function checkExits(clobClient, positions) {
  if (!EXIT_CHECK_ENABLED) return positions;

  const openPositions = positions.filter(p => p.status === "open" && p.no_token_id);
  if (openPositions.length === 0) return positions;

  const now = Date.now();
  let exitsChecked = 0;
  let exitsExecuted = 0;

  for (const pos of openPositions) {
    const entryTime = new Date(pos.entry_time).getTime();
    const holdHours = (now - entryTime) / (1000 * 60 * 60);
    if (holdHours < EXIT_MAX_HOLD_HOURS) continue;

    exitsChecked++;
    try {
      const obResp = await axios.get(`${CLOB_HOST}/book`, {
        params: { token_id: pos.no_token_id },
        timeout: 8000,
      });
      const bids = obResp.data.bids || [];
      if (bids.length === 0) continue;

      const currentNoBid = parseFloat(bids[0].price);
      const entryNoCost = pos.no_price_at_entry;
      const profitPct = (currentNoBid - entryNoCost) / entryNoCost;

      if (profitPct >= EXIT_PROFIT_PCT) {
        console.log(`  EXIT: ${pos.question.slice(0, 50)} | entry NO=${entryNoCost.toFixed(3)} → bid=${currentNoBid.toFixed(3)} (${(profitPct * 100).toFixed(0)}% profit)`);

        const shares = Math.round(pos.position_size / entryNoCost);
        const tickSize = await clobClient.getTickSize(pos.no_token_id);
        const negRisk = await clobClient.getNegRisk(pos.no_token_id);

        const order = await clobClient.createAndPostOrder({
          tokenID: pos.no_token_id,
          price: currentNoBid,
          size: shares,
          side: "SELL",
        }, {
          tickSize,
          negRisk,
        });

        console.log(`  EXIT ORDER OK: id=${order.orderID || order.id || "submitted"}`);
        pos.status = "exited";
        pos.exit_time = new Date().toISOString();
        pos.exit_no_price = currentNoBid;
        pos.exit_profit_pct = profitPct;
        pos.exit_order_id = order.orderID || order.id || "";
        exitsExecuted++;
      }
    } catch (e) {
      console.log(`    Exit check error for ${pos.question?.slice(0, 40)}: ${e.message?.slice(0, 60)}`);
    }
  }

  if (exitsChecked > 0) {
    console.log(`Exit scan: ${exitsChecked} checked, ${exitsExecuted} executed`);
  }
  return positions;
}

// ── Main ───────────────────────────────────────────────────
async function main() {
  const args = process.argv.slice(2);
  const dryRun = !args.includes("--live");
  const loop = args.includes("--loop");
  const interval = parseInt(args.find(a => a.startsWith("--interval="))?.split("=")[1] || "300") * 1000;
  const positionSize = parseFloat(args.find(a => a.startsWith("--position-size="))?.split("=")[1] || `${DEFAULT_POSITION_SIZE}`);
  const noExit = args.includes("--no-exit");

  loadFlipRates();

  const whaleChecker = new WhaleChecker();
  const account = privateKeyToAccount(PK.startsWith("0x") ? PK : `0x${PK}`);
  const walletClient = createWalletClient({ account, chain: polygon, transport: http() });
  console.log(`Owner (EOA) address: ${account.address}`);

  const builderCreds = { key: BUILDER_KEY, secret: BUILDER_SECRET, passphrase: BUILDER_PASSPHRASE };
  const builderSigner = new BuilderSigner(builderCreds);
  const builderConfig = new BuilderConfig({ localBuilderCreds: builderCreds });

  const relayClient = new RelayClient(RELAYER_URL, CHAIN_ID, walletClient, builderConfig);
  let depositWallet;
  try {
    depositWallet = await relayClient.deriveDepositWalletAddress();
    console.log(`Deposit wallet: ${depositWallet}`);
  } catch (e) {
    console.error(`Failed to derive deposit wallet: ${e.message || e}`);
    process.exit(1);
  }

  try {
    const geoResult = await new Promise((resolve, reject) => {
      require("child_process").exec(
        'curl -s -m 10 "https://polymarket.com/api/geoblock"',
        (err, stdout) => err ? reject(err) : resolve(stdout)
      );
    });
    const geoData = JSON.parse(geoResult);
    if (geoData.blocked) {
      console.error(`GEOBLOCKED in ${geoData.country}!`);
      process.exit(1);
    }
    console.log(`Geoblock: OK (${geoData.country})`);
  } catch (e) {
    console.log(`Geoblock check skipped (Vercel challenge — VPS is Lithuania)`);
  }

  const clobClient = new ClobClient({
    host: CLOB_HOST,
    chain: CHAIN_ID,
    signer: walletClient,
    signatureType: 3,
    funderAddress: depositWallet,
    builderConfig,
  });

  let apiKey;
  try {
    apiKey = await clobClient.createOrDeriveApiKey();
    console.log(`API key: ${apiKey.key}`);
  } catch (e) {
    console.error(`API key creation failed: ${e.message || e}`);
    process.exit(1);
  }
  clobClient.creds = apiKey;

  let balanceUsd = 0;
  try {
    const balResp = await clobClient.getBalanceAllowance({ asset_type: "COLLATERAL" });
    if (balResp && balResp.balance) {
      balanceUsd = parseFloat(balResp.balance) / 1e6;
    }
    console.log(`CLOB balance: $${balanceUsd.toFixed(2)}`);
  } catch (e) {
    console.log(`Balance check failed, continuing...`);
  }

  if (balanceUsd === 0 && !dryRun) {
    console.error("\nNo CLOB balance! Deposit USDC to deposit wallet first.");
    process.exit(1);
  }

  console.log(`\nLive Trader v2 — ${dryRun ? "DRY RUN" : "*** LIVE TRADING ***"}`);
  console.log(`  Position size: $${positionSize} (default, Kelly may vary)`);
  console.log(`  Strategy: BUY_NO at YES >= ${YES_MIN}`);
  console.log(`  Category cap: ${MAX_POSITIONS_PER_CATEGORY}/category`);
  console.log(`  Depth screen: min ${MIN_NO_DEPTH_SHARES} NO shares`);
  console.log(`  Exit: ${noExit ? "OFF" : `${EXIT_PROFIT_PCT * 100}% profit after ${EXIT_MAX_HOLD_HOURS}h`}`);
  console.log(`  Kelly: quarter (${(KELLY_FRACTION * 100).toFixed(0)}%)`);
  console.log(`  Whale signal: ${whaleChecker.enabled ? `${whaleChecker.smartWallets.size} smart wallets loaded` : "DISABLED"}`);
  console.log(`  Deposit wallet: ${depositWallet}`);

  function todayKey() {
    return new Date().toISOString().slice(0, 10);
  }

  async function scanAndTrade() {
    let positions = loadData("positions.json");

    // Clear whale trade cache at start of each scan cycle
    whaleChecker.clearCache();

    // ── #1: Check exits first ────────────────────────────
    if (!noExit && !dryRun) {
      positions = await checkExits(clobClient, positions);
      saveData("positions.json", positions);
    }

    // ── Entry scan ──────────────────────────────────────
    const markets = await getHighYesMarkets();
    if (markets.length === 0) {
      console.log("No qualifying markets found.");
      return;
    }

    const existingIds = new Set(positions.map(p => p.id));
    const today = todayKey();
    const todayPositions = positions.filter(p => p.entry_time && p.entry_time.startsWith(today)).length;

    const openPositions = positions.filter(p => p.status === "open" || p.status === "dry_run");
    const catCounts = {};
    for (const p of openPositions) {
      const cat = p.category || "other";
      catCounts[cat] = (catCounts[cat] || 0) + 1;
    }
    const catSummary = Object.entries(catCounts).sort((a, b) => b[1] - a[1]).map(([c, n]) => `${c}:${n}`).join(" ");
    console.log(`Category exposure: ${catSummary || "(none)"} | cap: ${MAX_POSITIONS_PER_CATEGORY}/cat`);

    if (todayPositions >= MAX_DAILY_POSITIONS) {
      console.log(`Daily limit reached: ${todayPositions}/${MAX_DAILY_POSITIONS}`);
      return;
    }

    for (const market of markets.slice(0, 15)) {
      if (existingIds.has(market.id)) continue;
      if (todayPositions + positions.filter(p => p.entry_time && p.entry_time.startsWith(today)).length >= MAX_DAILY_POSITIONS) break;

      // ── Category cap ───────────────────────────────────
      const cat = market.category;
      const catOpen = catCounts[cat] || 0;
      if (catOpen >= MAX_POSITIONS_PER_CATEGORY) {
        console.log(`  SKIP [${cat} cap ${catOpen}/${MAX_POSITIONS_PER_CATEGORY}]: ${market.question.slice(0, 55)}`);
        continue;
      }

      // ── #4: Kelly position sizing ──────────────────────
      let catPositionSize = positionSize;
      if (flipRates && flipRates.categories && flipRates.categories[cat]) {
        const kellySize = kellyPositionSize(cat, balanceUsd);
        if (kellySize === 0) {
          console.log(`  SKIP [${cat} negative Kelly edge]: ${market.question.slice(0, 55)}`);
          continue;
        }
        catPositionSize = kellySize;
        if (catPositionSize !== positionSize) {
          console.log(`  Kelly: ${cat} → $${catPositionSize} (WR=${(flipRates.categories[cat].win_rate * 100).toFixed(1)}%)`);
        }
      }

      const noPrice = 1 - market.yes_price;
      const shares = Math.round(catPositionSize / noPrice);

      // ── #2: Orderbook depth screen ─────────────────────
      const depth = await checkNoDepth(market.no_token_id, catPositionSize);
      if (!depth.depthOk) {
        console.log(`  SKIP [thin book ${depth.totalDepth?.toFixed(0) || "?"} vs ${depth.sharesNeeded || MIN_NO_DEPTH_SHARES} needed]: ${market.question.slice(0, 55)}`);
        continue;
      }
      if (depth.bestAsk !== null && depth.bestAsk < noPrice * 0.9) {
        console.log(`    Depth: best NO ask=${depth.bestAsk.toFixed(3)} vs theoretical=${noPrice.toFixed(3)} (${depth.totalDepth.toFixed(0)} shares)`);
      }

      // ── #5: Whale watching signal ──────────────────────
      let whaleSignal = { level: "UNKNOWN", count: 0, wallets: [], boost: 1.0, reason: "skipped" };
      if (whaleChecker.enabled) {
        // Use conditionId (hex) for data-api queries — returns correct No/Yes outcomes
        whaleSignal = await whaleChecker.checkMarket(market.condition_id);
        const emoji = whaleSignal.level === "STRONG" ? "◆" : whaleSignal.level === "MODERATE" ? "◇" : "○";
        console.log(`    Whale: ${emoji} ${whaleSignal.level} (${whaleSignal.count} wallets, ${whaleSignal.reason})`);
      }

      // Adjust position size by whale boost
      let adjustedSize = catPositionSize;
      if (whaleSignal.boost !== 1.0) {
        adjustedSize = Math.round(catPositionSize * whaleSignal.boost * 100) / 100;
        adjustedSize = Math.max(0.5, Math.min(adjustedSize, 10)); // clamp to [0.5, 10]
      }

      if (dryRun) {
        console.log(`  [DRY RUN] BUY NO @ YES=${market.yes_price.toFixed(3)} NO=$${noPrice.toFixed(3)} size=$${adjustedSize} (${shares} shares) ${whaleSignal.level !== "UNKNOWN" ? `whale=${whaleSignal.level}` : ""} | ${market.question.slice(0, 65)}`);
        positions.push({
          id: market.id,
          question: market.question,
          entry_time: new Date().toISOString(),
          yes_price_at_entry: market.yes_price,
          no_price_at_entry: noPrice,
          position_size: adjustedSize,
          no_token_id: market.no_token_id,
          category: cat,
          status: "dry_run",
          whale_signal: whaleSignal.level,
          whale_count: whaleSignal.count,
        });
        catCounts[cat] = (catCounts[cat] || 0) + 1;
        saveData("positions.json", positions);
        continue;
      }

      try {
        const adjustedShares = Math.round(adjustedSize / noPrice);
        console.log(`  BUYING NO @ YES=${market.yes_price.toFixed(3)} NO=$${noPrice.toFixed(3)} size=$${adjustedSize} (${adjustedShares} shares) ${whaleSignal.level !== "UNKNOWN" ? `whale=${whaleSignal.level}` : ""} | ${market.question.slice(0, 65)}`);

        const tickSize = await clobClient.getTickSize(market.no_token_id);
        const negRisk = await clobClient.getNegRisk(market.no_token_id);

        const order = await clobClient.createAndPostOrder({
          tokenID: market.no_token_id,
          price: noPrice,
          size: adjustedShares,
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
          position_size: adjustedSize,
          no_token_id: market.no_token_id,
          category: cat,
          status: "open",
          order_id: order.orderID || order.id || "",
          whale_signal: whaleSignal.level,
          whale_count: whaleSignal.count,
        });
        catCounts[cat] = (catCounts[cat] || 0) + 1;
        saveData("positions.json", positions);

      } catch (e) {
        const errMsg = e.response?.data || e.message || String(e);
        console.error(`  Order failed: ${errMsg}`);
      }
    }
  }

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
