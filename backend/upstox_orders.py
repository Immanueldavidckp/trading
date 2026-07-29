"""
Upstox v2 Order Execution Engine — place / modify / cancel orders + portfolio.

Supports two modes controlled by MOCK_MODE env var:
  MOCK_MODE=true  → paper trading against mock_portfolio.json (safe, default)
  MOCK_MODE=false → LIVE orders via Upstox API (real money!)

Upstox order placement docs:
  POST /v2/order/place   — place a new order
  GET  /v2/order/retrieve-all — list today's orders
  PUT  /v2/order/modify  — modify a pending order
  DELETE /v2/order/cancel — cancel a pending order
  GET  /v2/portfolio/short-term-positions — intraday + delivery positions
  GET  /v2/portfolio/long-term-holdings   — demat holdings
  GET  /v2/user/get-funds-and-margin      — available margin / balance
"""

import os
import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

IST = timezone(timedelta(hours=5, minutes=30))
MOCK_MODE = os.environ.get("MOCK_MODE", "true").strip().lower() in ("true", "1", "yes")
BASE = "https://api.upstox.com/v2"

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
MOCK_FILE = os.path.join(_BACKEND_DIR, "mock_portfolio.json")
TRADE_LOG = os.path.join(_BACKEND_DIR, "local_data", "trade_log.json")


def _now_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


# ── Mock portfolio helpers ───────────────────────────────────────────────────

def _load_mock() -> dict:
    try:
        with open(MOCK_FILE) as f:
            return json.load(f)
    except Exception:
        return {"orders": [], "positions": {}, "holdings": [], "balance": 1000000.0}


def _save_mock(data: dict):
    with open(MOCK_FILE, "w") as f:
        json.dump(data, f, indent=4)


def _append_trade_log(entry: dict):
    os.makedirs(os.path.dirname(TRADE_LOG), exist_ok=True)
    try:
        with open(TRADE_LOG) as f:
            log = json.load(f)
    except Exception:
        log = []
    log.append(entry)
    with open(TRADE_LOG, "w") as f:
        json.dump(log, f, indent=2)


# ── Live Upstox API helpers ──────────────────────────────────────────────────

def _headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ── Order Execution Engine ───────────────────────────────────────────────────

class OrderEngine:
    """Unified order engine that routes to mock or live Upstox depending on MOCK_MODE."""

    def __init__(self, upstox_client=None):
        self.upstox = upstox_client
        self.mock_mode = MOCK_MODE

    def _token(self) -> Optional[str]:
        if self.upstox and self.upstox.access_token:
            return self.upstox.access_token
        return None

    # ── Place Order ──────────────────────────────────────────────────────────

    def place_order(
        self,
        tsym: str,
        side: str,
        qty: int,
        order_type: str = "MARKET",
        price: float = 0.0,
        trigger_price: float = 0.0,
        product: str = "D",
        validity: str = "DAY",
        disclosed_qty: int = 0,
    ) -> dict:
        """
        Place a BUY or SELL order.

        tsym:          Stock symbol (e.g. 'RELIANCE', 'TCS')
        side:          'BUY' or 'SELL'
        qty:           Number of shares
        order_type:    'MARKET', 'LIMIT', 'SL', 'SL-M'
        price:         Limit price (for LIMIT/SL orders)
        trigger_price: Trigger price (for SL/SL-M orders)
        product:       'D' (delivery/CNC), 'I' (intraday/MIS), 'CO', 'OCO'
        validity:      'DAY', 'IOC'
        """
        side = side.upper()
        if side not in ("BUY", "SELL"):
            return {"ok": False, "error": f"Invalid side '{side}'. Use BUY or SELL."}

        order_type = order_type.upper()
        if order_type not in ("MARKET", "LIMIT", "SL", "SL-M"):
            return {"ok": False, "error": f"Invalid order_type '{order_type}'."}

        if qty <= 0:
            return {"ok": False, "error": "Quantity must be > 0."}

        if self.mock_mode:
            return self._mock_place(tsym, side, qty, order_type, price, product)

        return self._live_place(tsym, side, qty, order_type, price,
                                trigger_price, product, validity, disclosed_qty)

    @staticmethod
    def _is_fo(tsym: str) -> bool:
        """Detect if a symbol is an F&O instrument (futures/options)."""
        t = tsym.upper()
        return t.endswith("FUT") or t.endswith("CE") or t.endswith("PE")

    def _mock_place(self, tsym: str, side: str, qty: int,
                    order_type: str, price: float, product: str) -> dict:
        """Execute order against mock portfolio."""
        portfolio = _load_mock()
        tsym_upper = tsym.strip().upper()
        is_fo = self._is_fo(tsym_upper)

        exec_price = price if (order_type == "LIMIT" and price > 0) else self._get_ltp(tsym_upper)
        if exec_price is None or exec_price <= 0:
            exec_price = price if price > 0 else 100.0

        order_id = f"MOCK-{uuid.uuid4().hex[:8].upper()}"
        total_value = exec_price * qty
        pnl = None

        if is_fo:
            # F&O: track positions, not holdings. Short-selling is allowed.
            fo_positions = portfolio.setdefault("fo_positions", [])
            margin_pct = 0.15 if side == "SELL" else 1.0
            margin_needed = exec_price * qty * margin_pct

            if side == "BUY":
                if total_value > portfolio.get("balance", 0):
                    return {"ok": False, "error": f"Insufficient balance. Need Rs.{total_value:.2f}, "
                            f"have Rs.{portfolio['balance']:.2f}"}
                portfolio["balance"] -= total_value

            elif side == "SELL":
                if margin_needed > portfolio.get("balance", 0):
                    return {"ok": False, "error": f"Insufficient margin. Need Rs.{margin_needed:.2f}, "
                            f"have Rs.{portfolio['balance']:.2f}"}
                portfolio["balance"] += total_value  # credit received
                margin_needed = exec_price * qty * 0.15

            existing_pos = None
            for p in fo_positions:
                if p["tsym"] == tsym_upper:
                    existing_pos = p
                    break

            if existing_pos:
                if existing_pos["side"] == side:
                    old_val = existing_pos["avgprc"] * existing_pos["qty"]
                    new_val = old_val + total_value
                    existing_pos["qty"] += qty
                    existing_pos["avgprc"] = round(new_val / existing_pos["qty"], 2)
                    existing_pos["margin_used"] = round(
                        existing_pos["avgprc"] * existing_pos["qty"] * margin_pct, 2)
                else:
                    if qty >= existing_pos["qty"]:
                        pnl = round((exec_price - existing_pos["avgprc"]) * existing_pos["qty"], 2)
                        if existing_pos["side"] == "SELL":
                            pnl = -pnl
                        remaining = qty - existing_pos["qty"]
                        portfolio["fo_positions"] = [p for p in fo_positions if p is not existing_pos]
                        if remaining > 0:
                            portfolio["fo_positions"].append({
                                "tsym": tsym_upper, "side": side, "qty": remaining,
                                "avgprc": exec_price, "product": product,
                                "margin_used": round(exec_price * remaining * margin_pct, 2),
                                "opened_at": _now_ist(),
                            })
                    else:
                        pnl = round((exec_price - existing_pos["avgprc"]) * qty, 2)
                        if existing_pos["side"] == "SELL":
                            pnl = -pnl
                        existing_pos["qty"] -= qty
                        existing_pos["margin_used"] = round(
                            existing_pos["avgprc"] * existing_pos["qty"] * margin_pct, 2)
            else:
                fo_positions.append({
                    "tsym": tsym_upper, "side": side, "qty": qty,
                    "avgprc": exec_price, "product": product,
                    "margin_used": round(margin_needed, 2),
                    "opened_at": _now_ist(),
                })

        else:
            # Equity: existing logic
            if side == "BUY":
                if total_value > portfolio.get("balance", 0):
                    return {"ok": False, "error": f"Insufficient balance. Need Rs.{total_value:.2f}, "
                            f"have Rs.{portfolio['balance']:.2f}"}
                portfolio["balance"] -= total_value
                existing = None
                for h in portfolio.get("holdings", []):
                    if h["tsym"] == tsym_upper or h["tsym"] == tsym_upper + "-EQ":
                        existing = h
                        break
                if existing:
                    old_val = existing["avgprc"] * existing["qty"]
                    new_val = old_val + total_value
                    existing["qty"] += qty
                    existing["avgprc"] = round(new_val / existing["qty"], 2)
                else:
                    portfolio["holdings"].append({
                        "exch": "NSE", "token": "", "tsym": tsym_upper,
                        "qty": qty, "avgprc": exec_price, "cname": tsym_upper,
                    })

            elif side == "SELL":
                holding = None
                for h in portfolio.get("holdings", []):
                    if h["tsym"] == tsym_upper or h["tsym"] == tsym_upper + "-EQ":
                        holding = h
                        break
                if not holding or holding["qty"] < qty:
                    avail = holding["qty"] if holding else 0
                    return {"ok": False, "error": f"Insufficient holdings. Have {avail} of {tsym_upper}, need {qty}."}
                portfolio["balance"] += total_value
                pnl = round((exec_price - holding["avgprc"]) * qty, 2)
                holding["qty"] -= qty
                if holding["qty"] == 0:
                    portfolio["holdings"] = [h for h in portfolio["holdings"]
                                             if h is not holding]

        order_entry = {
            "order_id": order_id,
            "tsym": tsym_upper,
            "side": side,
            "qty": qty,
            "order_type": order_type,
            "price": exec_price,
            "product": product,
            "segment": "NFO" if is_fo else "NSE",
            "status": "COMPLETE",
            "placed_at": _now_ist(),
            "mode": "MOCK",
        }
        portfolio.setdefault("orders", []).append(order_entry)
        _save_mock(portfolio)

        log_entry = {**order_entry, "balance_after": portfolio["balance"]}
        if pnl is not None:
            log_entry["pnl"] = pnl
        _append_trade_log(log_entry)

        result = {
            "ok": True,
            "mode": "MOCK",
            "order_id": order_id,
            "tsym": tsym_upper,
            "side": side,
            "qty": qty,
            "exec_price": exec_price,
            "total_value": round(total_value, 2),
            "balance_after": round(portfolio["balance"], 2),
            "segment": "NFO" if is_fo else "NSE",
            "status": "COMPLETE",
            "placed_at": order_entry["placed_at"],
        }
        if pnl is not None:
            result["pnl"] = pnl
        return result

    def _live_place(self, tsym: str, side: str, qty: int,
                    order_type: str, price: float, trigger_price: float,
                    product: str, validity: str, disclosed_qty: int) -> dict:
        """Place a real order via Upstox API v2."""
        token = self._token()
        if not token:
            return {"ok": False, "error": "Not logged in to Upstox. Visit /api/upstox/login_url first."}

        instrument_key = self.upstox.instrument_key(tsym) if self.upstox else None
        if not instrument_key:
            return {"ok": False, "error": f"Symbol '{tsym}' not found in instrument master."}

        product_map = {"D": "D", "I": "I", "CO": "CO", "OCO": "OCO"}
        body = {
            "quantity": qty,
            "product": product_map.get(product, "D"),
            "validity": validity,
            "price": price if order_type in ("LIMIT", "SL") else 0,
            "tag": "trading_agent",
            "instrument_token": instrument_key,
            "order_type": order_type,
            "transaction_type": side,
            "disclosed_quantity": disclosed_qty,
            "trigger_price": trigger_price if order_type in ("SL", "SL-M") else 0,
            "is_amo": False,
        }

        try:
            r = requests.post(f"{BASE}/order/place",
                              headers=_headers(token), json=body, timeout=15)
            d = r.json()
            if d.get("status") == "success":
                oid = d.get("data", {}).get("order_id", "")
                result = {
                    "ok": True, "mode": "LIVE", "order_id": oid,
                    "tsym": tsym.upper(), "side": side, "qty": qty,
                    "order_type": order_type, "price": price,
                    "product": product, "status": "PLACED",
                    "placed_at": _now_ist(),
                }
                _append_trade_log(result)
                return result
            return {"ok": False, "error": str(d.get("errors", d))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Cancel Order ─────────────────────────────────────────────────────────

    def cancel_order(self, order_id: str) -> dict:
        if self.mock_mode:
            portfolio = _load_mock()
            for o in portfolio.get("orders", []):
                if o["order_id"] == order_id and o["status"] in ("PENDING", "OPEN"):
                    o["status"] = "CANCELLED"
                    _save_mock(portfolio)
                    return {"ok": True, "order_id": order_id, "status": "CANCELLED", "mode": "MOCK"}
            return {"ok": False, "error": f"Order {order_id} not found or already complete."}

        token = self._token()
        if not token:
            return {"ok": False, "error": "Not logged in."}
        try:
            r = requests.delete(f"{BASE}/order/cancel",
                                headers=_headers(token),
                                params={"order_id": order_id}, timeout=15)
            d = r.json()
            if d.get("status") == "success":
                return {"ok": True, "order_id": order_id, "status": "CANCELLED", "mode": "LIVE"}
            return {"ok": False, "error": str(d.get("errors", d))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Get Orders ───────────────────────────────────────────────────────────

    def get_orders(self) -> dict:
        if self.mock_mode:
            portfolio = _load_mock()
            return {"ok": True, "mode": "MOCK", "orders": portfolio.get("orders", [])}

        token = self._token()
        if not token:
            return {"ok": False, "error": "Not logged in."}
        try:
            r = requests.get(f"{BASE}/order/retrieve-all",
                             headers=_headers(token), timeout=15)
            d = r.json()
            if d.get("status") == "success":
                return {"ok": True, "mode": "LIVE", "orders": d.get("data", [])}
            return {"ok": False, "error": str(d.get("errors", d))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Portfolio: Positions ─────────────────────────────────────────────────

    def get_positions(self) -> dict:
        if self.mock_mode:
            portfolio = _load_mock()
            return {"ok": True, "mode": "MOCK",
                    "positions": portfolio.get("positions", {})}

        token = self._token()
        if not token:
            return {"ok": False, "error": "Not logged in."}
        try:
            r = requests.get(f"{BASE}/portfolio/short-term-positions",
                             headers=_headers(token), timeout=15)
            d = r.json()
            if d.get("status") == "success":
                return {"ok": True, "mode": "LIVE", "positions": d.get("data", [])}
            return {"ok": False, "error": str(d.get("errors", d))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Portfolio: Holdings ──────────────────────────────────────────────────

    def get_holdings(self) -> dict:
        if self.mock_mode:
            portfolio = _load_mock()
            return {"ok": True, "mode": "MOCK",
                    "holdings": portfolio.get("holdings", []),
                    "balance": portfolio.get("balance", 0)}

        token = self._token()
        if not token:
            return {"ok": False, "error": "Not logged in."}
        try:
            r = requests.get(f"{BASE}/portfolio/long-term-holdings",
                             headers=_headers(token), timeout=15)
            d = r.json()
            if d.get("status") == "success":
                return {"ok": True, "mode": "LIVE", "holdings": d.get("data", [])}
            return {"ok": False, "error": str(d.get("errors", d))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Funds / Margin ───────────────────────────────────────────────────────

    def get_funds(self) -> dict:
        if self.mock_mode:
            portfolio = _load_mock()
            bal = portfolio.get("balance", 0)
            holdings_val = sum(h["avgprc"] * h["qty"] for h in portfolio.get("holdings", []))
            return {
                "ok": True, "mode": "MOCK",
                "available_balance": round(bal, 2),
                "holdings_value": round(holdings_val, 2),
                "total_value": round(bal + holdings_val, 2),
            }

        token = self._token()
        if not token:
            return {"ok": False, "error": "Not logged in."}
        try:
            r = requests.get(f"{BASE}/user/get-funds-and-margin",
                             headers=_headers(token), timeout=15)
            d = r.json()
            if d.get("status") == "success":
                return {"ok": True, "mode": "LIVE", "funds": d.get("data", {})}
            return {"ok": False, "error": str(d.get("errors", d))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Trade Log ────────────────────────────────────────────────────────────

    @staticmethod
    def get_trade_log() -> dict:
        try:
            with open(TRADE_LOG) as f:
                log = json.load(f)
            return {"ok": True, "trades": log}
        except Exception:
            return {"ok": True, "trades": []}

    # ── F&O: Place multi-leg strategy order ────────────────────────────────

    def place_fo_strategy(self, strategy: dict) -> dict:
        """Execute all legs of an F&O strategy (from nifty_fo_engine).
        Each leg is placed as a separate order. Returns combined result."""
        legs = strategy.get("legs", [])
        if not legs:
            return {"ok": False, "error": "Strategy has no legs."}

        underlying = strategy.get("underlying", "NIFTY")
        expiry = strategy.get("expiry", "")
        lot_size = strategy.get("lot_size", 25)
        results = []
        all_ok = True

        for i, leg in enumerate(legs):
            side = leg["side"]
            qty = leg["qty"]
            price = leg.get("price", 0)
            strike = leg.get("strike", 0)
            option_type = leg.get("option_type", "")
            instrument = leg.get("instrument", "OPT")

            if instrument == "FUT":
                tsym = f"{underlying}FUT"
            else:
                tsym = f"{underlying}{int(strike)}{option_type}"

            res = self.place_order(
                tsym=tsym,
                side=side,
                qty=qty,
                order_type="LIMIT" if price > 0 else "MARKET",
                price=price,
                product="I",
            )
            res["leg"] = i + 1
            res["leg_desc"] = f"{side} {qty}x {tsym} @ Rs.{price}"
            results.append(res)
            if not res.get("ok"):
                all_ok = False

        return {
            "ok": all_ok,
            "strategy": strategy.get("strategy", "Unknown"),
            "underlying": underlying,
            "expiry": expiry,
            "legs_total": len(legs),
            "legs_filled": sum(1 for r in results if r.get("ok")),
            "leg_results": results,
            "margin_required": strategy.get("margin_required"),
            "max_profit": strategy.get("max_profit"),
            "max_loss": strategy.get("max_loss"),
            "placed_at": _now_ist(),
        }

    def get_fo_positions(self) -> dict:
        """Return F&O positions from the mock portfolio."""
        if self.mock_mode:
            portfolio = _load_mock()
            fo_positions = portfolio.get("fo_positions", [])
            margin_used = sum(p.get("margin_used", 0) for p in fo_positions)
            return {
                "ok": True, "mode": "MOCK",
                "fo_positions": fo_positions,
                "margin_used": round(margin_used, 2),
            }
        return self.get_positions()

    # ── Helper: get LTP from feed or stored data ─────────────────────────────

    def _get_ltp(self, tsym: str) -> Optional[float]:
        try:
            from upstox_feed import UpstoxQuoteFeed
            latest = UpstoxQuoteFeed.latest_prices()
            for sym, data in latest.items():
                if sym.upper().replace("-EQ", "") == tsym.upper().replace("-EQ", ""):
                    return data.get("lp") or data.get("ltp")
        except Exception:
            pass
        try:
            import db as _db
            conn = _db.connect()
            cur = conn.cursor()
            cur.execute(
                f"SELECT lp FROM price_changes WHERE tsym = {_db.PLACE} "
                f"AND lp IS NOT NULL ORDER BY id DESC LIMIT 1",
                [tsym]
            )
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row and row[0]:
                return float(row[0])
        except Exception:
            pass
        return None
