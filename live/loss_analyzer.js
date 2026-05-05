/**
 * Loss Analyzer — Compound/learning from resolved positions
 *
 * Categorizes each resolved loss into failure modes, tracks patterns
 * over time, and produces adjustment signals for live entry decisions.
 *
 * Failure modes:
 *   WRONG_CATEGORY   — Category has negative Kelly edge (skip entirely)
 *   THIN_BOOK        — Orderbook depth was insufficient at entry
 *   HELD_TOO_LONG    — Position had profitable exit window but wasn't sold
 *   NO_WHALE         — No smart-money confirmation (whale signal was NONE)
 *   HIGH_YES         — Entered at YES >= 0.98 (too close to 1.0, tiny edge)
 *   NO_MODEL_EDGE    — Weather market where KL-divergence was weak/none
 *   BAD_EXIT         — Exit executed but at a loss (market moved against)
 *
 * Outputs:
 *   - Per-category loss rate → feeds Kelly sizing
 *   - Per-failure-mode count → feeds entry filters
 *   - "avoid" list: categories/conditions with persistent negative EV
 *   - Loss rate trend: improving, stable, or degrading
 *
 * Usage:
 *   const { LossAnalyzer } = require("./loss_analyzer");
 *   const la = new LossAnalyzer();
 *   la.analyzeResolved(positions);  // categorize all resolved positions
 *   const signal = la.entrySignal(market);  // check before entering
 */

const fs = require("fs");
const path = require("path");

const DATA_DIR = path.join(__dirname, "shadow_data");
const LOSS_HISTORY_PATH = path.join(DATA_DIR, "loss_history.json");

// ── Failure mode definitions ────────────────────────────────────
const FAILURE_MODES = {
  WRONG_CATEGORY: {
    label: "Wrong category",
    description: "Category has negative or very low Kelly edge",
    filter: "Skip category entirely",
  },
  THIN_BOOK: {
    label: "Thin book",
    description: "Depth was below minimum at entry time",
    filter: "Increase depth threshold",
  },
  HELD_TOO_LONG: {
    label: "Held too long",
    description: "Position was profitable at some point but not exited",
    filter: "Tighten exit window",
  },
  NO_WHALE: {
    label: "No whale",
    description: "No smart-money confirmation at entry",
    filter: "Require whale signal for category",
  },
  HIGH_YES: {
    label: "High YES entry",
    description: "Entered at YES >= 0.98 (almost no edge room)",
    filter: "Lower YES_MAX threshold",
  },
  NO_MODEL_EDGE: {
    label: "No model edge",
    description: "Weather market where KL-divergence was weak",
    filter: "Require KL-Weather STRONG/MODERATE",
  },
  BAD_EXIT: {
    label: "Bad exit",
    description: "Exit executed at a loss (market moved against after entry)",
    filter: "Review exit timing",
  },
  UNKNOWN: {
    label: "Unknown",
    description: "Loss without clear failure mode",
    filter: "Monitor for patterns",
  },
};

class LossAnalyzer {
  constructor() {
    this.history = [];
    this.stats = {
      byCategory: {},      // { weather: { wins: N, losses: N, lossRate: N } }
      byFailure: {},        // { THIN_BOOK: N, NO_WHALE: N, ... }
      avoidCategories: [],  // Categories with loss rate > 80%
      lossRateTrend: "stable", // improving, stable, degrading
      totalAnalyzed: 0,
    };
    this._load();
  }

  // ── Load historical loss data ─────────────────────────────────
  _load() {
    try {
      if (fs.existsSync(LOSS_HISTORY_PATH)) {
        const data = JSON.parse(fs.readFileSync(LOSS_HISTORY_PATH, "utf8"));
        this.history = data.history || [];
        this.stats = data.stats || this.stats;
        console.log(`[Loss] Loaded ${this.history.length} analyzed positions, ${this.stats.avoidCategories?.length || 0} avoided categories`);
      }
    } catch (e) {
      console.log(`[Loss] Load failed: ${e.message}`);
    }
  }

  // ── Save analysis results ─────────────────────────────────────
  _save() {
    try {
      fs.mkdirSync(DATA_DIR, { recursive: true });
      fs.writeFileSync(LOSS_HISTORY_PATH, JSON.stringify({
        history: this.history.slice(-500), // Keep last 500
        stats: this.stats,
        updated: new Date().toISOString(),
      }, null, 2));
    } catch (e) {
      console.log(`[Loss] Save failed: ${e.message}`);
    }
  }

  // ── Classify failure mode for a losing position ───────────────
  _classifyFailure(pos) {
    const failures = [];

    // Check: category with low edge
    const cat = pos.category || "other";
    if (this.stats.byCategory[cat] && this.stats.byCategory[cat].lossRate > 0.85) {
      failures.push("WRONG_CATEGORY");
    }

    // Check: entered at very high YES
    if (pos.yes_price_at_entry >= 0.98) {
      failures.push("HIGH_YES");
    }

    // Check: no whale confirmation
    if (pos.whale_signal === "NONE" || !pos.whale_signal) {
      failures.push("NO_WHALE");
    }

    // Check: exited at a loss
    if (pos.status === "exited" && pos.exit_profit_pct !== undefined && pos.exit_profit_pct < 0) {
      failures.push("BAD_EXIT");
    }

    // Check: was profitable at some point but held (we can't know for sure
    // without price history, but if the market eventually resolved NO and
    // we didn't exit, we may have missed a window)
    // Heuristic: if position was open for > 24h and lost, it might have
    // had a profitable window
    if (pos.status !== "exited") {
      const entryTime = new Date(pos.entry_time).getTime();
      const holdHours = (Date.now() - entryTime) / (1000 * 60 * 60);
      if (holdHours > 24) {
        failures.push("HELD_TOO_LONG");
      }
    }

    // Check: weather market with no model edge
    if (cat === "weather" && (!pos.kl_signal || pos.kl_signal === "NONE" || pos.kl_signal === "UNKNOWN")) {
      failures.push("NO_MODEL_EDGE");
    }

    // Return primary failure (most actionable)
    // Priority: WRONG_CATEGORY > HIGH_YES > THIN_BOOK > NO_WHALE > NO_MODEL_EDGE > HELD_TOO_LONG > BAD_EXIT > UNKNOWN
    const priority = ["WRONG_CATEGORY", "HIGH_YES", "THIN_BOOK", "NO_WHALE", "NO_MODEL_EDGE", "HELD_TOO_LONG", "BAD_EXIT"];
    for (const mode of priority) {
      if (failures.includes(mode)) return mode;
    }

    return "UNKNOWN";
  }

  // ── Analyze all resolved positions ────────────────────────────
  analyzeResolved(positions) {
    const resolved = positions.filter(p =>
      p.status === "resolved" || p.status === "lost" || p.status === "won" || p.status === "exited"
    );

    let newAnalyzed = 0;

    for (const pos of resolved) {
      // Skip if already analyzed
      if (this.history.find(h => h.id === pos.id && h.entry_time === pos.entry_time)) continue;

      const isWin = pos.status === "won" ||
        (pos.status === "resolved" && pos.result === "win") ||
        (pos.status === "exited" && pos.exit_profit_pct > 0);
      const cat = pos.category || "other";

      // Initialize category stats
      if (!this.stats.byCategory[cat]) {
        this.stats.byCategory[cat] = { wins: 0, losses: 0, lossRate: 0 };
      }

      if (isWin) {
        this.stats.byCategory[cat].wins++;
      } else {
        this.stats.byCategory[cat].losses++;
        // Classify failure mode
        const failure = this._classifyFailure(pos);
        if (!this.stats.byFailure) this.stats.byFailure = {};
        this.stats.byFailure[failure] = (this.stats.byFailure[failure] || 0) + 1;

        this.history.push({
          id: pos.id,
          question: pos.question?.slice(0, 80),
          category: cat,
          entry_time: pos.entry_time,
          yes_price_at_entry: pos.yes_price_at_entry,
          status: pos.status,
          failure_mode: failure,
          whale_signal: pos.whale_signal,
          kl_signal: pos.kl_signal,
          analyzed_at: new Date().toISOString(),
        });
      }

      // Update loss rate
      const catStats = this.stats.byCategory[cat];
      const total = catStats.wins + catStats.losses;
      catStats.lossRate = total > 0 ? catStats.losses / total : 0;

      newAnalyzed++;
    }

    if (newAnalyzed > 0) {
      // Update avoid categories (loss rate > 80% with at least 3 samples)
      this.stats.avoidCategories = Object.entries(this.stats.byCategory)
        .filter(([_, s]) => (s.wins + s.losses) >= 3 && s.lossRate > 0.80)
        .map(([cat]) => cat);

      // Compute loss rate trend (compare recent 10 vs older 10)
      const losses = this.history.filter(h => h.failure_mode);
      if (losses.length >= 20) {
        const recent = losses.slice(-10);
        const older = losses.slice(-20, -10);
        const recentLossRate = recent.length / Math.max(1, recent.length + 10); // Approximate
        const olderLossRate = older.length / Math.max(1, older.length + 10);
        if (recentLossRate < olderLossRate - 0.05) this.stats.lossRateTrend = "improving";
        else if (recentLossRate > olderLossRate + 0.05) this.stats.lossRateTrend = "degrading";
        else this.stats.lossRateTrend = "stable";
      }

      this.stats.totalAnalyzed = this.history.length;
      this._save();
      console.log(`[Loss] Analyzed ${newAnalyzed} resolved positions | ${this.stats.avoidCategories.length} avoided categories | trend: ${this.stats.lossRateTrend}`);
    }

    return this.stats;
  }

  // ── Entry signal: should we enter this market? ────────────────
  // Returns { pass, boost, reasons[] }
  entrySignal(market) {
    const reasons = [];
    let boost = 1.0;
    const cat = market.category || "other";

    // Check 1: Avoid category
    if (this.stats.avoidCategories.includes(cat)) {
      return {
        pass: false,
        boost: 0,
        reasons: [`Category "${cat}" on avoid list (loss rate >80%)`],
      };
    }

    // Check 2: Category loss rate
    const catStats = this.stats.byCategory[cat];
    if (catStats && (catStats.wins + catStats.losses) >= 3) {
      if (catStats.lossRate > 0.70) {
        boost *= 0.7;
        reasons.push(`${cat} loss rate ${(catStats.lossRate * 100).toFixed(0)}% — reducing size`);
      } else if (catStats.lossRate < 0.50) {
        boost *= 1.1;
        reasons.push(`${cat} loss rate ${(catStats.lossRate * 100).toFixed(0)}% — performing well`);
      }
    }

    // Check 3: High YES entry
    if (market.yes_price >= 0.98) {
      boost *= 0.5;
      reasons.push("YES >= 0.98 — historically poor edge");
    }

    // Check 4: Failure mode frequency
    if (this.stats.byFailure) {
      const totalLosses = Object.values(this.stats.byFailure).reduce((a, b) => a + b, 0);
      if (totalLosses >= 5) {
        const noWhalePct = (this.stats.byFailure.NO_WHALE || 0) / totalLosses;
        if (noWhalePct > 0.5 && cat !== "weather") {
          // Most losses had no whale — consider requiring whale
          reasons.push(`${(noWhalePct * 100).toFixed(0)}% of losses had no whale signal`);
        }

        const highYesPct = (this.stats.byFailure.HIGH_YES || 0) / totalLosses;
        if (highYesPct > 0.3) {
          boost *= 0.8;
          reasons.push(`${(highYesPct * 100).toFixed(0)}% of losses at YES >= 0.98`);
        }
      }
    }

    // Check 5: Trend
    if (this.stats.lossRateTrend === "degrading") {
      boost *= 0.9;
      reasons.push("Loss rate trend: degrading — slight caution");
    } else if (this.stats.lossRateTrend === "improving") {
      boost *= 1.05;
      reasons.push("Loss rate trend: improving");
    }

    return {
      pass: true,
      boost: Math.round(boost * 100) / 100,
      reasons,
    };
  }

  // ── Summary for logging ───────────────────────────────────────
  summary() {
    const cats = Object.entries(this.stats.byCategory || {})
      .sort((a, b) => (b[1].wins + b[1].losses) - (a[0].wins + a[0].losses))
      .map(([cat, s]) => `${cat}:${s.wins}W/${s.losses}L(${(s.lossRate * 100).toFixed(0)}%)`)
      .join(" ");

    const failures = Object.entries(this.stats.byFailure || {})
      .sort((a, b) => b[1] - a[1])
      .map(([mode, count]) => `${FAILURE_MODES[mode]?.label || mode}:${count}`)
      .join(" ");

    return {
      totalAnalyzed: this.stats.totalAnalyzed,
      categories: cats,
      failures: failures || "(none yet)",
      avoid: this.stats.avoidCategories.join(", ") || "(none)",
      trend: this.stats.lossRateTrend,
    };
  }
}

module.exports = { LossAnalyzer, FAILURE_MODES };
