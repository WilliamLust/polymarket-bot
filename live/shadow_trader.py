"""
Polymarket Shadow Trader — Paper Trading Module

Monitors live Polymarket markets for the 95+¢ YES strategy.
Reads real orderbook data (works from US IP — read-only endpoints).
Logs hypothetical trades with real fill prices from the orderbook.
Tracks paper P&L against actual market resolution.

This validates the negative slippage assumption WITHOUT risking real money
and WITHOUT needing a VPS (read-only CLOB API is not geoblocked).

Usage:
    source venv/bin/activate
    python live/shadow_trader.py                    # One scan + report
    python live/shadow_trader.py --loop             # Continuous monitoring
    python live/shadow_trader.py --loop --interval 60  # Check every 60s
    python live/shadow_trader.py --status            # Show tracked positions
    python live/shadow_trader.py --pnl               # Show P&L on resolved positions
"""

import os
import sys
import json
import time
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal

import requests

# ── Configuration ──────────────────────────────────────────────

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
FEE_RATE = 0.02
YES_MIN = 0.95          # Only enter when YES >= 95¢
YES_MAX = 0.99          # Skip YES >= 99¢ — too certain, no NO liquidity
POSITION_SIZE = 1.0      # $1 per position (paper)
MAX_DAILY_POSITIONS = 20
MAX_DAILY_LOSS = 50.0
MIN_LIQUIDITY = 5000.0   # Min $ volume for a market to be tradeable
MAX_BOOK_CHECKS = 15     # Max orderbooks to check per scan
DATA_DIR = Path("live/shadow_data")

# ── Logging ────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("shadow")

# ── Gamma API: Market Discovery ────────────────────────────────

def get_active_markets(min_volume: float = MIN_LIQUIDITY) -> list[dict]:
    """Fetch active markets from Gamma API, paginated. Early-exit on low volume."""
    markets = []
    offset = 0
    limit = 100
    
    while True:
        url = f"{GAMMA_API}/markets"
        params = {
            "closed": "false",
            "active": "true",
            "limit": limit,
            "offset": offset,
            "order": "volume",
            "ascending": "false",
        }
        
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            batch = resp.json()
        except Exception as e:
            log.error(f"Gamma API error: {e}")
            break
        
        if not batch:
            break
        
        for m in batch:
            try:
                outcome_prices = m.get("outcomePrices", "[]")
                if isinstance(outcome_prices, str):
                    prices = json.loads(outcome_prices)
                else:
                    prices = outcome_prices
                
                if not prices or len(prices) < 1:
                    continue
                
                yes_price = float(prices[0])
                volume = float(m.get("volume", 0) or 0)
                
                # Skip low-volume early
                if volume < min_volume:
                    continue
                
                clob_token_ids = m.get("clobTokenIds", "[]")
                if isinstance(clob_token_ids, str):
                    token_ids = json.loads(clob_token_ids)
                else:
                    token_ids = clob_token_ids
                
                if not token_ids or len(token_ids) < 2:
                    continue
                
                markets.append({
                    "id": m.get("id", ""),
                    "question": m.get("question", ""),
                    "slug": m.get("slug", ""),
                    "yes_price": yes_price,
                    "volume": volume,
                    "yes_token_id": token_ids[0],
                    "no_token_id": token_ids[1] if len(token_ids) > 1 else None,
                    "end_date": m.get("endDate", ""),
                    "category": m.get("category", "other"),
                    "outcome_prices": prices,
                })
            except Exception:
                continue
        
        offset += limit
        # Early exit: results are sorted by volume desc.
        # Once we hit a batch with no qualifying markets, stop.
        if len(batch) < limit:
            break
        
        # Also stop if we've fetched enough pages (top 500 by volume is plenty)
        if offset >= 500:
            break
    
    return markets


def get_high_yes_markets(min_yes: float = YES_MIN) -> list[dict]:
    """Filter to markets where min_yes <= YES < YES_MAX — our entry zone."""
    all_markets = get_active_markets()
    high_yes = [m for m in all_markets if min_yes <= m["yes_price"] < YES_MAX]
    log.info(f"Found {len(high_yes)} markets with {min_yes:.0%} <= YES < {YES_MAX:.0%} (of {len(all_markets)} active)")
    return high_yes


# ── CLOB API: Orderbook Data ──────────────────────────────────

def get_orderbook(token_id: str) -> dict | None:
    """Get real orderbook from CLOB API (read-only, works from US IP)."""
    url = f"{CLOB_API}/book"
    params = {"token_id": token_id}
    
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning(f"Orderbook fetch failed for {token_id[:16]}...: {e}")
        return None


def get_no_fill_price(market: dict, position_size: float = POSITION_SIZE) -> dict | None:
    """
    Calculate realistic NO fill price from the live orderbook.
    Walk the NO asks to fill `position_size` worth of shares.
    Returns fill details or None if can't fill.
    """
    no_token_id = market.get("no_token_id")
    if not no_token_id:
        return None
    
    book = get_orderbook(no_token_id)
    if not book:
        return None
    
    asks = book.get("asks", [])
    if not asks:
        return None
    
    # Sort asks by price ascending (cheapest first)
    asks_sorted = sorted(asks, key=lambda x: float(x.get("price", 1)))
    
    remaining_size = position_size  # Number of shares to buy
    total_cost = 0.0
    best_ask_price = float(asks_sorted[0].get("price", 1)) if asks_sorted else None
    depth_at_best = 0.0
    
    for ask in asks_sorted:
        price = float(ask.get("price", 1))
        size = float(ask.get("size", 0))
        
        if remaining_size <= 0:
            break
        
        fill_size = min(remaining_size, size)
        total_cost += fill_size * price
        remaining_size -= fill_size
        
        if price == best_ask_price:
            depth_at_best += size
    
    if remaining_size > 0:
        # Can't fill the full position
        return None
    
    avg_fill_price = total_cost / position_size
    theoretical_cost = 1 - market["yes_price"]
    slippage = (avg_fill_price - theoretical_cost) / theoretical_cost if theoretical_cost > 0 else 0
    
    return {
        "market_id": market["id"],
        "question": market["question"],
        "yes_price": market["yes_price"],
        "theoretical_no_cost": theoretical_cost,
        "no_best_ask": best_ask_price,
        "no_depth_at_best": depth_at_best,
        "avg_fill_price": avg_fill_price,
        "slippage_pct": slippage,
        "fill_cost_total": total_cost,
        "position_size": position_size,
        "no_token_id": no_token_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── Paper Position Tracking ───────────────────────────────────

def data_path(filename: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / filename


def load_positions() -> list[dict]:
    path = data_path("positions.json")
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return []


def save_positions(positions: list[dict]) -> None:
    path = data_path("positions.json")
    with open(path, "w") as f:
        json.dump(positions, f, indent=2)


def load_trades() -> list[dict]:
    path = data_path("trades.json")
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return []


def save_trades(trades: list[dict]) -> None:
    path = data_path("trades.json")
    with open(path, "w") as f:
        json.dump(trades, f, indent=2)


def open_position(fill: dict) -> dict:
    """Record a paper position entry."""
    position = {
        "id": fill["market_id"],
        "question": fill["question"],
        "entry_time": fill["timestamp"],
        "yes_price_at_entry": fill["yes_price"],
        "no_fill_price": fill["avg_fill_price"],
        "theoretical_no_cost": fill["theoretical_no_cost"],
        "slippage_pct": fill["slippage_pct"],
        "position_size": fill["position_size"],
        "entry_cost": fill["fill_cost_total"],
        "status": "open",
        "resolution": None,
        "pnl": None,
    }
    
    positions = load_positions()
    # Don't duplicate — skip if already tracking this market
    existing_ids = {p["id"] for p in positions}
    if position["id"] in existing_ids:
        log.info(f"  Already tracking {position['id'][:16]}... — skip")
        return position
    
    positions.append(position)
    save_positions(positions)
    
    # Log trade
    trades = load_trades()
    trades.append({
        "type": "ENTRY",
        "time": fill["timestamp"],
        "market_id": fill["market_id"],
        "question": fill["question"],
        "fill_price": fill["avg_fill_price"],
        "theoretical": fill["theoretical_no_cost"],
        "slippage": fill["slippage_pct"],
        "size": fill["position_size"],
        "cost": fill["fill_cost_total"],
    })
    save_trades(trades)
    
    return position


def check_resolutions() -> list[dict]:
    """Check open positions against Gamma API for resolution."""
    positions = load_positions()
    open_positions = [p for p in positions if p["status"] == "open"]
    
    if not open_positions:
        log.info("No open positions to check.")
        return []
    
    log.info(f"Checking resolution for {len(open_positions)} open positions...")
    resolved = []
    
    for pos in open_positions:
        try:
            url = f"{GAMMA_API}/markets/{pos['id']}"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            market = resp.json()
            
            outcome_prices = market.get("outcomePrices", "[]")
            if isinstance(outcome_prices, str):
                prices = json.loads(outcome_prices)
            else:
                prices = outcome_prices
            
            closed = market.get("closed", False)
            
            if not closed:
                continue
            
            # Determine resolution
            if prices and len(prices) >= 2:
                yes_final = float(prices[0])
                resolution = "YES" if yes_final > 0.5 else "NO"
            else:
                continue
            
            # Calculate P&L
            if resolution == "NO":
                # We win — get $1 per share minus fee minus entry cost
                payout = pos["position_size"] * (1 - FEE_RATE) - pos["entry_cost"]
            else:
                # We lose — lost entry cost
                payout = -pos["entry_cost"]
            
            pos["status"] = "resolved"
            pos["resolution"] = resolution
            pos["pnl"] = round(payout, 4)
            pos["resolved_time"] = datetime.now(timezone.utc).isoformat()
            
            resolved.append(pos)
            
            # Log trade
            trades = load_trades()
            trades.append({
                "type": "EXIT",
                "time": pos["resolved_time"],
                "market_id": pos["id"],
                "question": pos["question"],
                "resolution": resolution,
                "pnl": payout,
            })
            save_trades(trades)
            
        except Exception as e:
            log.warning(f"Resolution check failed for {pos['id'][:16]}...: {e}")
    
    if resolved:
        save_positions(positions)
    
    return resolved


# ── Main Scan Loop ─────────────────────────────────────────────

def scan_and_enter(dry_run: bool = True) -> list[dict]:
    """Scan for high-YES markets and record paper entries."""
    log.info("Scanning for markets with YES >= 95%...")
    
    markets = get_high_yes_markets()
    if not markets:
        log.info("No qualifying markets found.")
        return []
    
    # Sort by YES price descending (most extreme first)
    markets.sort(key=lambda m: m["yes_price"], reverse=True)
    
    log.info(f"Checking orderbooks for {len(markets)} markets...")
    entries = []
    
    positions = load_positions()
    existing_ids = {p["id"] for p in positions}
    
    # Daily limits
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_trades = [t for t in load_trades() if t.get("type") == "ENTRY" and t.get("time", "").startswith(today)]
    today_pnl = sum(p.get("pnl", 0) for p in positions if p.get("status") == "resolved" and p.get("resolved_time", "").startswith(today))
    
    for market in markets[:MAX_BOOK_CHECKS]:  # Cap orderbook checks
        # Skip if already tracking
        if market["id"] in existing_ids:
            continue
        
        # Daily limits
        if len(today_trades) >= MAX_DAILY_POSITIONS:
            log.info(f"Daily position limit reached ({MAX_DAILY_POSITIONS})")
            break
        if today_pnl <= -MAX_DAILY_LOSS:
            log.info(f"Daily loss limit reached (${today_pnl:.2f})")
            break
        
        # Get realistic fill
        fill = get_no_fill_price(market, POSITION_SIZE)
        if not fill:
            log.debug(f"  Can't fill: {market['question'][:50]}...")
            continue
        
        # Log the fill data regardless — this validates slippage model
        slippage_str = f"{fill['slippage_pct']:+.1%}"
        cost_str = f"{fill['avg_fill_price']:.3f}"
        theo_str = f"{fill['theoretical_no_cost']:.3f}"
        best_str = f"{fill['no_best_ask']:.3f}"
        
        log.info(f"  FILL: YES={fill['yes_price']:.2f} NO_ask={best_str} "
                 f"avg_fill={cost_str} theo={theo_str} slip={slippage_str} "
                 f"| {market['question'][:60]}")
        
        if dry_run:
            # Still record the fill observation for slippage validation
            entries.append(fill)
        else:
            pos = open_position(fill)
            entries.append(fill)
            today_trades.append({"type": "ENTRY"})
    
    return entries


def print_status() -> None:
    """Display current paper trading status."""
    positions = load_positions()
    open_pos = [p for p in positions if p["status"] == "open"]
    resolved_pos = [p for p in positions if p["status"] == "resolved"]
    
    print(f"\n{'=' * 70}")
    print("SHADOW TRADER STATUS")
    print(f"{'=' * 70}")
    print(f"Open positions:     {len(open_pos)}")
    print(f"Resolved positions: {len(resolved_pos)}")
    
    if resolved_pos:
        total_pnl = sum(p["pnl"] for p in resolved_pos)
        wins = sum(1 for p in resolved_pos if p["pnl"] > 0)
        wr = wins / len(resolved_pos) if resolved_pos else 0
        print(f"Total P&L:          ${total_pnl:.2f}")
        print(f"Win rate:           {wr:.1%}")
    
    # Slippage stats from all fills observed
    trades = load_trades()
    fills = [t for t in trades if t.get("type") == "ENTRY"]
    if fills:
        slips = [t["slippage"] for t in fills]
        avg_slip = sum(slips) / len(slips)
        neg_slips = [s for s in slips if s < 0]
        pos_slips = [s for s in slips if s >= 0]
        print(f"\nSlippage observations: {len(fills)}")
        print(f"  Avg slippage:    {avg_slip:+.1%}")
        print(f"  Negative (good): {len(neg_slips)} ({len(neg_slips)/len(fills):.0%})")
        print(f"  Positive (bad):  {len(pos_slips)} ({len(pos_slips)/len(fills):.0%})")
        if neg_slips:
            print(f"  Best fill:       {min(slips):+.1%}")
        if pos_slips:
            print(f"  Worst fill:      {max(slips):+.1%}")
    
    if open_pos:
        print(f"\nOpen positions:")
        for p in open_pos[:10]:
            print(f"  {p['yes_price_at_entry']:.2f} YES | fill={p['no_fill_price']:.3f} "
                  f"slip={p['slippage_pct']:+.1%} | {p['question'][:50]}")


def print_pnl() -> None:
    """Detailed P&L report on resolved positions."""
    positions = load_positions()
    resolved = [p for p in positions if p["status"] == "resolved"]
    
    if not resolved:
        print("No resolved positions yet.")
        return
    
    print(f"\n{'=' * 70}")
    print("SHADOW TRADER P&L")
    print(f"{'=' * 70}")
    
    total_pnl = 0
    total_wins = 0
    
    print(f"{'Market':<50} {'Res':>4} {'Fill':>6} {'Theo':>6} {'Slip':>7} {'P&L':>8}")
    print("-" * 82)
    
    for p in sorted(resolved, key=lambda x: x.get("resolved_time", ""), reverse=True)[:30]:
        pnl = p.get("pnl", 0)
        total_pnl += pnl
        if pnl > 0:
            total_wins += 1
        
        print(f"{p['question'][:50]:<50} {p['resolution']:>4} "
              f"{p['no_fill_price']:>.3f} {p['theoretical_no_cost']:>.3f} "
              f"{p['slippage_pct']:>+6.1%} ${pnl:>7.2f}")
    
    wr = total_wins / len(resolved) if resolved else 0
    print("-" * 82)
    print(f"{'TOTAL':<50} {'':>4} {'':>6} {'':>6} {'':>7} ${total_pnl:>7.2f}")
    print(f"Win rate: {wr:.1%} | Positions: {len(resolved)}")


def save_fill_observations(fills: list[dict]) -> None:
    """Append fill observations to a running log for slippage validation."""
    path = data_path("fill_observations.jsonl")
    with open(path, "a") as f:
        for fill in fills:
            f.write(json.dumps(fill) + "\n")


# ── Main ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Polymarket Shadow Trader (Paper Trading)")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=120, help="Seconds between scans (default: 120)")
    parser.add_argument("--status", action="store_true", help="Show current status")
    parser.add_argument("--pnl", action="store_true", help="Show P&L report")
    parser.add_argument("--live", action="store_true", help="Actually record positions (default: dry-run/observe only)")
    parser.add_argument("--position-size", type=float, default=POSITION_SIZE, help="Paper position size in dollars")
    parser.add_argument("--yes-min", type=float, default=YES_MIN, help="Minimum YES price to enter")
    args = parser.parse_args()
    
    if args.status:
        print_status()
        return
    
    if args.pnl:
        print_pnl()
        return
    
    # Override config from args
    position_size = args.position_size
    yes_min = args.yes_min
    
    dry_run = not args.live
    
    print(f"Shadow Trader — {'DRY RUN (observe only)' if dry_run else 'LIVE (recording positions)'}")
    print(f"  YES min: {yes_min:.0%}")
    print(f"  Position size: ${position_size}")
    print(f"  Interval: {args.interval}s")
    
    if args.loop:
        scan_count = 0
        while True:
            scan_count += 1
            print(f"\n{'─' * 70}")
            print(f"Scan #{scan_count} — {datetime.now().strftime('%H:%M:%S')}")
            
            try:
                fills = scan_and_enter(dry_run=dry_run)
                if fills:
                    save_fill_observations(fills)
                    print(f"  {len(fills)} fill observations recorded")
                
                # Check resolutions on existing positions
                if not dry_run:
                    resolved = check_resolutions()
                    if resolved:
                        for r in resolved:
                            print(f"  RESOLVED: {r['resolution']} P&L=${r['pnl']:.2f} | {r['question'][:50]}")
                
            except Exception as e:
                log.error(f"Scan error: {e}")
            
            time.sleep(args.interval)
    else:
        # Single scan
        fills = scan_and_enter(dry_run=dry_run)
        if fills:
            save_fill_observations(fills)
            print(f"\n{len(fills)} fill observations saved to {data_path('fill_observations.jsonl')}")
            print("Run with --pnl to see results as positions resolve.")
        else:
            print("No qualifying fills found this scan.")


if __name__ == "__main__":
    main()
