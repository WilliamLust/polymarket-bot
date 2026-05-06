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
 * v3 features:
 *   - Whale watching signal (smart-money NO-buyer confirmation)
 * v4 features:
 *   - KL-divergence weather model (NOAA forecast vs market price)
 *   - Loss analyzer (compound/learning from resolved positions)
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
const MIN_HOURS_TO_CLOSE = 8;
// ── Weather region caps (v5) ──────────────────────────────
const WEATHER_REGIONS = {
  "new york": "northeast", "nyc": "northeast", "boston": "northeast",
  "philadelphia": "northeast", "washington": "northeast", "d.c.": "northeast",
  "atlanta": "southeast", "miami": "southeast", "dallas": "southcentral",
  "houston": "southcentral",
  "chicago": "midwest", "detroit": "midwest", "denver": "midwest",
  "minneapolis": "midwest",
  "los angeles": "southwest", "la ": "southwest", "phoenix": "southwest",
  "seattle": "pacific", "san francisco": "pacific",
  "london": "europe", "paris": "europe", "berlin": "europe",
  "madrid": "europe", "rome": "europe", "amsterdam": "europe",
  "tokyo": "asia", "seoul": "asia", "shanghai": "asia",
  "beijing": "asia", "singapore": "asia",
  "sydney": "oceania", "melbourne": "oceania",
};
const MAX_PER_WEATHER_REGION = 2;

// ── Correlation clusters (v6) ────────────────────────────
const CORRELATION_CLUSTERS = {
  crypto: {
    "btc_eth_price": (q) => /\b(bitcoin|btc|ethereum|eth)\b/i.test(q || ""),
  },
  politics: {
    "uk_election": (q) => /\b(reform uk|labour|tory|conservative|council seat|uk local|united kingdom)\b/i.test(q || ""),
    "us_politics": (q) => /\b(senate|congress|governor|republican|democrat|gop|supreme court)\b/i.test(q || ""),
  },
  sports: {
    "premier_league": (q) => /\b(premier league|epl|nottingham|arsenal|chelsea|liverpool|manchester)\b/i.test(q || ""),
  },
};
const MAX_PER_CORRELATION_CLUSTER = 4;

// ── Secondary strategy (v6) ──────────────────────────────
const YES_SECONDARY_MIN = 0.85;
const YES_SECONDARY_MAX = 0.95;
const SECONDARY_SIZE_FRACTION = 1.0;  // Must be >= 1.0 due to Polymarket $1 minimum order
const SECONDARY_CATEGORY_CAP = 2;





// ── Order retry wrapper (handles order_version_mismatch) ───
async function placeOrderWithRetry(clobClient, orderParams, options, maxRetries = 2) {
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      // Force refresh cached version before first attempt
      if (attempt > 0) {
        await clobClient.resolveVersion(true);
      }
      const order = await clobClient.createAndPostOrder(orderParams, options);
      return order;
    } catch (e) {
      const errMsg = e.response?.data || e.message || String(e);
      const isVersionMismatch = typeof errMsg === 'string' && errMsg.includes('order_version_mismatch');
      if (isVersionMismatch && attempt < maxRetries) {
        console.log(`  Order version mismatch, retrying (${attempt + 1}/${maxRetries})...`);
        // Force version refresh
        clobClient.cachedVersion = undefined;
        await new Promise(r => setTimeout(r, 500));
        continue;
      }
      throw e;
    }
  }
}

function getWeatherRegion(question) {
  const q = question.toLowerCase();
  // Try longer keys first
  const sorted = Object.entries(WEATHER_REGIONS).sort((a, b) => b[0].length - a[0].length);
  for (const [key, region] of sorted) {
    if (q.includes(key)) return region;
  }
  return null;
}
const DATA_DIR = path.join(__dirname, "shadow_data");

// ── #2: Orderbook depth screen ─────────────────────────────
const MIN_NO_DEPTH_SHARES = 50;

// ── #1: Exit strategy ──────────────────────────────────────
const EXIT_PROFIT_PCT = 0.50;
const EXIT_MAX_HOLD_HOURS = 6;
const EXIT_CHECK_ENABLED = false; // v5: disabled - profit-lock is negative EV, hold to resolution

// ── #4: Kelly position sizing ──────────────────────────────
const FLIP_RATES_PATH = path.join(__dirname, "..", "backtesting", "category_flip_rates.json");
const KELLY_FRACTION = 0.25;

// ── Circuit breaker (v6) ──────────────────────────────────
const MAX_DRAWDOWN_PCT = 0.15;
const CIRCUIT_BREAKER_PATH = path.join(DATA_DIR, "circuit_breaker.json");


// ── #5: Whale watching signal ─────────────────────────────
const { WhaleChecker } = require("./whale_checker");

// ── #6: KL-divergence weather model ──────────────────────
const { KLWeather } = require("./kl_weather");

// ── #7: Loss analyzer (compound learning) ─────────────────
const { LossAnalyzer } = require("./loss_analyzer");

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
function normalizeCategory(raw, question, slug) {
  const q = (question || "").toLowerCase();
  const s = (slug || "").toLowerCase();
  const qs = q + " " + s;
  if (/\b(high|highest|low|lowest)\s+temperature\b/.test(qs)) return "weather";
  if (/\b(fahrenheit|celsius)\b/.test(qs)) return "weather";
  if (/\b(weather|forecast|rain|snow|precipitation)\b/.test(qs)) return "weather";
  if (/\b(tornado|hurricane|storm)\b/.test(qs)) return "weather";
  if (/\b(bitcoin|btc|ethereum|eth|solana|sol|xrp|dogecoin|doge|cardano|ada)\b/.test(qs)) return "crypto";
  if (/\b(crypto|cryptocurrency|defi|nft|token|coin)\b/.test(qs)) return "crypto";
  if (/\b(satoshi|whale|holdler|binance|coinbase|kraken)\b/.test(qs)) return "crypto";
  if (/\b(nba|nfl|mlb|nhl|mls|premier league|la liga|serie a|bundesliga)\b/.test(qs)) return "sports";
  if (/\b(win (on|vs|against)|super bowl|world cup|world series|stanley cup|championship)\b/.test(qs)) return "sports";
  if (/\b(score|touchdown|goal scorer|home run|triple crown)\b/.test(qs)) return "sports";
  if (/\b(ufc|mma|boxing|wwe|wrestling|golf|tennis|f1|formula)\b/.test(qs)) return "sports";
  if (/\b(fighter|match|game|bout|round|knockout|playoff)\b/.test(qs)) return "sports";
  if (/\b(elect|election|vote|voter|ballot|campaign|primary|runoff)\b/.test(qs)) return "politics";
  if (/\b(president|senator|congress|governor|mayor|parliament|council seat)\b/.test(qs)) return "politics";
  if (/\b(democrat|republican|conservative|labour|tory|reform uk|liberal|gop)\b/.test(qs)) return "politics";
  if (/\b(bill|legislation|supreme court|impeach|veto|filibuster)\b/.test(qs)) return "politics";
  if (/\b(season|episode|series finale|premiere)\b/.test(qs) && /\b(die|kill|survive|win|appear)\b/.test(qs)) return "entertainment";
  if (/\b(oscar|emmy|grammy|golden globe|academy award)\b/.test(qs)) return "entertainment";
  if (/\b(movie|film|album|song|release|box office|billboard)\b/.test(qs)) return "entertainment";
  if (/\b(tv show|netflix|hbo|disney|amazon prime|streaming)\b/.test(qs)) return "entertainment";
  if (/\b(ai|artificial intelligence|llm|gpt|chatgpt|claude|gemini|openai)\b/.test(qs)) return "tech";
  if (/\b(apple|google|microsoft|meta|amazon|tesla|nvidia|palantir|pltr)\b/.test(qs)) return "tech";
  if (/\b(space|spacex|nasa|rocket|satellite|moon|mars)\b/.test(qs)) return "tech";
  if (/\b(stock|share|dow|s&p|nasdaq|index|etf|dividend)\b/.test(qs)) return "finance";
  if (/\b(gdp|inflation|interest rate|federal reserve|fed|cpi|unemployment)\b/.test(qs)) return "finance";
  if (/\b(oil|natural gas|gold|silver|commodity|crude|brent)\b/.test(qs)) return "finance";
  if (/\b(up or down|price target|market cap|ipo)\b/.test(qs)) return "finance";
  const c = (raw || "").toLowerCase().trim();
  if (!c) return "other";
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
  const size = Math.max(1.0, Math.min(bankroll * quarterKelly, 10));  // v6fix: $1 minimum
  return Math.round(size * 100) / 100;
}

// ── Market discovery ───────────────────────────────────────
async function getHighYesMarkets(yesMin = YES_MIN, yesMax = YES_MAX) {
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
        if (volume < 2000) continue;  // v6: lowered from 5000 — depth screen catches illiquid books

        let tokenIds = m.clobTokenIds;
        if (typeof tokenIds === "string") tokenIds = JSON.parse(tokenIds);
        if (!tokenIds || tokenIds.length < 2) continue;

        if (yesPrice >= yesMin && yesPrice < yesMax) {
          markets.push({
            id: m.id,
            question: m.question,
            slug: m.slug,
            yes_price: yesPrice,
            volume,
            yes_token_id: tokenIds[0],
            no_token_id: tokenIds[1],
            category: normalizeCategory(m.category, m.question, m.slug),
            condition_id: m.conditionId || "",
            end_date: m.endDate || null,
          });
        }
      } catch {}
    }

    offset += limit;
    if (batch.length < limit || offset >= 500) break;
  }


  // ── Event tag fallback for 'other' categories (v6) ──
  if (Date.now() - eventTagCacheTime > EVENT_TAG_TTL_MS) {
    eventTagCache = await buildEventTagMap();
    eventTagCacheTime = Date.now();
  }
  let reclassified = 0;
  for (const m of markets) {
    if (m.category === "other" && m.condition_id && eventTagCache[m.condition_id]) {
      const eventCat = eventTagsToCategory(eventTagCache[m.condition_id]);
      if (eventCat) {
        m.category = eventCat;
        reclassified++;
      }
    }
  }
  if (reclassified > 0) console.log(`[EventTags] Reclassified ${reclassified} 'other' markets via event tags`);

  markets.sort((a, b) => b.yes_price - a.yes_price);
  console.log(`Found ${markets.length} markets with ${yesMin}<=YES<${yesMax}`);
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

        const order = await placeOrderWithRetry(clobClient, {
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



// ── #1b: Resolution checker — detect resolved positions ──────
async function checkResolutions(positions) {
  const openPositions = positions.filter(p => p.status === "open");
  if (openPositions.length === 0) return positions;

  let resolved = 0;
  for (const pos of openPositions) {
    try {
      const resp = await axios.get(`${GAMMA_API}/markets/${pos.id}`, { timeout: 8000 });
      const m = resp.data;
      if (!m.closed) continue;

      // Market is closed — determine outcome
      // For BUY_NO: we win if the market resolves NO (yes_price rounds to 0)
      const yesPrice = parseFloat(m.outcomePrices?.[0] ?? m.yes_price ?? "0.5");
      const noWin = yesPrice < 0.5; // NO side wins
      const entryCost = pos.entry_cost || (pos.no_price_at_entry * pos.position_size);

      if (noWin) {
        // Our NO position won — payout is position_size
        pos.status = "resolved";
        pos.resolution = "WIN";
        pos.pnl = pos.position_size - entryCost;
      } else {
        // YES won — we lose our entry cost
        pos.status = "resolved";
        pos.resolution = "LOSS";
        pos.pnl = -entryCost;
      }
      pos.resolved_time = new Date().toISOString();
      resolved++;
      console.log(`  RESOLVED [${pos.resolution}] ${pos.question?.slice(0, 50)}... PnL=$${pos.pnl?.toFixed(3)}`);
    } catch (e) {
      // Skip on API error — will retry next scan
    }
  }
  if (resolved > 0) {
    console.log(`Resolution scan: ${resolved} positions resolved`);
  }
  return positions;
}



// ── Event-based category fallback (v6) ───────────────────
let eventTagCache = null;
let eventTagCacheTime = 0;
const EVENT_TAG_TTL_MS = 30 * 60 * 1000; // 30-minute cache

async function buildEventTagMap() {
  const tagMap = {};
  try {
    let offset = 0;
    while (offset < 2000) {
      const resp = await axios.get(`${GAMMA_API}/events`, {
        params: { closed: false, active: true, limit: 100, offset },
        timeout: 15000,
      });
      const events = resp.data || [];
      if (events.length === 0) break;
      for (const ev of events) {
        const tags = (ev.tags || []).map(t => (typeof t === 'string' ? t : t.name || t.slug || '').toLowerCase());
        for (const m of (ev.markets || [])) {
          const cid = m.conditionId || "";
          if (cid) tagMap[cid] = tags;
        }
      }
      offset += 100;
      if (events.length < 100) break;
    }
  } catch (e) {
    console.log(`[EventTags] Build failed: ${e.message?.slice(0, 60)}`);
  }
  console.log(`[EventTags] Built tag map: ${Object.keys(tagMap).length} markets`);
  return tagMap;
}

function eventTagsToCategory(tags) {
  if (!tags || tags.length === 0) return null;
  for (const tag of tags) {
    if (["weather", "temperature"].includes(tag)) return "weather";
    if (["crypto", "cryptocurrency"].includes(tag)) return "crypto";
    if (["sports", "nba", "nfl", "mlb", "nhl", "soccer", "mma", "boxing", "tennis", "golf"].includes(tag)) return "sports";
    if (["politics", "us-politics", "world-politics", "government"].includes(tag)) return "politics";
    if (["entertainment", "pop-culture", "tv", "movies", "music", "awards", "celebrity"].includes(tag)) return "entertainment";
    if (["science", "technology", "ai", "space"].includes(tag)) return "tech";
    if (["finance", "economics", "markets"].includes(tag)) return "finance";
  }
  return null;
}
// ── Circuit breaker (v6) ──────────────────────────────────
function loadCircuitBreaker() {
  try {
    if (fs.existsSync(CIRCUIT_BREAKER_PATH)) {
      return JSON.parse(fs.readFileSync(CIRCUIT_BREAKER_PATH, "utf8"));
    }
  } catch (e) {}
  return { peak_bankroll: 0, halted: false, halted_at: null };
}

function saveCircuitBreaker(cb) {
  fs.writeFileSync(CIRCUIT_BREAKER_PATH, JSON.stringify(cb, null, 2));
}

function checkCircuitBreaker(positions, balanceUsd) {
  let cb = loadCircuitBreaker();
  const resolvedPnl = positions
    .filter(p => p.status === "resolved" && p.pnl !== undefined)
    .reduce((sum, p) => sum + p.pnl, 0);
  const openCost = positions
    .filter(p => p.status === "open")
    .reduce((sum, p) => sum + (p.entry_cost || (p.no_price_at_entry || 0) * (p.position_size || 0)), 0);
  const currentBankroll = balanceUsd + openCost + resolvedPnl;

  if (cb.peak_bankroll === 0) {
    cb.peak_bankroll = currentBankroll;
  }
  if (currentBankroll > cb.peak_bankroll) {
    cb.peak_bankroll = currentBankroll;
  }

  const drawdownPct = cb.peak_bankroll > 0 ? (cb.peak_bankroll - currentBankroll) / cb.peak_bankroll : 0;
  cb.current_drawdown_pct = drawdownPct;
  cb.current_bankroll = currentBankroll;

  if (drawdownPct >= MAX_DRAWDOWN_PCT && !cb.halted) {
    cb.halted = true;
    cb.halted_at = new Date().toISOString();
    saveCircuitBreaker(cb);
    return { halted: true, drawdownPct, currentBankroll, peakBankroll: cb.peak_bankroll };
  }

  if (cb.halted && drawdownPct < MAX_DRAWDOWN_PCT * 0.67) {
    cb.halted = false;
    cb.halted_at = null;
    console.log("Circuit breaker: auto-unhalted (drawdown recovered below threshold)");
  }

  saveCircuitBreaker(cb);
  return { halted: cb.halted || false, drawdownPct, currentBankroll, peakBankroll: cb.peak_bankroll };
}
// ── Main ───────────────────────────────────────────────────
async function main() {
  const args = process.argv.slice(2);
  const dryRun = !args.includes("--live");
  const loop = args.includes("--loop");
  const interval = parseInt(args.find(a => a.startsWith("--interval="))?.split("=")[1] || "300") * 1000;
  const positionSize = parseFloat(args.find(a => a.startsWith("--position-size="))?.split("=")[1] || `${DEFAULT_POSITION_SIZE}`);
  const weatherUrgent = args.includes("--weather-urgent");
  const noExit = !EXIT_CHECK_ENABLED;

  loadFlipRates();

  const whaleChecker = new WhaleChecker();
  const klWeather = new KLWeather();
  const lossAnalyzer = new LossAnalyzer();
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

  console.log(`\nLive Trader v6 — ${dryRun ? "DRY RUN" : "*** LIVE TRADING ***"}`);
  console.log(`  Position size: $${positionSize} (default, Kelly may vary)`);
  console.log(`  Strategy: BUY_NO at YES >= ${YES_MIN}`);
  console.log(`  Category cap: ${MAX_POSITIONS_PER_CATEGORY}/category`);
  console.log(`  Weather region cap: ${MAX_PER_WEATHER_REGION}/region (8 regions)`);
  console.log(`  Close filter: skip markets <${MIN_HOURS_TO_CLOSE}h to resolution`);
  console.log(`  Depth screen: min ${MIN_NO_DEPTH_SHARES} NO shares`);
  console.log(`  Exit: ${noExit ? "OFF" : `${EXIT_PROFIT_PCT * 100}% profit after ${EXIT_MAX_HOLD_HOURS}h`}`);
  console.log(`  Kelly: quarter (${(KELLY_FRACTION * 100).toFixed(0)}%)`);
  console.log(`  Circuit breaker: ${(MAX_DRAWDOWN_PCT * 100).toFixed(0)}% drawdown halt`);
  console.log(`  Secondary: ${YES_SECONDARY_MIN}-${YES_SECONDARY_MAX} YES @ ${(SECONDARY_SIZE_FRACTION * 100).toFixed(0)}% size`);
  console.log(`  Volume floor: $2000 (was $5000)`);
  console.log(`  Whale NONE boost: 1.0 (was 0.7)`);
  console.log(`  Loss analyzer: avoid at 10 samples / 90% loss rate (was 3/80%)`);
  console.log(`  Whale signal: ${whaleChecker.enabled ? `${whaleChecker.smartWallets.size} smart wallets loaded` : "DISABLED"}`);
  console.log(`  KL-Weather: ${klWeather.enabled ? "active (NWS forecast → KL-div)" : "DISABLED"}`);
  console.log(`  Loss analyzer: ${lossAnalyzer.stats.totalAnalyzed} positions analyzed, ${lossAnalyzer.stats.avoidCategories.length} avoided categories`);
  console.log(`  Deposit wallet: ${depositWallet}`);

  function todayKey() {
    return new Date().toISOString().slice(0, 10);
  }

  async function scanAndTrade() {
    let positions = loadData("positions.json");

    // ── Circuit breaker check (v6) ────────────────────
    const cb = checkCircuitBreaker(positions, balanceUsd);
    if (cb.halted) {
      console.log(`CIRCUIT BREAKER: Trading halted! Drawdown ${(cb.drawdownPct * 100).toFixed(1)}% >= ${(MAX_DRAWDOWN_PCT * 100).toFixed(0)}% | Bankroll $${cb.currentBankroll.toFixed(2)} | Peak $${cb.peakBankroll.toFixed(2)}`);
      return;
    }
    if (cb.drawdownPct > 0.01) {
      console.log(`Drawdown: ${(cb.drawdownPct * 100).toFixed(1)}% | Bankroll: $${cb.currentBankroll.toFixed(2)} | Peak: $${cb.peakBankroll.toFixed(2)}`);
    }


    // ── Refresh balance each scan cycle (v6) ─────────
    try {
      const balResp = await clobClient.getBalanceAllowance({ asset_type: "COLLATERAL" });
      if (balResp && balResp.balance) {
        const newBal = parseFloat(balResp.balance) / 1e6;
        if (newBal !== balanceUsd) {
          console.log(`Balance refresh: $${balanceUsd.toFixed(2)} -> $${newBal.toFixed(2)}`);
          balanceUsd = newBal;
        }
      }
    } catch (e) {
      console.log(`Balance refresh failed, using $${balanceUsd.toFixed(2)}`);
    }


    // Clear whale trade cache at start of each scan cycle
    whaleChecker.clearCache();
    klWeather.clearCache();

    // ── #7: Analyze resolved positions for learning ─────
    lossAnalyzer.analyzeResolved(positions);

    // ── #1b: Check if any open positions have resolved ──
    positions = await checkResolutions(positions);
    saveData("positions.json", positions);

    // ── #1: Check exits first ────────────────────────────
    if (!noExit && !dryRun) {
      positions = await checkExits(clobClient, positions);
      saveData("positions.json", positions);
    }

    // ── Entry scan ──────────────────────────────────────
    let markets = await getHighYesMarkets();
  
  if (weatherUrgent) {
      const before = markets.length;
      markets = markets.filter(m => m.category === "weather");
      console.log("WEATHER URGENT SCAN -- filtered " + before + " markets to " + markets.length + " weather markets");
    }
    if (markets.length === 0) {
      console.log(weatherUrgent ? "WEATHER URGENT SCAN -- No qualifying weather markets found." : "No qualifying markets found.");
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


      // ── Weather region cap (v5) ──────────────────────────
      if (cat === "weather") {
        const region = getWeatherRegion(market.question);
        if (region) {
          const regionOpen = positions.filter(p =>
            p.status === "open" && p.category === "weather" && getWeatherRegion(p.question) === region
          ).length;
          if (regionOpen >= MAX_PER_WEATHER_REGION) {
            console.log(`  SKIP [${region} region cap ${regionOpen}/${MAX_PER_WEATHER_REGION}]: ${market.question.slice(0, 55)}`);
            continue;
          }
        }
      }

      // ── Correlation cluster cap (v6) ───────────────────
      let clusterSkip = false;
      if (CORRELATION_CLUSTERS[cat]) {
        for (const [clusterName, testFn] of Object.entries(CORRELATION_CLUSTERS[cat])) {
          if (testFn(market.question)) {
            const clusterOpen = positions.filter(p =>
              p.status === "open" && p.category === cat && testFn(p.question)
            ).length;
            if (clusterOpen >= MAX_PER_CORRELATION_CLUSTER) {
              console.log(`  SKIP [${clusterName} cluster cap ${clusterOpen}/${MAX_PER_CORRELATION_CLUSTER}]: ${market.question.slice(0, 55)}`);
              clusterSkip = true;
              break;
            }
          }
        }
      }
      if (clusterSkip) continue;


      // ── Time-to-close filter (v5) ──────────────────────
      if (market.end_date) {
        const hoursToClose = (new Date(market.end_date).getTime() - Date.now()) / (1000 * 3600);
        if (hoursToClose < MIN_HOURS_TO_CLOSE) {
          console.log(`  SKIP [closing <${MIN_HOURS_TO_CLOSE}h]: ${market.question.slice(0, 55)}`);
          continue;
        }
      }

      // ── #7: Loss analyzer entry signal ─────────────────
      const lossSignal = lossAnalyzer.entrySignal(market);
      if (!lossSignal.pass) {
        console.log(`  SKIP [loss analyzer: ${lossSignal.reasons[0]}]: ${market.question.slice(0, 55)}`);
        continue;
      }
      if (lossSignal.boost !== 1.0 || lossSignal.reasons.length > 0) {
        console.log(`    Loss: boost=${lossSignal.boost} ${lossSignal.reasons.join("; ")}`);
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

      // ── #6: KL-divergence weather model ────────────────
      let klSignal = { level: "SKIP", dkl: 0, boost: 1.0, reason: "not weather" };
      if (klWeather.enabled && cat === "weather") {
        klSignal = await klWeather.evaluate(market);
        if (klSignal.level !== "SKIP") {
          const sym = klSignal.level === "STRONG" ? "◆" : klSignal.level === "MODERATE" ? "◇" : klSignal.level === "CAUTION" ? "⚠" : "○";
          console.log(`    KL-Weather: ${sym} ${klSignal.level} D_KL=${klSignal.dkl} bits (${klSignal.reason.slice(0, 60)})`);
        }
      }

      // Adjust position size by whale boost, KL-weather boost, and loss analyzer boost
      let adjustedSize = catPositionSize;
      // Composite: multiply all boost factors together
      const compositeBoost = whaleSignal.boost * klSignal.boost * lossSignal.boost;
      if (compositeBoost !== 1.0) {
        adjustedSize = Math.round(catPositionSize * compositeBoost * 100) / 100;
        adjustedSize = Math.max(1.0, Math.min(adjustedSize, 10));  // v6fix: $1 minimum for Polymarket // clamp to [0.5, 10]
        console.log(`    Size boost: whale=${whaleSignal.boost} kl=${klSignal.boost} loss=${lossSignal.boost} → composite=${compositeBoost.toFixed(2)} → $${adjustedSize}`);
      }

      if (dryRun) {
        console.log(`  [DRY RUN] BUY NO @ YES=${market.yes_price.toFixed(3)} NO=$${noPrice.toFixed(3)} size=$${adjustedSize} (${shares} shares) ${whaleSignal.level !== "UNKNOWN" ? `whale=${whaleSignal.level}` : ""} ${klSignal.level !== "SKIP" ? `kl=${klSignal.level}` : ""} | ${market.question.slice(0, 65)}`);
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
          kl_signal: klSignal.level,
          kl_dkl: klSignal.dkl,
          composite_boost: compositeBoost,
        });
        catCounts[cat] = (catCounts[cat] || 0) + 1;
        saveData("positions.json", positions);
        continue;
      }

      try {
        const adjustedShares = Math.round(adjustedSize / noPrice);
        console.log(`  BUYING NO @ YES=${market.yes_price.toFixed(3)} NO=$${noPrice.toFixed(3)} size=$${adjustedSize} (${adjustedShares} shares) ${whaleSignal.level !== "UNKNOWN" ? `whale=${whaleSignal.level}` : ""} ${klSignal.level !== "SKIP" ? `kl=${klSignal.level}` : ""} | ${market.question.slice(0, 65)}`);

        const tickSize = await clobClient.getTickSize(market.no_token_id);
        const negRisk = await clobClient.getNegRisk(market.no_token_id);

        const order = await placeOrderWithRetry(clobClient, {
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
          kl_signal: klSignal.level,
          kl_dkl: klSignal.dkl,
          composite_boost: compositeBoost,
        });
        catCounts[cat] = (catCounts[cat] || 0) + 1;
        saveData("positions.json", positions);

      } catch (e) {
        const errMsg = e.response?.data || e.message || String(e);
        console.error(`  Order failed: ${errMsg}`);
      }
    }
    // ── Secondary strategy scan (v6): 85-94 cent YES bucket ──
    const secondaryMarkets = await getHighYesMarkets(YES_SECONDARY_MIN, YES_SECONDARY_MAX);

    for (const market of secondaryMarkets.slice(0, 10)) {
      if (existingIds.has(market.id)) continue;

      const cat = market.category;
      const catOpen = positions.filter(p =>
        (p.status === "open" || p.status === "dry_run") && p.category === cat
      ).length;
      if (catOpen >= SECONDARY_CATEGORY_CAP) continue;
      if (catCounts[cat] >= MAX_POSITIONS_PER_CATEGORY) continue;

      if (market.end_date) {
        const hoursToClose = (new Date(market.end_date).getTime() - Date.now()) / (1000 * 3600);
        if (hoursToClose < MIN_HOURS_TO_CLOSE) continue;
      }

      const secSize = Math.max(1.0, positionSize * SECONDARY_SIZE_FRACTION);  // v6fix: $1 minimum
      const depth = await checkNoDepth(market.no_token_id, secSize);
      if (!depth.depthOk) continue;

      const lossSignal = lossAnalyzer.entrySignal(market);
      if (!lossSignal.pass) continue;

      const noPrice = 1 - market.yes_price;
      const shares = Math.round(secSize / noPrice);

      if (dryRun) {
        console.log(`  [SECONDARY DRY] BUY NO @ YES=${market.yes_price.toFixed(3)} NO=$${noPrice.toFixed(3)} size=$${secSize.toFixed(2)} | ${market.question.slice(0, 55)}`);
        continue;
      }

      try {
        console.log(`  [SECONDARY] BUY NO @ YES=${market.yes_price.toFixed(3)} NO=$${noPrice.toFixed(3)} size=$${secSize.toFixed(2)} | ${market.question.slice(0, 55)}`);
        const tickSize = await clobClient.getTickSize(market.no_token_id);
        const negRisk = await clobClient.getNegRisk(market.no_token_id);
        const order = await placeOrderWithRetry(clobClient, {
          tokenID: market.no_token_id,
          price: noPrice,
          size: shares,
          side: "BUY",
        }, { tickSize, negRisk });

        console.log(`  SECONDARY ORDER OK: id=${order.orderID || order.id || "submitted"}`);
        positions.push({
          id: market.id,
          question: market.question,
          entry_time: new Date().toISOString(),
          yes_price_at_entry: market.yes_price,
          no_price_at_entry: noPrice,
          position_size: secSize,
          no_token_id: market.no_token_id,
          category: cat,
          status: "open",
          strategy: "secondary_85_94",
          order_id: order.orderID || order.id || "",
        });
        catCounts[cat] = (catCounts[cat] || 0) + 1;
        saveData("positions.json", positions);
      } catch (e) {
        console.error(`  Secondary order failed: ${(e.response?.data || e.message || String(e)).slice(0, 80)}`);
      }
    }
  }


  if (weatherUrgent) {
    console.log("\n" + "\u2500".repeat(70));
    console.log("WEATHER URGENT SCAN -- " + new Date().toISOString().slice(11, 19) + " (single pass)");
    await scanAndTrade();
  } else if (loop) {
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
