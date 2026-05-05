/**
 * KL-Divergence Weather Model — Model-backed entry signal for weather markets
 *
 * Fetches NWS forecast for the relevant ASOS station, converts to a
 * probability distribution over temperature buckets, then computes KL-divergence
 * against the market's implied distribution from YES price.
 *
 * Signal levels:
 *   STRONG  = D_KL >= 0.20 bits  → model strongly disagrees with market
 *   MODERATE = D_KL >= 0.10 bits  → meaningful divergence
 *   WEAK    = D_KL >= 0.05 bits  → slight edge
 *   NONE    = D_KL <  0.05 bits  → market and model agree, no edge
 *
 * Usage:
 *   const { KLWeather } = require("./kl_weather");
 *   const klw = new KLWeather();
 *   const signal = await klw.evaluate(market);
 */

const axios = require("axios");
const fs = require("fs");
const path = require("path");

const NWS_API = "https://api.weather.gov";
const CACHE_TTL_MS = 30 * 60 * 1000; // 30-min cache (forecasts update hourly)

// ── Station database: resolution stations for Polymarket weather markets ──
// Key = lowercase substring to match in market question, value = { station, lat, lon, unit }
const STATION_DB = {
  // North America
  "new york":  { station: "KLGA", lat: 40.778, lon: -73.873, unit: "F" },
  "nyc":       { station: "KLGA", lat: 40.778, lon: -73.873, unit: "F" },
  "los angeles": { station: "KLAX", lat: 33.943, lon: -118.408, unit: "F" },
  "la ":       { station: "KLAX", lat: 33.943, lon: -118.408, unit: "F" },
  "chicago":   { station: "KORD", lat: 41.974, lon: -87.907, unit: "F" },
  "miami":     { station: "KMIA", lat: 25.793, lon: -80.316, unit: "F" },
  "dallas":    { station: "KDFW", lat: 32.897, lon: -97.038, unit: "F" },
  "houston":   { station: "KIAH", lat: 29.985, lon: -95.341, unit: "F" },
  "phoenix":   { station: "KPHX", lat: 33.436, lon: -112.012, unit: "F" },
  "denver":    { station: "KDEN", lat: 39.856, lon: -104.674, unit: "F" },
  "atlanta":   { station: "KATL", lat: 33.641, lon: -84.428, unit: "F" },
  "boston":    { station: "KBOS", lat: 42.363, lon: -71.006, unit: "F" },
  "seattle":   { station: "KSEA", lat: 47.45, lon: -122.309, unit: "F" },
  "san francisco": { station: "KSFO", lat: 37.619, lon: -122.375, unit: "F" },
  "washington": { station: "KDCA", lat: 38.852, lon: -77.038, unit: "F" },
  "d.c.":      { station: "KDCA", lat: 38.852, lon: -77.038, unit: "F" },
  "detroit":   { station: "KDTW", lat: 42.216, lon: -83.355, unit: "F" },
  "minneapolis": { station: "KMSP", lat: 44.882, lon: -93.222, unit: "F" },
  "philadelphia": { station: "KPHL", lat: 39.872, lon: -75.241, unit: "F" },
  // Europe
  "london":    { station: "EGLC", lat: 51.505, lon: 0.0495, unit: "C" },
  "paris":     { station: "LFPB", lat: 48.969, lon: 2.441, unit: "C" },
  "berlin":    { station: "EDDB", lat: 52.36, lon: 13.504, unit: "C" },
  "madrid":    { station: "LEMD", lat: 40.492, lon: -3.569, unit: "C" },
  "rome":      { station: "LIRF", lat: 41.8, lon: 12.239, unit: "C" },
  "amsterdam": { station: "EHAM", lat: 52.309, lon: 4.764, unit: "C" },
  // East Asia
  "tokyo":     { station: "RJTT", lat: 35.552, lon: 139.78, unit: "C" },
  "seoul":     { station: "RKSS", lat: 37.558, lon: 126.791, unit: "C" },
  "shanghai":  { station: "ZSSS", lat: 31.144, lon: 121.806, unit: "C" },
  "beijing":   { station: "ZBAA", lat: 40.08, lon: 116.585, unit: "C" },
  "singapore": { station: "WSSS", lat: 1.365, lon: 103.988, unit: "C" },
  // Oceania
  "sydney":    { station: "YSSY", lat: -33.946, lon: 151.177, unit: "C" },
  "melbourne": { station: "YMML", lat: -37.67, lon: 144.843, unit: "C" },
};

const NWS_HEADERS = {
  "User-Agent": "PolymarketBot/1.0 (weather-signal; contact: bot@example.com)",
  Accept: "application/ld+json",
};

class KLWeather {
  constructor() {
    this.pointsCache = new Map();   // lat,lon → points metadata (persistent)
    this.forecastCache = new Map(); // station → forecast data (TTL-based)
    this.enabled = true;
    console.log("[KL-Weather] Initialized — station DB has " + Object.keys(STATION_DB).length + " city mappings");
  }

  // ── Identify station from market question ──────────────────────
  _matchStation(question) {
    const q = question.toLowerCase();
    // Try longer matches first (e.g. "san francisco" before "la ")
    const sorted = Object.entries(STATION_DB).sort((a, b) => b[0].length - a[0].length);
    for (const [key, station] of sorted) {
      if (q.includes(key)) return station;
    }
    return null;
  }

  // ── Parse temperature bucket from market question ─────────────
  // Questions like: "Will the high temperature in NYC on May 5 be above 75°F?"
  // or: "Highest temperature in London on May 5: Above 22°C?"
  // Returns { threshold, direction, unit } or null
  _parseBucket(question) {
    // Pattern: "above N°F" or "over N°F" or "> N°F" or "at least N°F"
    const fMatch = question.match(/(?:above|over|exceed|at least|>\s*)(\d+)\s*°?\s*F/i);
    if (fMatch) return { threshold: parseInt(fMatch[1]), direction: "above", unit: "F" };

    const cMatch = question.match(/(?:above|over|exceed|at least|>\s*)(\d+)\s*°?\s*C/i);
    if (cMatch) return { threshold: parseInt(cMatch[1]), direction: "above", unit: "C" };

    // Pattern: "below N°F" or "under N°F"
    const fBelow = question.match(/(?:below|under|<\s*)(\d+)\s*°?\s*F/i);
    if (fBelow) return { threshold: parseInt(fBelow[1]), direction: "below", unit: "F" };

    const cBelow = question.match(/(?:below|under|<\s*)(\d+)\s*C/i);
    if (cBelow) return { threshold: parseInt(cBelow[1]), direction: "below", unit: "C" };

    // Pattern: "between X and Y °F" — threshold is midpoint, we'll handle differently
    const fBetween = question.match(/between\s+(\d+)\s*°?\s*F\s+and\s+(\d+)\s*°?\s*F/i);
    if (fBetween) return { threshold: parseInt(fBetween[1]), direction: "above", unit: "F", upper: parseInt(fBetween[2]) };

    const cBetween = question.match(/between\s+(\d+)\s*°?\s*C\s+and\s+(\d+)\s*°?\s*C/i);
    if (cBetween) return { threshold: parseInt(cBetween[1]), direction: "above", unit: "C", upper: parseInt(cBetween[2]) };

    return null;
  }

  // ── Parse target date from question ────────────────────────────
  _parseDate(question) {
    const q = question.toLowerCase();
    const now = new Date();

    // "on May 5" or "on May 5th"
    const dateMatch = q.match(/on\s+(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?/i);
    if (dateMatch) {
      const months = { jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5, jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11 };
      const month = months[dateMatch[1].toLowerCase().slice(0, 3)];
      if (month !== undefined) {
        const day = parseInt(dateMatch[2]);
        const year = now.getFullYear();
        const target = new Date(year, month, day);
        // If the date is in the past, assume next year
        if (target < now && (now - target) > 7 * 86400000) {
          target.setFullYear(year + 1);
        }
        return target;
      }
    }

    // "today" / "tomorrow"
    if (q.includes("today")) return now;
    if (q.includes("tomorrow")) {
      const t = new Date(now);
      t.setDate(t.getDate() + 1);
      return t;
    }

    // Default: assume today (forecast will cover it)
    return now;
  }

  // ── Fetch NWS forecast for a lat/lon ──────────────────────────
  async _fetchForecast(lat, lon) {
    const cacheKey = `${lat},${lon}`;
    const cached = this.forecastCache.get(cacheKey);
    if (cached && Date.now() - cached.timestamp < CACHE_TTL_MS) {
      return cached.data;
    }

    try {
      // Step 1: Get points metadata
      const pointsResp = await axios.get(`${NWS_API}/points/${lat.toFixed(4)},${lon.toFixed(4)}`, {
        headers: NWS_HEADERS,
        timeout: 10000,
      });
      const forecastUrl = pointsResp.data?.properties?.forecast;
      const gridpointUrl = pointsResp.data?.properties?.forecastGridData;
      if (!forecastUrl && !gridpointUrl) {
        return null;
      }

      // Step 2: Get hourly forecast (more granular for temperature buckets)
      const hourlyUrl = pointsResp.data?.properties?.forecastHourly || forecastUrl;
      const fcstResp = await axios.get(hourlyUrl, {
        headers: NWS_HEADERS,
        timeout: 10000,
      });
      const periods = fcstResp.data?.properties?.periods || [];
      if (periods.length === 0) return null;

      // Step 3: Also get gridpoint data for max/min temperature if available
      let gridData = null;
      if (gridpointUrl) {
        try {
          const gridResp = await axios.get(gridpointUrl, {
            headers: NWS_HEADERS,
            timeout: 10000,
          });
          gridData = gridResp.data?.properties || null;
        } catch {
          // Non-critical — hourly forecast is sufficient
        }
      }

      const result = { periods, gridData, fetched: Date.now() };
      this.forecastCache.set(cacheKey, { data: result, timestamp: Date.now() });
      return result;
    } catch (e) {
      // Non-US stations won't have NWS data — that's fine
      if (e.response?.status === 404) {
        return null;
      }
      console.log(`[KL-Weather] Forecast fetch error: ${e.message?.slice(0, 60)}`);
      return null;
    }
  }

  // ── Extract forecast high/low for target date ─────────────────
  _extractForecastTemps(periods, targetDate, unit) {
    const targetDay = targetDate.toISOString().slice(0, 10);
    const isCelsius = unit === "C";

    // Collect all hourly temperatures for the target day
    const hourlyTemps = [];
    for (const period of periods) {
      const startTime = period.startTime;
      if (!startTime) continue;
      const periodDay = startTime.slice(0, 10);
      if (periodDay !== targetDay) continue;

      const temp = period.temperature;
      if (temp === null || temp === undefined) continue;

      // Convert to target unit if needed
      let t = temp;
      const tempUnit = (period.temperatureUnit || "F").toUpperCase();
      if (isCelsius && tempUnit === "F") t = (temp - 32) * 5 / 9;
      if (!isCelsius && tempUnit === "C") t = temp * 9 / 5 + 32;

      hourlyTemps.push(t);
    }

    if (hourlyTemps.length === 0) return null;

    const high = Math.max(...hourlyTemps);
    const low = Math.min(...hourlyTemps);
    const mean = hourlyTemps.reduce((a, b) => a + b, 0) / hourlyTemps.length;

    // Estimate spread (standard deviation of hourly temps)
    const variance = hourlyTemps.reduce((sum, t) => sum + (t - mean) ** 2, 0) / hourlyTemps.length;
    const stdDev = Math.sqrt(variance);

    return { high, low, mean, stdDev, hourlyTemps, sampleSize: hourlyTemps.length };
  }

  // ── Build probability distribution from forecast ──────────────
  // Returns P(temp >= threshold) using a normal approximation
  _forecastProbability(temps, bucket) {
    if (!temps || temps.sampleSize < 4) return null;

    const { mean, stdDev } = temps;
    const threshold = bucket.threshold;

    // Use high temperature distribution for "above" questions
    // The forecast high is the peak, spread by stdDev
    // For "above X", we want P(high >= X)
    // Model the high as Normal(mean, sigma) where sigma captures forecast uncertainty
    const sigma = Math.max(stdDev, 2.0); // Minimum 2° uncertainty

    // Normal CDF approximation (Abramowitz & Stegun)
    const z = (threshold - mean) / sigma;
    const p_above = 1 - this._normalCDF(z);
    const p_below = this._normalCDF(z);

    if (bucket.direction === "above") return p_above;
    if (bucket.direction === "below") return p_below;

    // "between X and Y" — P(X <= temp <= Y)
    if (bucket.upper) {
      const z2 = (bucket.upper - mean) / sigma;
      return this._normalCDF(z2) - this._normalCDF(z);
    }

    return p_above;
  }

  // ── Normal CDF (standard) ─────────────────────────────────────
  _normalCDF(z) {
    // Rational approximation (Abramowitz & Stegun 26.2.17)
    const a1 = 0.254829592;
    const a2 = -0.284496736;
    const a3 = 1.421413741;
    const a4 = -1.453152027;
    const a5 = 1.061405429;
    const p = 0.3275911;

    const sign = z < 0 ? -1 : 1;
    const x = Math.abs(z) / Math.sqrt(2);

    const t = 1.0 / (1.0 + p * x);
    const y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * Math.exp(-x * x);

    return 0.5 * (1.0 + sign * y);
  }

  // ── KL-divergence D_KL(P_model || P_market) ──────────────────
  // P_model = forecast probability of YES
  // P_market = market implied probability (yes_price)
  // We compute D_KL in bits
  _klDivergence(pModel, pMarket) {
    // Clamp to avoid log(0)
    const p1 = Math.max(1e-6, Math.min(1 - 1e-6, pModel));
    const p2 = Math.max(1e-6, Math.min(1 - 1e-6, pMarket));
    const q1 = 1 - p1;
    const q2 = 1 - p2;

    // D_KL(Bernoulli(p1) || Bernoulli(p2))
    const kl = p1 * Math.log2(p1 / p2) + q1 * Math.log2(q1 / q2);
    return kl;
  }

  // ── Main entry: evaluate a weather market ─────────────────────
  async evaluate(market) {
    const question = market.question || "";
    const category = market.category || "";

    // Only apply to weather markets
    if (category !== "weather") {
      return { level: "SKIP", dkl: 0, boost: 1.0, reason: "not a weather market" };
    }

    // Step 1: Match station
    const station = this._matchStation(question);
    if (!station) {
      return { level: "UNKNOWN", dkl: 0, boost: 1.0, reason: "no station match" };
    }

    // Step 2: Parse temperature bucket
    const bucket = this._parseBucket(question);
    if (!bucket) {
      return { level: "UNKNOWN", dkl: 0, boost: 1.0, reason: "no temp bucket in question" };
    }

    // Step 3: Parse date
    const targetDate = this._parseDate(question);

    // Step 4: Fetch forecast
    const forecast = await this._fetchForecast(station.lat, station.lon);
    if (!forecast) {
      return { level: "UNKNOWN", dkl: 0, boost: 1.0, reason: `no NWS data for ${station.station}` };
    }

    // Step 5: Extract temperature distribution
    const temps = this._extractForecastTemps(forecast.periods, targetDate, station.unit);
    if (!temps) {
      return { level: "UNKNOWN", dkl: 0, boost: 1.0, reason: "no forecast data for target date" };
    }

    // Step 6: Compute model probability
    const pModel = this._forecastProbability(temps, bucket);
    if (pModel === null) {
      return { level: "UNKNOWN", dkl: 0, boost: 1.0, reason: "insufficient forecast samples" };
    }

    // Step 7: Compute KL-divergence vs market
    const pMarket = market.yes_price; // Market's implied P(YES)
    const dkl = this._klDivergence(pModel, pMarket);

    // Step 8: Signal classification
    // We also need to know the *direction* of disagreement
    // If model says YES is more likely than market → model agrees with market (bad for BUY_NO)
    // If model says YES is less likely than market → model supports BUY_NO
    const modelSaysNo = pModel < pMarket;
    const edgeDirection = modelSaysNo ? "model-supports-NO" : "model-supports-YES";

    let level, boost, reason;
    if (dkl >= 0.20 && modelSaysNo) {
      level = "STRONG";
      boost = 1.5;
      reason = `D_KL=${dkl.toFixed(3)} bits, model P(YES)=${(pModel * 100).toFixed(1)}% vs market ${(pMarket * 100).toFixed(1)}%, ${edgeDirection}, ${station.station} high=${temps.high.toFixed(1)}°`;
    } else if (dkl >= 0.10 && modelSaysNo) {
      level = "MODERATE";
      boost = 1.2;
      reason = `D_KL=${dkl.toFixed(3)} bits, model P(YES)=${(pModel * 100).toFixed(1)}% vs market ${(pMarket * 100).toFixed(1)}%, ${edgeDirection}, ${station.station} high=${temps.high.toFixed(1)}°`;
    } else if (dkl >= 0.05 && modelSaysNo) {
      level = "WEAK";
      boost = 1.0;
      reason = `D_KL=${dkl.toFixed(3)} bits, model P(YES)=${(pModel * 100).toFixed(1)}% vs market ${(pMarket * 100).toFixed(1)}%, ${edgeDirection}`;
    } else if (!modelSaysNo && dkl >= 0.10) {
      // Model DISAGREES with our BUY_NO thesis — reduce size
      level = "CAUTION";
      boost = 0.5;
      reason = `D_KL=${dkl.toFixed(3)} bits, model says YES more likely (P(YES)=${(pModel * 100).toFixed(1)}% vs ${(pMarket * 100).toFixed(1)}%), ${station.station}`;
    } else {
      level = "NONE";
      boost = 1.0;
      reason = `D_KL=${dkl.toFixed(3)} bits, model and market agree, ${station.station} high=${temps.high.toFixed(1)}°`;
    }

    return {
      level,
      dkl: Math.round(dkl * 1000) / 1000,
      pModel: Math.round(pModel * 1000) / 1000,
      pMarket: Math.round(pMarket * 1000) / 1000,
      boost,
      reason,
      station: station.station,
      forecastHigh: temps.high,
      forecastLow: temps.low,
      sampleSize: temps.sampleSize,
    };
  }

  // ── Clear forecast cache ──────────────────────────────────────
  clearCache() {
    this.forecastCache.clear();
  }
}

module.exports = { KLWeather };
