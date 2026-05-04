"""
Market data client for Polymarket public APIs.

All endpoints are read-only — no authentication required.
Rate limits are generous (4K-9K req/10sec).
"""

import json
import time
from typing import Optional

import requests


class PolymarketClient:
    """Read-only client for Polymarket Gamma, CLOB, and Data APIs."""

    GAMMA_BASE = "https://gamma-api.polymarket.com"
    CLOB_BASE = "https://clob.polymarket.com"
    DATA_BASE = "https://data-api.polymarket.com"

    def __init__(self, rate_limit_delay: float = 0.1):
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.delay = rate_limit_delay
        self._last_request = 0.0

    def _get(self, base: str, path: str, params: dict = None) -> dict | list:
        """Rate-limited GET request."""
        elapsed = time.time() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        
        resp = self.session.get(f"{base}{path}", params=params, timeout=30)
        resp.raise_for_status()
        self._last_request = time.time()
        return resp.json()

    # ── Gamma API ──────────────────────────────────────────────

    def search_markets(self, query: str, limit: int = 20) -> list:
        """Search for markets by keyword."""
        data = self._get(self.GAMMA_BASE, "/public-search", {"q": query})
        return data.get("events", [])

    def list_events(
        self,
        limit: int = 100,
        active: bool = True,
        closed: bool = False,
        order: str = "volume",
        ascending: bool = False,
        tag: str = None,
    ) -> list:
        """List events with optional filters."""
        params = {
            "limit": limit,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "order": order,
            "ascending": str(ascending).lower(),
        }
        if tag:
            params["tag"] = tag
        return self._get(self.GAMMA_BASE, "/events", params)

    def list_markets(
        self,
        limit: int = 100,
        active: bool = True,
        closed: bool = False,
    ) -> list:
        """List markets with optional filters."""
        return self._get(self.GAMMA_BASE, "/markets", {
            "limit": limit,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
        })

    # ── CLOB API ───────────────────────────────────────────────

    def get_price(self, token_id: str, side: str = "buy") -> float:
        """Get current price for a token."""
        data = self._get(self.CLOB_BASE, "/price", {
            "token_id": token_id,
            "side": side,
        })
        return float(data.get("price", 0))

    def get_midpoint(self, token_id: str) -> float:
        """Get midpoint price for a token."""
        data = self._get(self.CLOB_BASE, "/midpoint", {"token_id": token_id})
        return float(data.get("mid", 0))

    def get_spread(self, token_id: str) -> float:
        """Get bid-ask spread for a token."""
        data = self._get(self.CLOB_BASE, "/spread", {"token_id": token_id})
        return float(data.get("spread", 0))

    def get_orderbook(self, token_id: str) -> dict:
        """Get full orderbook for a token."""
        return self._get(self.CLOB_BASE, "/book", {"token_id": token_id})

    def get_price_history(
        self,
        condition_id: str,
        interval: str = "1m",
        fidelity: int = 100,
    ) -> list:
        """Get price history for a market."""
        data = self._get(self.CLOB_BASE, "/prices-history", {
            "market": condition_id,
            "interval": interval,
            "fidelity": fidelity,
        })
        return data.get("history", [])

    def list_clob_markets(self, limit: int = 100, cursor: str = None) -> dict:
        """List CLOB markets with pagination."""
        params = {"limit": limit}
        if cursor:
            params["next_cursor"] = cursor
        return self._get(self.CLOB_BASE, "/markets", params)

    # ── Data API ───────────────────────────────────────────────

    def get_trades(self, condition_id: str = None, limit: int = 100) -> list:
        """Get recent trades, optionally filtered by market."""
        params = {"limit": limit}
        if condition_id:
            params["market"] = condition_id
        return self._get(self.DATA_BASE, "/trades", params)

    def get_open_interest(self, condition_id: str) -> dict:
        """Get open interest for a market."""
        return self._get(self.DATA_BASE, "/oi", {"market": condition_id})

    # ── Helpers ────────────────────────────────────────────────

    @staticmethod
    def parse_market_prices(market: dict) -> dict:
        """Parse double-encoded price fields from a Gamma market."""
        result = {"question": market.get("question", ""), "id": market.get("id", "")}
        
        for field in ["outcomePrices", "outcomes", "clobTokenIds"]:
            raw = market.get(field, "[]")
            if isinstance(raw, str):
                result[field] = json.loads(raw)
            else:
                result[field] = raw
        
        result["volume"] = market.get("volume", 0)
        result["liquidity"] = market.get("liquidity", 0)
        result["conditionId"] = market.get("conditionId", "")
        
        return result
