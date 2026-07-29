"""
Nifty Index Futures & Options — Strategy Engine.

Generates trade setups for NIFTY / BANKNIFTY index derivatives:
  - Directional: naked futures, buy calls/puts
  - Spreads: bull call spread, bear put spread
  - Neutral: iron condor, short straddle, short strangle
  - Hedged directional: long straddle, long strangle

Each strategy returns a structured plan with legs, max profit, max loss,
breakevens, margin requirement, lot size, and entry conditions.
"""

import math
from datetime import datetime, timedelta, timezone
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))

# ── Lot sizes (SEBI standard as of 2024) ─────────────────────────────────────
LOT_SIZES = {"NIFTY": 25, "BANKNIFTY": 15, "FINNIFTY": 25}

# ── Margin approximations (% of contract notional) ───────────────────────────
MARGIN_PCT = {
    "FUT_BUY": 0.12, "FUT_SELL": 0.12,
    "OPT_BUY": 1.0,       # full premium
    "OPT_SELL": 0.15,      # ~15% SPAN + exposure
    "SPREAD": 0.05,        # reduced margin for defined-risk spreads
}


def _round_strike(price: float, step: float = 50.0) -> float:
    return round(price / step) * step


def _now_ist():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


# ── Strategy builders ────────────────────────────────────────────────────────

def long_futures(underlying: str, spot: float, expiry: str,
                 lots: int = 1) -> dict:
    """Buy Nifty/BankNifty futures — directional bullish."""
    lot = LOT_SIZES.get(underlying, 25)
    qty = lot * lots
    notional = spot * qty
    margin = notional * MARGIN_PCT["FUT_BUY"]
    return {
        "strategy": "Long Futures",
        "underlying": underlying,
        "bias": "BULLISH",
        "expiry": expiry,
        "lots": lots,
        "lot_size": lot,
        "qty": qty,
        "legs": [
            {"side": "BUY", "instrument": "FUT", "strike": 0,
             "option_type": "", "qty": qty, "price": spot},
        ],
        "max_profit": "Unlimited",
        "max_loss": "Unlimited (use stop-loss)",
        "breakeven": round(spot, 2),
        "margin_required": round(margin, 2),
        "notional_value": round(notional, 2),
        "risk_type": "UNLIMITED",
        "generated_at": _now_ist(),
    }


def short_futures(underlying: str, spot: float, expiry: str,
                  lots: int = 1) -> dict:
    """Sell Nifty/BankNifty futures — directional bearish."""
    lot = LOT_SIZES.get(underlying, 25)
    qty = lot * lots
    notional = spot * qty
    margin = notional * MARGIN_PCT["FUT_SELL"]
    return {
        "strategy": "Short Futures",
        "underlying": underlying,
        "bias": "BEARISH",
        "expiry": expiry,
        "lots": lots,
        "lot_size": lot,
        "qty": qty,
        "legs": [
            {"side": "SELL", "instrument": "FUT", "strike": 0,
             "option_type": "", "qty": qty, "price": spot},
        ],
        "max_profit": "Unlimited",
        "max_loss": "Unlimited (use stop-loss)",
        "breakeven": round(spot, 2),
        "margin_required": round(margin, 2),
        "notional_value": round(notional, 2),
        "risk_type": "UNLIMITED",
        "generated_at": _now_ist(),
    }


def buy_call(underlying: str, spot: float, expiry: str,
             strike: float = 0, premium: float = 0,
             lots: int = 1) -> dict:
    """Buy a call option — directional bullish, limited risk."""
    lot = LOT_SIZES.get(underlying, 25)
    qty = lot * lots
    if strike == 0:
        strike = _round_strike(spot)
    cost = premium * qty
    return {
        "strategy": "Buy Call",
        "underlying": underlying,
        "bias": "BULLISH",
        "expiry": expiry,
        "lots": lots,
        "lot_size": lot,
        "qty": qty,
        "legs": [
            {"side": "BUY", "instrument": "OPT", "strike": strike,
             "option_type": "CE", "qty": qty, "price": premium},
        ],
        "max_profit": "Unlimited",
        "max_loss": round(cost, 2),
        "breakeven": round(strike + premium, 2),
        "margin_required": round(cost, 2),
        "risk_type": "DEFINED",
        "generated_at": _now_ist(),
    }


def buy_put(underlying: str, spot: float, expiry: str,
            strike: float = 0, premium: float = 0,
            lots: int = 1) -> dict:
    """Buy a put option — directional bearish, limited risk."""
    lot = LOT_SIZES.get(underlying, 25)
    qty = lot * lots
    if strike == 0:
        strike = _round_strike(spot)
    cost = premium * qty
    return {
        "strategy": "Buy Put",
        "underlying": underlying,
        "bias": "BEARISH",
        "expiry": expiry,
        "lots": lots,
        "lot_size": lot,
        "qty": qty,
        "legs": [
            {"side": "BUY", "instrument": "OPT", "strike": strike,
             "option_type": "PE", "qty": qty, "price": premium},
        ],
        "max_profit": round((strike - premium) * qty, 2),
        "max_loss": round(cost, 2),
        "breakeven": round(strike - premium, 2),
        "margin_required": round(cost, 2),
        "risk_type": "DEFINED",
        "generated_at": _now_ist(),
    }


def bull_call_spread(underlying: str, spot: float, expiry: str,
                     buy_strike: float = 0, sell_strike: float = 0,
                     buy_premium: float = 0, sell_premium: float = 0,
                     lots: int = 1) -> dict:
    """Bull Call Spread — buy lower CE, sell higher CE. Defined risk."""
    lot = LOT_SIZES.get(underlying, 25)
    qty = lot * lots
    step = 100 if underlying == "BANKNIFTY" else 50
    if buy_strike == 0:
        buy_strike = _round_strike(spot, step)
    if sell_strike == 0:
        sell_strike = buy_strike + step * 2
    net_debit = buy_premium - sell_premium
    width = sell_strike - buy_strike
    max_profit = (width - net_debit) * qty
    max_loss = net_debit * qty
    return {
        "strategy": "Bull Call Spread",
        "underlying": underlying,
        "bias": "BULLISH",
        "expiry": expiry,
        "lots": lots,
        "lot_size": lot,
        "qty": qty,
        "legs": [
            {"side": "BUY", "instrument": "OPT", "strike": buy_strike,
             "option_type": "CE", "qty": qty, "price": buy_premium},
            {"side": "SELL", "instrument": "OPT", "strike": sell_strike,
             "option_type": "CE", "qty": qty, "price": sell_premium},
        ],
        "net_debit": round(net_debit * qty, 2),
        "max_profit": round(max_profit, 2),
        "max_loss": round(max_loss, 2),
        "breakeven": round(buy_strike + net_debit, 2),
        "margin_required": round(max_loss, 2),
        "risk_type": "DEFINED",
        "generated_at": _now_ist(),
    }


def bear_put_spread(underlying: str, spot: float, expiry: str,
                    buy_strike: float = 0, sell_strike: float = 0,
                    buy_premium: float = 0, sell_premium: float = 0,
                    lots: int = 1) -> dict:
    """Bear Put Spread — buy higher PE, sell lower PE. Defined risk."""
    lot = LOT_SIZES.get(underlying, 25)
    qty = lot * lots
    step = 100 if underlying == "BANKNIFTY" else 50
    if buy_strike == 0:
        buy_strike = _round_strike(spot, step)
    if sell_strike == 0:
        sell_strike = buy_strike - step * 2
    net_debit = buy_premium - sell_premium
    width = buy_strike - sell_strike
    max_profit = (width - net_debit) * qty
    max_loss = net_debit * qty
    return {
        "strategy": "Bear Put Spread",
        "underlying": underlying,
        "bias": "BEARISH",
        "expiry": expiry,
        "lots": lots,
        "lot_size": lot,
        "qty": qty,
        "legs": [
            {"side": "BUY", "instrument": "OPT", "strike": buy_strike,
             "option_type": "PE", "qty": qty, "price": buy_premium},
            {"side": "SELL", "instrument": "OPT", "strike": sell_strike,
             "option_type": "PE", "qty": qty, "price": sell_premium},
        ],
        "net_debit": round(net_debit * qty, 2),
        "max_profit": round(max_profit, 2),
        "max_loss": round(max_loss, 2),
        "breakeven": round(buy_strike - net_debit, 2),
        "margin_required": round(max_loss, 2),
        "risk_type": "DEFINED",
        "generated_at": _now_ist(),
    }


def iron_condor(underlying: str, spot: float, expiry: str,
                call_sell: float = 0, call_buy: float = 0,
                put_sell: float = 0, put_buy: float = 0,
                call_sell_prem: float = 0, call_buy_prem: float = 0,
                put_sell_prem: float = 0, put_buy_prem: float = 0,
                lots: int = 1) -> dict:
    """Iron Condor — sell OTM call spread + sell OTM put spread. Neutral, defined risk."""
    lot = LOT_SIZES.get(underlying, 25)
    qty = lot * lots
    step = 100 if underlying == "BANKNIFTY" else 50
    if call_sell == 0:
        call_sell = _round_strike(spot + step * 3, step)
    if call_buy == 0:
        call_buy = call_sell + step * 2
    if put_sell == 0:
        put_sell = _round_strike(spot - step * 3, step)
    if put_buy == 0:
        put_buy = put_sell - step * 2

    net_credit = (call_sell_prem - call_buy_prem + put_sell_prem - put_buy_prem)
    call_width = call_buy - call_sell
    put_width = put_sell - put_buy
    max_width = max(call_width, put_width)
    max_loss = (max_width - net_credit) * qty
    max_profit = net_credit * qty
    be_upper = call_sell + net_credit
    be_lower = put_sell - net_credit

    return {
        "strategy": "Iron Condor",
        "underlying": underlying,
        "bias": "NEUTRAL",
        "expiry": expiry,
        "lots": lots,
        "lot_size": lot,
        "qty": qty,
        "legs": [
            {"side": "SELL", "instrument": "OPT", "strike": call_sell,
             "option_type": "CE", "qty": qty, "price": call_sell_prem},
            {"side": "BUY", "instrument": "OPT", "strike": call_buy,
             "option_type": "CE", "qty": qty, "price": call_buy_prem},
            {"side": "SELL", "instrument": "OPT", "strike": put_sell,
             "option_type": "PE", "qty": qty, "price": put_sell_prem},
            {"side": "BUY", "instrument": "OPT", "strike": put_buy,
             "option_type": "PE", "qty": qty, "price": put_buy_prem},
        ],
        "net_credit": round(net_credit * qty, 2),
        "max_profit": round(max_profit, 2),
        "max_loss": round(max_loss, 2),
        "breakeven_upper": round(be_upper, 2),
        "breakeven_lower": round(be_lower, 2),
        "profit_zone": f"{round(be_lower, 0)} – {round(be_upper, 0)}",
        "margin_required": round(max_loss + max_profit, 2),
        "risk_type": "DEFINED",
        "generated_at": _now_ist(),
    }


def long_straddle(underlying: str, spot: float, expiry: str,
                  strike: float = 0, call_prem: float = 0,
                  put_prem: float = 0, lots: int = 1) -> dict:
    """Long Straddle — buy ATM call + ATM put. Profit from big moves either way."""
    lot = LOT_SIZES.get(underlying, 25)
    qty = lot * lots
    step = 100 if underlying == "BANKNIFTY" else 50
    if strike == 0:
        strike = _round_strike(spot, step)
    total_prem = call_prem + put_prem
    cost = total_prem * qty
    return {
        "strategy": "Long Straddle",
        "underlying": underlying,
        "bias": "VOLATILE",
        "expiry": expiry,
        "lots": lots,
        "lot_size": lot,
        "qty": qty,
        "legs": [
            {"side": "BUY", "instrument": "OPT", "strike": strike,
             "option_type": "CE", "qty": qty, "price": call_prem},
            {"side": "BUY", "instrument": "OPT", "strike": strike,
             "option_type": "PE", "qty": qty, "price": put_prem},
        ],
        "max_profit": "Unlimited",
        "max_loss": round(cost, 2),
        "breakeven_upper": round(strike + total_prem, 2),
        "breakeven_lower": round(strike - total_prem, 2),
        "move_needed_pct": round(total_prem / spot * 100, 2),
        "margin_required": round(cost, 2),
        "risk_type": "DEFINED",
        "generated_at": _now_ist(),
    }


def short_straddle(underlying: str, spot: float, expiry: str,
                   strike: float = 0, call_prem: float = 0,
                   put_prem: float = 0, lots: int = 1) -> dict:
    """Short Straddle — sell ATM call + ATM put. Profit from low volatility / time decay."""
    lot = LOT_SIZES.get(underlying, 25)
    qty = lot * lots
    step = 100 if underlying == "BANKNIFTY" else 50
    if strike == 0:
        strike = _round_strike(spot, step)
    total_prem = call_prem + put_prem
    credit = total_prem * qty
    margin = spot * qty * MARGIN_PCT["OPT_SELL"]
    return {
        "strategy": "Short Straddle",
        "underlying": underlying,
        "bias": "NEUTRAL",
        "expiry": expiry,
        "lots": lots,
        "lot_size": lot,
        "qty": qty,
        "legs": [
            {"side": "SELL", "instrument": "OPT", "strike": strike,
             "option_type": "CE", "qty": qty, "price": call_prem},
            {"side": "SELL", "instrument": "OPT", "strike": strike,
             "option_type": "PE", "qty": qty, "price": put_prem},
        ],
        "net_credit": round(credit, 2),
        "max_profit": round(credit, 2),
        "max_loss": "Unlimited",
        "breakeven_upper": round(strike + total_prem, 2),
        "breakeven_lower": round(strike - total_prem, 2),
        "margin_required": round(margin, 2),
        "risk_type": "UNLIMITED",
        "generated_at": _now_ist(),
    }


def long_strangle(underlying: str, spot: float, expiry: str,
                  call_strike: float = 0, put_strike: float = 0,
                  call_prem: float = 0, put_prem: float = 0,
                  lots: int = 1) -> dict:
    """Long Strangle — buy OTM call + OTM put. Cheaper than straddle, needs bigger move."""
    lot = LOT_SIZES.get(underlying, 25)
    qty = lot * lots
    step = 100 if underlying == "BANKNIFTY" else 50
    if call_strike == 0:
        call_strike = _round_strike(spot + step * 2, step)
    if put_strike == 0:
        put_strike = _round_strike(spot - step * 2, step)
    total_prem = call_prem + put_prem
    cost = total_prem * qty
    return {
        "strategy": "Long Strangle",
        "underlying": underlying,
        "bias": "VOLATILE",
        "expiry": expiry,
        "lots": lots,
        "lot_size": lot,
        "qty": qty,
        "legs": [
            {"side": "BUY", "instrument": "OPT", "strike": call_strike,
             "option_type": "CE", "qty": qty, "price": call_prem},
            {"side": "BUY", "instrument": "OPT", "strike": put_strike,
             "option_type": "PE", "qty": qty, "price": put_prem},
        ],
        "max_profit": "Unlimited",
        "max_loss": round(cost, 2),
        "breakeven_upper": round(call_strike + total_prem, 2),
        "breakeven_lower": round(put_strike - total_prem, 2),
        "margin_required": round(cost, 2),
        "risk_type": "DEFINED",
        "generated_at": _now_ist(),
    }


def short_strangle(underlying: str, spot: float, expiry: str,
                   call_strike: float = 0, put_strike: float = 0,
                   call_prem: float = 0, put_prem: float = 0,
                   lots: int = 1) -> dict:
    """Short Strangle — sell OTM call + OTM put. Profit from range-bound market."""
    lot = LOT_SIZES.get(underlying, 25)
    qty = lot * lots
    step = 100 if underlying == "BANKNIFTY" else 50
    if call_strike == 0:
        call_strike = _round_strike(spot + step * 3, step)
    if put_strike == 0:
        put_strike = _round_strike(spot - step * 3, step)
    total_prem = call_prem + put_prem
    credit = total_prem * qty
    margin = spot * qty * MARGIN_PCT["OPT_SELL"]
    return {
        "strategy": "Short Strangle",
        "underlying": underlying,
        "bias": "NEUTRAL",
        "expiry": expiry,
        "lots": lots,
        "lot_size": lot,
        "qty": qty,
        "legs": [
            {"side": "SELL", "instrument": "OPT", "strike": call_strike,
             "option_type": "CE", "qty": qty, "price": call_prem},
            {"side": "SELL", "instrument": "OPT", "strike": put_strike,
             "option_type": "PE", "qty": qty, "price": put_prem},
        ],
        "net_credit": round(credit, 2),
        "max_profit": round(credit, 2),
        "max_loss": "Unlimited",
        "breakeven_upper": round(call_strike + total_prem, 2),
        "breakeven_lower": round(put_strike - total_prem, 2),
        "profit_zone": f"{round(put_strike - total_prem, 0)} – {round(call_strike + total_prem, 0)}",
        "margin_required": round(margin, 2),
        "risk_type": "UNLIMITED",
        "generated_at": _now_ist(),
    }


# ── Strategy recommender ─────────────────────────────────────────────────────

STRATEGIES = {
    "long_futures": long_futures,
    "short_futures": short_futures,
    "buy_call": buy_call,
    "buy_put": buy_put,
    "bull_call_spread": bull_call_spread,
    "bear_put_spread": bear_put_spread,
    "iron_condor": iron_condor,
    "long_straddle": long_straddle,
    "short_straddle": short_straddle,
    "long_strangle": long_strangle,
    "short_strangle": short_strangle,
}


def recommend_strategies(underlying: str, spot: float, expiry: str,
                         bias: str = "NEUTRAL", lots: int = 1) -> dict:
    """Given a market view (BULLISH/BEARISH/NEUTRAL/VOLATILE), recommend
    the best-fit strategies with pre-computed parameters."""
    bias = bias.upper()
    results = []

    if bias == "BULLISH":
        results.append(long_futures(underlying, spot, expiry, lots))
        atm = _round_strike(spot)
        results.append(buy_call(underlying, spot, expiry, atm, premium=spot * 0.015, lots=lots))
        results.append(bull_call_spread(underlying, spot, expiry,
                                        buy_premium=spot * 0.015,
                                        sell_premium=spot * 0.005, lots=lots))

    elif bias == "BEARISH":
        results.append(short_futures(underlying, spot, expiry, lots))
        atm = _round_strike(spot)
        results.append(buy_put(underlying, spot, expiry, atm, premium=spot * 0.015, lots=lots))
        results.append(bear_put_spread(underlying, spot, expiry,
                                       buy_premium=spot * 0.015,
                                       sell_premium=spot * 0.005, lots=lots))

    elif bias == "VOLATILE":
        atm = _round_strike(spot)
        results.append(long_straddle(underlying, spot, expiry, atm,
                                     call_prem=spot * 0.015, put_prem=spot * 0.015, lots=lots))
        results.append(long_strangle(underlying, spot, expiry,
                                     call_prem=spot * 0.008, put_prem=spot * 0.008, lots=lots))

    else:  # NEUTRAL
        results.append(iron_condor(underlying, spot, expiry,
                                   call_sell_prem=spot * 0.005,
                                   call_buy_prem=spot * 0.002,
                                   put_sell_prem=spot * 0.005,
                                   put_buy_prem=spot * 0.002, lots=lots))
        atm = _round_strike(spot)
        results.append(short_straddle(underlying, spot, expiry, atm,
                                      call_prem=spot * 0.015, put_prem=spot * 0.015, lots=lots))
        results.append(short_strangle(underlying, spot, expiry,
                                      call_prem=spot * 0.008, put_prem=spot * 0.008, lots=lots))

    return {
        "ok": True,
        "underlying": underlying,
        "spot": spot,
        "expiry": expiry,
        "bias": bias,
        "strategies": results,
        "generated_at": _now_ist(),
    }


def list_strategies() -> list:
    """List all available F&O strategies with descriptions."""
    return [
        {"key": "long_futures", "name": "Long Futures", "bias": "BULLISH",
         "risk": "UNLIMITED", "desc": "Buy index futures for directional upside"},
        {"key": "short_futures", "name": "Short Futures", "bias": "BEARISH",
         "risk": "UNLIMITED", "desc": "Sell index futures for directional downside"},
        {"key": "buy_call", "name": "Buy Call", "bias": "BULLISH",
         "risk": "DEFINED", "desc": "Limited risk bullish bet, pay premium"},
        {"key": "buy_put", "name": "Buy Put", "bias": "BEARISH",
         "risk": "DEFINED", "desc": "Limited risk bearish bet, pay premium"},
        {"key": "bull_call_spread", "name": "Bull Call Spread", "bias": "BULLISH",
         "risk": "DEFINED", "desc": "Buy lower call, sell higher call — capped profit/loss"},
        {"key": "bear_put_spread", "name": "Bear Put Spread", "bias": "BEARISH",
         "risk": "DEFINED", "desc": "Buy higher put, sell lower put — capped profit/loss"},
        {"key": "iron_condor", "name": "Iron Condor", "bias": "NEUTRAL",
         "risk": "DEFINED", "desc": "Sell OTM call spread + put spread — profit from range"},
        {"key": "long_straddle", "name": "Long Straddle", "bias": "VOLATILE",
         "risk": "DEFINED", "desc": "Buy ATM call + put — profit from big move either way"},
        {"key": "short_straddle", "name": "Short Straddle", "bias": "NEUTRAL",
         "risk": "UNLIMITED", "desc": "Sell ATM call + put — profit from low volatility"},
        {"key": "long_strangle", "name": "Long Strangle", "bias": "VOLATILE",
         "risk": "DEFINED", "desc": "Buy OTM call + put — cheaper straddle, needs bigger move"},
        {"key": "short_strangle", "name": "Short Strangle", "bias": "NEUTRAL",
         "risk": "UNLIMITED", "desc": "Sell OTM call + put — profit from range-bound market"},
    ]
