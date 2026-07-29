"""
Test script: Nifty F&O strategies — plan + execute test trades.
Run from backend/: python test_fo_trades.py
"""

import os
import sys
import json

os.environ["MOCK_MODE"] = "true"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import nifty_fo_engine
from upstox_orders import OrderEngine


def main():
    engine = OrderEngine()
    NIFTY_SPOT = 24850.0
    EXPIRY = "2025-08-28"

    print("=" * 70)
    print("NIFTY F&O TRADING AGENT — STRATEGY EXECUTION TEST")
    print(f"Mode       : {'MOCK (paper trading)' if engine.mock_mode else 'LIVE'}")
    print(f"Nifty Spot : {NIFTY_SPOT}")
    print(f"Expiry     : {EXPIRY}")
    print("=" * 70)

    # ── Show available strategies ──
    print("\nAvailable F&O Strategies:")
    for s in nifty_fo_engine.list_strategies():
        print(f"  [{s['bias']:>8}] {s['name']:<25} | Risk: {s['risk']:<10} | {s['desc']}")

    # ── Test 1: Bull Call Spread ──
    print("\n" + "=" * 70)
    print("TRADE 1: BULL CALL SPREAD on NIFTY")
    print("  Buy 24850 CE, Sell 24950 CE — defined risk bullish play")
    print("=" * 70)

    plan1 = nifty_fo_engine.bull_call_spread(
        underlying="NIFTY",
        spot=NIFTY_SPOT,
        expiry=EXPIRY,
        buy_strike=24850,
        sell_strike=24950,
        buy_premium=180.0,
        sell_premium=130.0,
        lots=1,
    )
    print("\nStrategy Plan:")
    print(f"  Strategy       : {plan1['strategy']}")
    print(f"  Bias           : {plan1['bias']}")
    print(f"  Lot size       : {plan1['lot_size']}")
    print(f"  Qty per leg    : {plan1['qty']}")
    print(f"  Net Debit      : Rs.{plan1['net_debit']:,.2f}")
    print(f"  Max Profit     : Rs.{plan1['max_profit']:,.2f}")
    print(f"  Max Loss       : Rs.{plan1['max_loss']:,.2f}")
    print(f"  Breakeven      : {plan1['breakeven']}")
    print(f"  Risk Type      : {plan1['risk_type']}")
    print("\n  Legs:")
    for leg in plan1["legs"]:
        print(f"    {leg['side']} {leg['qty']}x {plan1['underlying']}"
              f" {int(leg['strike'])} {leg['option_type']} @ Rs.{leg['price']}")

    print("\n  Executing...")
    result1 = engine.place_fo_strategy(plan1)
    print(f"  Result: {'SUCCESS' if result1['ok'] else 'FAILED'}")
    print(f"  Legs filled: {result1['legs_filled']}/{result1['legs_total']}")
    for lr in result1["leg_results"]:
        status = "OK" if lr["ok"] else "FAIL"
        print(f"    Leg {lr['leg']}: [{status}] {lr['leg_desc']} — {lr.get('order_id', lr.get('error', ''))}")

    # ── Test 2: Iron Condor ──
    print("\n" + "=" * 70)
    print("TRADE 2: IRON CONDOR on NIFTY")
    print("  Sell 25100 CE + Buy 25200 CE + Sell 24600 PE + Buy 24500 PE")
    print("  Neutral strategy — profit if Nifty stays in range")
    print("=" * 70)

    plan2 = nifty_fo_engine.iron_condor(
        underlying="NIFTY",
        spot=NIFTY_SPOT,
        expiry=EXPIRY,
        call_sell=25100,
        call_buy=25200,
        put_sell=24600,
        put_buy=24500,
        call_sell_prem=65.0,
        call_buy_prem=40.0,
        put_sell_prem=55.0,
        put_buy_prem=30.0,
        lots=1,
    )
    print("\nStrategy Plan:")
    print(f"  Strategy       : {plan2['strategy']}")
    print(f"  Bias           : {plan2['bias']}")
    print(f"  Net Credit     : Rs.{plan2['net_credit']:,.2f}")
    print(f"  Max Profit     : Rs.{plan2['max_profit']:,.2f}")
    print(f"  Max Loss       : Rs.{plan2['max_loss']:,.2f}")
    print(f"  Profit Zone    : {plan2['profit_zone']}")
    print(f"  BE Upper       : {plan2['breakeven_upper']}")
    print(f"  BE Lower       : {plan2['breakeven_lower']}")
    print(f"  Risk Type      : {plan2['risk_type']}")
    print("\n  Legs:")
    for leg in plan2["legs"]:
        print(f"    {leg['side']} {leg['qty']}x {plan2['underlying']}"
              f" {int(leg['strike'])} {leg['option_type']} @ Rs.{leg['price']}")

    print("\n  Executing...")
    result2 = engine.place_fo_strategy(plan2)
    print(f"  Result: {'SUCCESS' if result2['ok'] else 'FAILED'}")
    print(f"  Legs filled: {result2['legs_filled']}/{result2['legs_total']}")
    for lr in result2["leg_results"]:
        status = "OK" if lr["ok"] else "FAIL"
        print(f"    Leg {lr['leg']}: [{status}] {lr['leg_desc']} — {lr.get('order_id', lr.get('error', ''))}")

    # ── Show auto-recommendations ──
    print("\n" + "=" * 70)
    print("AUTO-RECOMMENDED STRATEGIES (BULLISH bias)")
    print("=" * 70)
    recs = nifty_fo_engine.recommend_strategies("NIFTY", NIFTY_SPOT, EXPIRY, "BULLISH", 1)
    for s in recs["strategies"]:
        print(f"\n  {s['strategy']} ({s['bias']})")
        print(f"    Max Profit: {s['max_profit']}  |  Max Loss: {s['max_loss']}  |  Risk: {s['risk_type']}")
        if "breakeven" in s:
            print(f"    Breakeven: {s['breakeven']}")
        if "margin_required" in s:
            print(f"    Margin: Rs.{s['margin_required']:,.2f}")

    # ── Final summary ──
    print("\n" + "=" * 70)
    print("EXECUTION SUMMARY")
    print("=" * 70)
    log = engine.get_trade_log()
    fo_trades = [t for t in log["trades"] if any(x in t.get("tsym", "")
                 for x in ("FUT", "CE", "PE"))]
    print(f"  F&O trades executed: {len(fo_trades)}")
    for t in fo_trades:
        print(f"    {t['placed_at']} | {t['side']} {t['qty']}x {t['tsym']} "
              f"@ Rs.{t['price']} [{t['status']}]")

    funds = engine.get_funds()
    print(f"\n  Balance remaining: Rs.{funds['available_balance']:,.2f}")
    print()
    all_ok = result1["ok"] and result2["ok"]
    print("All F&O test trades executed successfully!" if all_ok
          else "Some trades failed — check errors above.")


if __name__ == "__main__":
    main()
