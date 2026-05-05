/**
 * Whale Checker — Live smart-money signal for Polymarket trades
 *
 * Loads the sig_001 wallet list (statistically significant NO-buyers
 * with WR above baseline) and checks whether any of them have been
 * active NO buyers in a given market's recent trade history.
 *
 * Signal levels:
 *   STRONG  = 3+ smart wallets present → WR ~22% (top 5% tier equivalent)
 *   MODERATE = 1-2 smart wallets present → WR ~19% (sig_001 tier)
 *   NONE   = 0 smart wallets → WR ~12% (baseline absent)
 *
 * Usage:
 *   const { WhaleChecker } = require("./whale_checker");
 *   const wc = new WhaleChecker();  // auto-loads wallet list
 *   const signal = await wc.checkMarket(marketId);
 *   // signal = { level: "MODERATE", count: 2, wallets: [...], boost: 1.3 }
 */

const axios = require("axios");
const fs = require("fs");
const path = require("path");

const CLOB_HOST = "https://clob.polymarket.com";
const WALLET_LIST_PATH = path.join(__dirname, "..", "backtesting", "smart_wallets.json");
const CACHE_TTL_MS = 10 * 60 * 1000; // 10-minute cache for trade lookups

class WhaleChecker {
  constructor() {
    this.smartWallets = new Set();
    this.walletMeta = {};  // wallet → { trades, wins, wr }
    this.tradeCache = new Map();  // marketId → { data, timestamp }
    this._loadWallets();
  }

  _loadWallets() {
    try {
      if (!fs.existsSync(WALLET_LIST_PATH)) {
        console.log("[Whale] No smart_wallets.json found — whale signal disabled");
        return;
      }
      const data = JSON.parse(fs.readFileSync(WALLET_LIST_PATH, "utf8"));
      const wallets = data.wallets || {};
      for (const [addr, meta] of Object.entries(wallets)) {
        // Normalize address to lowercase for comparison
        this.smartWallets.add(addr.toLowerCase());
        this.walletMeta[addr.toLowerCase()] = meta;
      }
      console.log(`[Whale] Loaded ${this.smartWallets.size} smart wallets (tier: ${data.tier}, baseline WR: ${data.baseline_wr})`);
    } catch (e) {
      console.log(`[Whale] Failed to load wallets: ${e.message}`);
    }
  }

  get enabled() {
    return this.smartWallets.size > 0;
  }

  /**
   * Fetch recent trades for a market from the CLOB API.
   * Uses cache to avoid redundant API calls within the same scan cycle.
   */
  async _fetchTrades(marketId) {
    const cached = this.tradeCache.get(marketId);
    if (cached && Date.now() - cached.timestamp < CACHE_TTL_MS) {
      return cached.data;
    }

    try {
      // Fetch last 500 trades — enough to see smart wallet activity
      const resp = await axios.get(`${CLOB_HOST}/trades`, {
        params: { market: marketId, limit: 500 },
        timeout: 8000,
      });
      const trades = resp.data || [];
      this.tradeCache.set(marketId, { data: trades, timestamp: Date.now() });
      return trades;
    } catch (e) {
      // On failure, return empty — don't block trading, just skip whale check
      return [];
    }
  }

  /**
   * Check a market for smart wallet NO-buy activity.
   * Returns { level, count, wallets, boost, reason }
   *
   * boost is a multiplier on position size:
   *   STRONG  → 1.5x (whale conviction is high)
   *   MODERATE → 1.2x (some whale presence)
   *   NONE    → 0.7x (no whale confirmation, reduce exposure)
   *   UNKNOWN → 1.0x (couldn't check, neutral)
   */
  async checkMarket(marketId) {
    if (!this.enabled) {
      return { level: "UNKNOWN", count: 0, wallets: [], boost: 1.0, reason: "disabled" };
    }

    const trades = await this._fetchTrades(marketId);
    if (!trades.length) {
      return { level: "UNKNOWN", count: 0, wallets: [], boost: 1.0, reason: "no_trade_data" };
    }

    // Identify NO-buyers from trades
    // CLOB trade: side="BUY" means taker bought, maker sold
    // For NO-buy detection: we look at trades on the NO token where someone bought
    // Actually, the CLOB trades endpoint shows trades for a market (condition_id),
    // with each trade having side (BUY/SELL) relative to the outcome token traded
    const smartBuyers = new Set();

    for (const trade of trades) {
      // Trade has: maker_address, taker_address, side, outcome
      // side="BUY" → taker bought the outcome, maker sold
      // If outcome="No" and side="BUY" → taker bought NO
      // If outcome="No" and side="SELL" → maker bought NO (taker sold NO)
      const outcome = (trade.outcome || "").toLowerCase();
      if (outcome !== "no") continue;

      const side = (trade.side || "").toUpperCase();
      let buyer = null;

      if (side === "BUY") {
        buyer = (trade.taker_address || "").toLowerCase();
      } else if (side === "SELL") {
        buyer = (trade.maker_address || "").toLowerCase();
      }

      if (buyer && this.smartWallets.has(buyer)) {
        smartBuyers.add(buyer);
      }
    }

    const count = smartBuyers.size;
    const wallets = [...smartBuyers].map(w => ({
      address: w,
      ...this.walletMeta[w],
    }));

    // Sort by WR descending (best whales first)
    wallets.sort((a, b) => (b.wr || 0) - (a.wr || 0));

    let level, boost, reason;
    if (count >= 3) {
      level = "STRONG";
      boost = 1.5;
      reason = `${count} smart wallets buying NO`;
    } else if (count >= 1) {
      level = "MODERATE";
      boost = 1.2;
      reason = `${count} smart wallet(s) buying NO`;
    } else {
      level = "NONE";
      boost = 0.7;
      reason = "no smart wallet activity";
    }

    return { level, count, wallets, boost, reason };
  }

  /**
   * Clear the trade cache (call at start of each scan cycle)
   */
  clearCache() {
    this.tradeCache.clear();
  }
}

module.exports = { WhaleChecker };
