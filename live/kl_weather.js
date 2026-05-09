/**
 * KL-Divergence Weather Model — Model-backed entry signal for weather markets
 *
 * Fetches NWS forecast for the relevant ASOS station, converts to a
 * probability distribution over temperature buckets, then computes KL-divergence
 * against the market's implied distribution from YES price.
 *
 * v5 additions:
 *   - Multi-model convergence gate (ECMWF + GFS via Open-Meteo API)
 *   - Airport station delta table (seasonal bias correction)
 *
 * Signal levels:
 *   STRONG  = D_KL >= 0.20 bits  → model strongly disagrees with market
 *   MODERATE = D_KL >= 0.10 bits  → meaningful divergence
 *   WEAK    = D_KL >= 0.05 bits  → slight edge
 *   NONE    = D_KL <  0.05 bits  → market and model agree, no edge
 *
 * Convergence gate modifiers:
 *   Spread > 5°F / 3°C → CAUTION (boost = 0.5x), overrides STRONG/MODERATE
 *   Spread 3-5°F / 2-3°C → cap boost at 1.0x
 *   Spread < 3°F / 2°C → normal boost from D_KL
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
const OPEN_METEO_API = "https://api.open-meteo.com/v1/forecast";
const CACHE_TTL_MS = 30 * 60 * 1000; // 30-min cache (forecasts update hourly)
const OPEN_METEO_CACHE_TTL_MS = 15 * 60 * 1000; // 15-min cache (models update every 6h)

// ── Station database: resolution stations for Polymarket weather markets ──
// Key = lowercase substring to match in market question, value = { station, lat, lon, unit }
const STATION_DB = {
  // North America - Tier 1 (major hubs)
  "new york":  { station: "KLGA", lat: 40.778, lon: -73.873, unit: "F", tier: 1 },
  "nyc":       { station: "KLGA", lat: 40.778, lon: -73.873, unit: "F", tier: 1 },
  "los angeles": { station: "KLAX", lat: 33.943, lon: -118.408, unit: "F", tier: 1 },
  "la ":       { station: "KLAX", lat: 33.943, lon: -118.408, unit: "F", tier: 1 },
  "chicago":   { station: "KORD", lat: 41.974, lon: -87.907, unit: "F", tier: 1 },
  "miami":     { station: "KMIA", lat: 25.793, lon: -80.316, unit: "F", tier: 1 },
  "dallas":    { station: "KDFW", lat: 32.897, lon: -97.038, unit: "F", tier: 1 },
  "houston":   { station: "KIAH", lat: 29.985, lon: -95.341, unit: "F", tier: 1 },
  "phoenix":   { station: "KPHX", lat: 33.436, lon: -112.012, unit: "F", tier: 1 },
  "denver":    { station: "KDEN", lat: 39.856, lon: -104.674, unit: "F", tier: 1 },
  "atlanta":   { station: "KATL", lat: 33.641, lon: -84.428, unit: "F", tier: 1 },
  "boston":    { station: "KBOS", lat: 42.363, lon: -71.006, unit: "F", tier: 1 },
  "seattle":   { station: "KSEA", lat: 47.45, lon: -122.309, unit: "F", tier: 1 },
  "san francisco": { station: "KSFO", lat: 37.619, lon: -122.375, unit: "F", tier: 1 },
  "washington": { station: "KDCA", lat: 38.852, lon: -77.038, unit: "F", tier: 1 },
  "d.c.":      { station: "KDCA", lat: 38.852, lon: -77.038, unit: "F", tier: 1 },
  // North America - Tier 2 (secondary cities, slower repricing = more edge)
  "detroit":   { station: "KDTW", lat: 42.216, lon: -83.355, unit: "F", tier: 2 },
  "minneapolis": { station: "KMSP", lat: 44.882, lon: -93.222, unit: "F", tier: 2 },
  "philadelphia": { station: "KPHL", lat: 39.872, lon: -75.241, unit: "F", tier: 2 },
  // Europe - Tier 1
  "london":    { station: "EGLC", lat: 51.505, lon: 0.0495, unit: "C", tier: 1 },
  // Europe - Tier 2
  "paris":     { station: "LFPB", lat: 48.969, lon: 2.441, unit: "C", tier: 2 },
  "berlin":    { station: "EDDB", lat: 52.36, lon: 13.504, unit: "C", tier: 2 },
  "madrid":    { station: "LEMD", lat: 40.492, lon: -3.569, unit: "C", tier: 2 },
  "rome":      { station: "LIRF", lat: 41.8, lon: 12.239, unit: "C", tier: 2 },
  "amsterdam": { station: "EHAM", lat: 52.309, lon: 4.764, unit: "C", tier: 2 },
  // East Asia - Tier 1
  "tokyo":     { station: "RJTT", lat: 35.552, lon: 139.78, unit: "C", tier: 1 },
  // East Asia - Tier 2
  "seoul":     { station: "RKSS", lat: 37.558, lon: 126.791, unit: "C", tier: 2 },
  "shanghai":  { station: "ZSSS", lat: 31.144, lon: 121.806, unit: "C", tier: 2 },
  "beijing":   { station: "ZBAA", lat: 40.08, lon: 116.585, unit: "C", tier: 2 },
  "singapore": { station: "WSSS", lat: 1.365, lon: 103.988, unit: "C", tier: 2 },
  // Oceania - Tier 2
  "sydney":    { station: "YSSY", lat: -33.946, lon: 151.177, unit: "C", tier: 2 },
  "melbourne": { station: "YMML", lat: -37.67, lon: 144.843, unit: "C", tier: 2 },
};

// ── Airport station delta table (v5) ─────────────────────────
// Seasonal offset: airport vs city-center. Negative = airport cooler.
// Values in station native unit (F for US, C for international).
// Source: polymarketweather.com research
//   KLGA: 3-5°F cooler than Midtown in summer (sea breeze off Long Island Sound)
//   LFPB: Le Bourget 1-2°C cooler than central Paris (urban heat island effect)
//   KLAX: 2°F cooler than downtown LA in summer (coastal marine layer)
//   KSFO: 2-4°F cooler than SF proper in summer (persistent marine layer / fog)
//   EGLC: London City minimal delta (~1°C, similar urban environment)
const STATION_DELTAS = {
  "KLGA": { winter: -1, spring: -2, summer: -4, fall: -2 },
  "LFPB": { winter: 0,  spring: -1, summer: -2, fall: -1 },
  "KLAX": { winter: 0,  spring: 0,  summer: -2, fall: -1 },
  "KSFO": { winter: 0,  spring: -1, summer: -3, fall: -1 },
  "EGLC": { winter: 0,  spring: 0,  summer: -1, fall: 0 },
};

// ── Helper: get season from month (0-indexed) ───────────────
function getSeason(month) {
  if (month >= 11 || month <= 1) return "winter";
  if (month >= 2 && month <= 4) return "spring";
  if (month >= 5 && month <= 7) return "summer";
  return "fall";
}

const NWS_HEADERS = {
  "User-Agent": "PolymarketBot/1.0 (weather-signal; contact: bot@example.com)",
  Accept: "application/ld+json",
};

class KLWeather {
  constructor() {
    this.pointsCache = new Map();   // lat,lon → points metadata (persistent)
    this.forecastCache = new Map(); // station → forecast data (TTL-based)
    this.openMeteoCache = new Map(); // lat,lon → ensemble data (TTL-based)
    this.enabled = true;
    this.priceTimestampCache = new Map();  // conditionId -> { price, timestamp } for latency tracking
    const tier1Count = Object.values(STATION_DB).filter(s => s.tier === 1).length;
    const tier2Count = Object.values(STATION_DB).filter(s => s.tier === 2).length;
    console.log("[KL-Weather] Initialized -- station DB has " + Object.keys(STATION_DB).length + " city mappings (" + tier1Count + " T1, " + tier2Count + " T2), " + Object.keys(STATION_DELTAS).length + " delta corrections");
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
    // Strip degree symbols from question so regex doesn't need to match them
    const q = question.replace(/\u00B0/g, "");

    // Pattern: "above NF" or "above N F" - \s* between keyword and digit
    const fMatch = q.match(/(?:above|over|exceed|at least|>\s*)\s*(\d+)\s*F/i);
    if (fMatch) return { threshold: parseInt(fMatch[1]), direction: "above", unit: "F" };

    const cMatch = q.match(/(?:above|over|exceed|at least|>\s*)\s*(\d+)\s*C/i);
    if (cMatch) return { threshold: parseInt(cMatch[1]), direction: "above", unit: "C" };

    // Pattern: "below NF" or "under NF"
    const fBelow = q.match(/(?:below|under|<\s*)\s*(\d+)\s*F/i);
    if (fBelow) return { threshold: parseInt(fBelow[1]), direction: "below", unit: "F" };

    const cBelow = q.match(/(?:below|under|<\s*)\s*(\d+)\s*C/i);
    if (cBelow) return { threshold: parseInt(cBelow[1]), direction: "below", unit: "C" };

    // Pattern: "between X and Y F"
    const fBetween = q.match(/between\s+(\d+)\s*F\s+and\s+(\d+)\s*F/i);
    if (fBetween) return { threshold: parseInt(fBetween[1]), direction: "above", unit: "F", upper: parseInt(fBetween[2]) };

    const cBetween = q.match(/between\s+(\d+)\s*C\s+and\s+(\d+)\s*C/i);
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

  // ── Fetch Open-Meteo ensemble data (v5) ──────────────────
  // Returns ECMWF and GFS daily max temps for the target date
  async _fetchOpenMeteo(lat, lon, targetDate, unit) {
    const cacheKey = `${lat.toFixed(2)},${lon.toFixed(2)}`;
    const cached = this.openMeteoCache.get(cacheKey);
    if (cached && Date.now() - cached.timestamp < OPEN_METEO_CACHE_TTL_MS) {
      return cached.data;
    }

    try {
      const targetDay = targetDate.toISOString().slice(0, 10);
      const resp = await axios.get(OPEN_METEO_API, {
        params: {
          latitude: lat,
          longitude: lon,
          daily: "temperature_2m_max",
          models: "ecmwf_ifs,gfs_seamless",
          forecast_days: 3,
          timezone: "auto",
        },
        timeout: 10000,
      });

      const daily = resp.data?.daily;
      if (!daily || !daily.time) return null;

      // Find target date index
      const dayIndex = daily.time.indexOf(targetDay);
      if (dayIndex === -1) {
        // Try tomorrow if today not in forecast (Open-Meteo sometimes starts from tomorrow)
        const tomorrow = new Date(targetDate);
        tomorrow.setDate(tomorrow.getDate() + 1);
        const tomorrowStr = tomorrow.toISOString().slice(0, 10);
        const altIndex = daily.time.indexOf(tomorrowStr);
        if (altIndex === -1) return null;
        // Use the altIndex but log it
        return this._extractEnsemble(daily, altIndex, unit);
      }

      const result = this._extractEnsemble(daily, dayIndex, unit);
      this.openMeteoCache.set(cacheKey, { data: result, timestamp: Date.now() });
      return result;
    } catch (e) {
      console.log(`[KL-Weather] Open-Meteo fetch error: ${e.message?.slice(0, 60)}`);
      return null;
    }
  }

  // ── Extract ensemble values from Open-Meteo daily response ──
  _extractEnsemble(daily, dayIndex, unit) {
    // Open-Meteo returns Celsius. Model-specific keys:
    //   ecmwf_ifs_temperature_2m_max, gfs_temperature_2m_max
    const ecmwfMaxC = daily.temperature_2m_max_ecmwf_ifs?.[dayIndex];
    const gfsMaxC = daily.temperature_2m_max_gfs_seamless?.[dayIndex];

    if (ecmwfMaxC === null || ecmwfMaxC === undefined ||
        gfsMaxC === null || gfsMaxC === undefined) {
      return null;
    }

    // Convert to station's native unit
    let ecmwfMax = ecmwfMaxC;
    let gfsMax = gfsMaxC;
    if (unit === "F") {
      ecmwfMax = ecmwfMaxC * 9 / 5 + 32;
      gfsMax = gfsMaxC * 9 / 5 + 32;
    }

    return {
      ecmwfMax: Math.round(ecmwfMax * 10) / 10,
      gfsMax: Math.round(gfsMax * 10) / 10,
      spread: Math.round(Math.abs(ecmwfMax - gfsMax) * 10) / 10,
      unit,
    };
  }

  // ── Apply airport station delta correction (v5) ──────────
  // Shifts temperature distribution by the seasonal bias at the airport
  _applyStationDelta(temps, stationInfo) {
    const deltas = STATION_DELTAS[stationInfo.station];
    if (!deltas) return temps; // No correction for this station

    const season = getSeason(new Date().getMonth());
    const delta = deltas[season];

    if (!delta || delta === 0) return temps;

    // Apply delta to shift the entire temperature distribution
    return {
      ...temps,
      high: temps.high + delta,
      low: temps.low + delta,
      mean: temps.mean + delta,
      hourlyTemps: temps.hourlyTemps.map(t => t + delta),
      deltaApplied: delta,
      deltaSeason: season,
    };
  }

  // ── Convergence gate logic (v5) ──────────────────────────
  // Modifies signal level/boost based on ECMWF vs GFS model spread
  _convergenceGate(ensemble, currentLevel, currentBoost) {
    if (!ensemble) {
      return { level: currentLevel, boost: currentBoost, gateReason: "no ensemble data" };
    }

    const spread = ensemble.spread;
    const isF = ensemble.unit === "F";

    // Thresholds: F uses 3/5, C uses 2/3
    const cautionThreshold = isF ? 5 : 3;
    const reduceThreshold = isF ? 3 : 2;

    if (spread > cautionThreshold) {
      // Major model disagreement → override to CAUTION
      return {
        level: "CAUTION",
        boost: 0.5,
        gateReason: "spread=" + spread.toFixed(1) + "\u00B0" + ensemble.unit + " > " + cautionThreshold + "\u00B0" + ensemble.unit + ", models disagree",
      };
    }

    if (spread > reduceThreshold) {
      // Moderate disagreement → cap boost at 1.0x
      const cappedBoost = Math.min(currentBoost, 1.0);
      return {
        level: currentLevel,
        boost: cappedBoost,
        gateReason: "spread=" + spread.toFixed(1) + "\u00B0" + ensemble.unit + " (" + reduceThreshold + "-" + cautionThreshold + "), boost capped at 1.0x",
      };
    }

    // Good agreement → normal boost from D_KL
    return {
      level: currentLevel,
      boost: currentBoost,
      gateReason: "spread=" + spread.toFixed(1) + "\u00B0" + ensemble.unit + " < " + reduceThreshold + "\u00B0" + ensemble.unit + ", models agree",
    };
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
    if (!temps || temps.sampleSize < 1) return null;

    const { mean, stdDev } = temps;
    const threshold = bucket.threshold;

    // Use high temperature distribution for "above" questions
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

    // Step 4: Try NWS forecast first, then fall back to Open-Meteo
    let temps = null;
    let forecastSource = "NWS";
    const forecast = await this._fetchForecast(station.lat, station.lon);
    if (forecast) {
      temps = this._extractForecastTemps(forecast.periods, targetDate, station.unit);
    }

    // Step 4b: If NWS failed, use Open-Meteo ECMWF as fallback
    if (!temps) {
      forecastSource = "Open-Meteo";
      const ensemble = await this._fetchOpenMeteo(station.lat, station.lon, targetDate, station.unit);
      if (ensemble && ensemble.ecmwfMax !== null) {
        // Build a simple temperature distribution from ECMWF max
        // Assume std dev of 3 degrees (conservative uncertainty)
        const ecmwfHigh = ensemble.ecmwfMax;
        temps = {
          high: ecmwfHigh,
          low: ecmwfHigh - 8,  // rough estimate
          mean: ecmwfHigh - 2,  // daily mean is lower than max
          stdDev: 3.0,
          hourlyTemps: [],  // not available from Open-Meteo daily
          sampleSize: 1,
          fromEnsemble: true,
        };
      }
    }

    if (!temps) {
      return { level: "UNKNOWN", dkl: 0, boost: 1.0, reason: "no forecast data for " + station.station + " (NWS and Open-Meteo both failed)" };
    }

    // Step 5 (v5): Apply airport station delta correction
    const deltaTemps = this._applyStationDelta(temps, station);

    // Step 6: Compute model probability (using delta-adjusted temps)
    const pModel = this._forecastProbability(deltaTemps, bucket);
    if (pModel === null) {
      return { level: "UNKNOWN", dkl: 0, boost: 1.0, reason: "insufficient forecast samples" };
    }

    // Step 7: Compute KL-divergence vs market
    const pMarket = market.yes_price;
    const dkl = this._klDivergence(pModel, pMarket);

    // Step 8: Signal classification (from D_KL)
    const modelSaysNo = pModel < pMarket;
    const edgeDirection = modelSaysNo ? "model-supports-NO" : "model-supports-YES";

    let level, boost, reason;
    if (dkl >= 0.20 && modelSaysNo) {
      level = "STRONG";
      boost = 1.5;
      reason = "D_KL=" + dkl.toFixed(3) + " bits, model P(YES)=" + (pModel * 100).toFixed(1) + "% vs market " + (pMarket * 100).toFixed(1) + "%, " + edgeDirection + ", " + station.station + " high=" + deltaTemps.high.toFixed(1) + "\u00B0 via " + forecastSource;
    } else if (dkl >= 0.10 && modelSaysNo) {
      level = "MODERATE";
      boost = 1.2;
      reason = "D_KL=" + dkl.toFixed(3) + " bits, model P(YES)=" + (pModel * 100).toFixed(1) + "% vs market " + (pMarket * 100).toFixed(1) + "%, " + edgeDirection + ", " + station.station + " high=" + deltaTemps.high.toFixed(1) + "\u00B0 via " + forecastSource;
    } else if (dkl >= 0.05 && modelSaysNo) {
      level = "WEAK";
      boost = 1.0;
      reason = "D_KL=" + dkl.toFixed(3) + " bits, model P(YES)=" + (pModel * 100).toFixed(1) + "% vs market " + (pMarket * 100).toFixed(1) + "%, " + edgeDirection;
    } else if (!modelSaysNo && dkl >= 0.10) {
      level = "CAUTION";
      boost = 0.5;
      reason = "D_KL=" + dkl.toFixed(3) + " bits, model says YES more likely (P(YES)=" + (pModel * 100).toFixed(1) + "% vs " + (pMarket * 100).toFixed(1) + "%), " + station.station;
    } else {
      level = "NONE";
      boost = 1.0;
      reason = "D_KL=" + dkl.toFixed(3) + " bits, model and market agree, " + station.station + " high=" + deltaTemps.high.toFixed(1) + "\u00B0 via " + forecastSource;
    }

    // Step 9 (v5): Multi-model convergence gate
    const ensemble = await this._fetchOpenMeteo(station.lat, station.lon, targetDate, station.unit);
    const gated = this._convergenceGate(ensemble, level, boost);
    level = gated.level;
    boost = gated.boost;

    // Step 10 (v7): Model freshness
    const freshInfo = this._computeModelFreshness();
    boost = boost * freshInfo.boost;

    // Step 11 (v7): City tier
    const cityTier = station.tier || 1;
    const secondaryCityBoost = cityTier === 2 ? 1.3 : 1.0;
    boost = boost * secondaryCityBoost;

    // Append convergence gate info to reason
    if (gated.gateReason) {
      reason = reason + " | " + gated.gateReason;
    }
    // Append freshness info
    reason = reason + " | freshness=" + freshInfo.freshness + " (age=" + freshInfo.ageMinutes + "min)";
    if (cityTier === 2) {
      reason = reason + " | TIER_2 city (1.3x boost)";
    }

    // Append delta info to reason
    if (deltaTemps.deltaApplied && deltaTemps.deltaApplied !== 0) {
      reason = reason + " | delta=" + deltaTemps.deltaApplied + "\u00B0" + station.unit + " (" + deltaTemps.deltaSeason + ")";
    }

    return {
      level,
      dkl: Math.round(dkl * 1000) / 1000,
      pModel: Math.round(pModel * 1000) / 1000,
      pMarket: Math.round(pMarket * 1000) / 1000,
      boost,
      reason,
      station: station.station,
      forecastHigh: deltaTemps.high,
      forecastLow: deltaTemps.low,
      rawForecastHigh: temps.high,
      forecastSource,
      deltaApplied: deltaTemps.deltaApplied || 0,
      deltaSeason: deltaTemps.deltaSeason || null,
      convergenceSpread: ensemble ? ensemble.spread : null,
      convergenceECMWF: ensemble ? ensemble.ecmwfMax : null,
      convergenceGFS: ensemble ? ensemble.gfsMax : null,
      sampleSize: temps.sampleSize,
      freshness: freshInfo.freshness,
      freshnessAgeMinutes: freshInfo.ageMinutes,
      freshnessBoost: freshInfo.boost,
      cityTier,
      secondaryCityBoost,
    };
  }

  // ── Clear forecast cache ──────────────────────────────────────
  clearCache() {
    this.forecastCache.clear();
    this.openMeteoCache.clear();
  }
}

module.exports = { KLWeather };
