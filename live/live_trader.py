"""
Polymarket Live Trader — Real Order Execution

Runs on VPS in non-blocked jurisdiction.
Uses py-clob-client for authenticated CLOB API access.
Implements the BUY_NO at YES>=95¢ strategy with real money.

Usage:
    source venv/bin/activate
    python live/live_trader.py --dry-run          # Observe only (no real orders)
    python live/live_trader.py --position-size 1  # Live with $1 positions
    python live/live_trader.py --status           # Show tracked positions
    python live/live_trader.py --pnl              # Show P&L
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
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

# ── Configuration ──────────────────────────────────────────────

GAMMA_API = "https://gamma-api.polymarket.com"
FEE_RATE = 0.02
YES_MIN = 0.95
YES_MAX = 0.99
POSITION_SIZE = 1.0
MAX_DAILY_POSITIONS = 20
MAX_DAILY_LOSS = 10.0
MIN_LIQUIDITY = 5000.0
MAX_BOOK_CHECKS = 15
MAX_CONCURRENT_OPEN = 50
DATA_DIR = Path("live/shadow_data")

# ── Logging ────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("live/shadow_data/trader.log"),
    ],
)
log = logging.getLogger("trader")


# ── CLOB Client Setup ─────────────────────────────────────────

def init_clob_client() -> ClobClient:
    """Initialize authenticated CLOB client from .env."""
    load_dotenv()
    
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
    host = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
    chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
    sig_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))
    funder = os.getenv("POLYMARKET_FUNDER")
    
    if not private_key or private_key == "PASTE_YOUR_PRIVATE_KEY_HERE":
        log.error("POLYMARKET_PRIVATE_KEY not set in .env")
        sys.exit(1)
    
    log.info(f"Connecting to CLOB at {host} (chain={chain_id}, sig_type={sig_type})...")
    
    client = ClobClient(
        host,
        key=private_key,
        chain_id=chain_id,
        signature_type=sig_type,
        funder=funder,
    )
    
    # Derive or create API credentials
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    
    # Verify connection
    ok = client.get_ok()
    log.info(f"CLOB connection: {ok}")
    
    # Check geoblock
    geo = requests.get("https://polymarket.com/api/geoblock", timeout=10).json()
    if geo.get("blocked"):
        log.error(f"GEOBLOCKED in {geo.get('country')}! Cannot trade from this IP.")
        sys.exit(1)
    log.info(f"Geoblock check: OK (IP in {geo.get('country')})")
    
    return client


# ── Market Discovery ──────────────────────────────────────────

def get_active_markets(min_volume: float = MIN_LIQUIDITY) -> list[dict]:
    """Fetch active markets from Gamma API."""
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
                    "condition_id": m.get("conditionId", ""),
                    "outcome_prices": prices,
                })
            except Exception:
                continue
        
        offset += limit
        if len(batch) < limit or offset >= 500:
            break
    
    return markets


def get_high_yes_markets() -> list[dict]:
    """Filter to 95-99¢ YES markets — our entry zone."""
    all_markets = get_active_markets()
    high_yes = [m for m in all_markets if YES_MIN <= m["yes_price"] < YES_MAX]
    log.info(f"Found {len(high_yes)} markets with {YES_MIN:.0%} <= YES < {YES_MAX:.0%} (of {len(all_markets)} active)")
    return high_yes


# ── Position Tracking ─────────────────────────────────────────

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


# ── Order Execution ────────────────────────────────────────────

def place_buy_no_order(client: ClobClient, market: dict, position_size: float) -> dict | None:
    """Place a real BUY NO market order via CLOB API."""
    no_token_id = market.get("no_token_id")
    if not no_token_id:
        log.warning(f"No token ID for market {market['id']}")
        return None
    
    try:
        # Market order: buy `position_size` worth of NO shares
        mo = MarketOrderArgs(
            token_id=no_token_id,
            amount=position_size,
            side=BUY,
            order_type=OrderType.FOK,  # Fill or Kill — don't leave partial orders
        )
        signed_order = client.create_market_order(mo)
        resp = client.post_order(signed_order, OrderType.FOK)
        
        log.info(f"  ORDER PLACED: {resp}")
        return resp
        
    except Exception as e:
        log.error(f"  Order failed: {e}")
        return None


def scan_and_trade(client: ClobClient, position_size: float, dry_run: bool = True) -> list[dict]:
    """Scan for qualifying markets and place orders."""
    log.info("Scanning for markets with 95-99% YES...")
    
    markets = get_high_yes_markets()
    if not markets:
        log.info("No qualifying markets found.")
        return []
    
    markets.sort(key=lambda m: m["yes_price"], reverse=True)
    
    positions = load_positions()
    existing_ids = {p["id"] for p in positions}
    open_count = sum(1 for p in positions if p["status"] == "open")
    
    # Daily limits
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_entries = [t for t in load_trades() if t.get("type") == "ENTRY" and t.get("time", "").startswith(today)]
    today_pnl = sum(p.get("pnl", 0) for p in positions if p.get("status") == "resolved" and p.get("resolved_time", "").startswith(today))
    
    entries = []
    
    for market in markets[:MAX_BOOK_CHECKS]:
        # Skip already tracked
        if market["id"] in existing_ids:
            continue
        
        # Limit checks
        if len(today_entries) >= MAX_DAILY_POSITIONS:
            log.info(f"Daily position limit reached ({MAX_DAILY_POSITIONS})")
            break
        if today_pnl <= -MAX_DAILY_LOSS:
            log.info(f"Daily loss limit reached (${today_pnl:.2f})")
            break
        if open_count >= MAX_CONCURRENT_OPEN:
            log.info(f"Max concurrent positions reached ({MAX_CONCURRENT_OPEN})")
            break
        
        if dry_run:
            # Just log what we would do
            log.info(f"  [DRY RUN] Would BUY NO @ YES={market['yes_price']:.2f} | {market['question'][:60]}")
            entries.append({"market_id": market["id"], "dry_run": True, "yes_price": market["yes_price"]})
        else:
            # Place real order
            log.info(f"  BUYING NO @ YES={market['yes_price']:.2f} size=${position_size} | {market['question'][:60]}")
            result = place_buy_no_order(client, market, position_size)
            
            if result:
                # Record position
                position = {
                    "id": market["id"],
                    "question": market["question"],
                    "entry_time": datetime.now(timezone.utc).isoformat(),
                    "yes_price_at_entry": market["yes_price"],
                    "position_size": position_size,
                    "no_token_id": market["no_token_id"],
                    "order_result": result,
                    "status": "open",
                    "resolution": None,
                    "pnl": None,
                }
                positions.append(position)
                save_positions(positions)
                
                trades = load_trades()
                trades.append({
                    "type": "ENTRY",
                    "time": position["entry_time"],
                    "market_id": market["id"],
                    "question": market["question"],
                    "yes_price": market["yes_price"],
                    "size": position_size,
                    "order_result": result,
                })
                save_trades(trades)
                
                entries.append(position)
                open_count += 1
    
    return entries


def check_resolutions(client: ClobClient) -> list[dict]:
    """Check open positions for resolution and calculate P&L."""
    positions = load_positions()
    open_positions = [p for p in positions if p["status"] == "open"]
    
    if not open_positions:
        return []
    
    log.info(f"Checking {len(open_positions)} open positions for resolution...")
    resolved = []
    
    for pos in open_positions:
        try:
            url = f"{GAMMA_API}/markets/{pos['id']}"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            market = resp.json()
            
            if not market.get("closed", False):
                continue
            
            outcome_prices = market.get("outcomePrices", "[]")
            if isinstance(outcome_prices, str):
                prices = json.loads(outcome_prices)
            else:
                prices = outcome_prices
            
            if not prices or len(prices) < 2:
                continue
            
            yes_final = float(prices[0])
            resolution = "YES" if yes_final > 0.5 else "NO"
            
            if resolution == "NO":
                payout = pos["position_size"] * (1 - FEE_RATE) - pos["position_size"] * (1 - pos["yes_price_at_entry"])
            else:
                payout = -(pos["position_size"] * (1 - pos["yes_price_at_entry"]))
            
            pos["status"] = "resolved"
            pos["resolution"] = resolution
            pos["pnl"] = round(payout, 4)
            pos["resolved_time"] = datetime.now(timezone.utc).isoformat()
            
            resolved.append(pos)
            
            trades = load_trades()
            trades.append({
                "type": "EXIT",
                "time": pos["resolved_time"],
                "market_id": pos["id"],
                "resolution": resolution,
                "pnl": payout,
            })
            save_trades(trades)
            
            log.info(f"  RESOLVED: {resolution} P&L=${pnl:.4f} | {pos['question'][:50]}")
            
        except Exception as e:
            log.warning(f"Resolution check failed for {pos['id'][:16]}: {e}")
    
    if resolved:
        save_positions(positions)
    
    return resolved


# ── Status & P&L ──────────────────────────────────────────────

def print_status() -> None:
    positions = load_positions()
    open_pos = [p for p in positions if p["status"] == "open"]
    resolved_pos = [p for p in positions if p["status"] == "resolved"]
    
    print(f"\n{'=' * 70}")
    print("LIVE TRADER STATUS")
    print(f"{'=' * 70}")
    print(f"Open positions:     {len(open_pos)}")
    print(f"Resolved positions: {len(resolved_pos)}")
    
    if resolved_pos:
        total_pnl = sum(p["pnl"] for p in resolved_pos)
        wins = sum(1 for p in resolved_pos if p["pnl"] > 0)
        wr = wins / len(resolved_pos)
        print(f"Total P&L:          ${total_pnl:.2f}")
        print(f"Win rate:           {wr:.1%}")


def print_pnl() -> None:
    positions = load_positions()
    resolved = [p for p in positions if p["status"] == "resolved"]
    
    if not resolved:
        print("No resolved positions yet.")
        return
    
    print(f"\n{'=' * 70}")
    print("LIVE TRADER P&L")
    print(f"{'=' * 70}")
    
    total_pnl = sum(p["pnl"] for p in resolved)
    wins = sum(1 for p in resolved if p["pnl"] > 0)
    
    for p in sorted(resolved, key=lambda x: x.get("resolved_time", ""), reverse=True)[:30]:
        pnl = p.get("pnl", 0)
        print(f"  {p['resolution']:>3} YES={p['yes_price_at_entry']:.2f} ${pnl:>7.4f} | {p['question'][:50]}")
    
    wr = wins / len(resolved)
    print(f"\nTotal: ${total_pnl:.2f} | WR: {wr:.1%} | Positions: {len(resolved)}")


# ── Main ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Polymarket Live Trader")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Observe only, no real orders")
    parser.add_argument("--live", action="store_true", help="Enable real order placement")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between scans")
    parser.add_argument("--position-size", type=float, default=1.0, help="Position size in dollars")
    parser.add_argument("--status", action="store_true", help="Show status")
    parser.add_argument("--pnl", action="store_true", help="Show P&L")
    args = parser.parse_args()
    
    if args.status:
        print_status()
        return
    
    if args.pnl:
        print_pnl()
        return
    
    dry_run = not args.live
    
    # Initialize CLOB client (authenticates with private key)
    client = init_clob_client()
    
    print(f"\nLive Trader — {'DRY RUN' if dry_run else '*** LIVE TRADING ***'}")
    print(f"  Position size: ${args.position_size}")
    print(f"  Strategy: BUY_NO at YES >= {YES_MIN:.0%}")
    print(f"  Daily limits: {MAX_DAILY_POSITIONS} positions, ${MAX_DAILY_LOSS} loss")
    
    if not dry_run:
        print(f"\n  ⚠ REAL MONEY AT RISK ⚠")
        confirm = input("  Type 'yes' to confirm: ")
        if confirm.lower() != "yes":
            print("Aborted.")
            return
    
    if args.loop:
        scan_count = 0
        while True:
            scan_count += 1
            print(f"\n{'─' * 70}")
            print(f"Scan #{scan_count} — {datetime.now().strftime('%H:%M:%S')}")
            
            try:
                scan_and_trade(client, args.position_size, dry_run=dry_run)
                check_resolutions(client)
            except Exception as e:
                log.error(f"Scan error: {e}")
            
            time.sleep(args.interval)
    else:
        scan_and_trade(client, args.position_size, dry_run=dry_run)
        check_resolutions(client)


if __name__ == "__main__":
    main()
