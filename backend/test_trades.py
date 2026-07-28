"""
Test script: place 2 mock trades to verify the trading execution engine works.
Run from backend/: python test_trades.py
"""

import os
import sys
import json

os.environ["MOCK_MODE"] = "true"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from upstox_orders import OrderEngine

def main():
    engine = OrderEngine()

    print("=" * 60)
    print("TRADING AGENT — EXECUTION TEST")
    print(f"Mode: {'MOCK (paper trading)' if engine.mock_mode else 'LIVE'}")
    print("=" * 60)

    # Check initial state
    funds = engine.get_funds()
    holdings = engine.get_holdings()
    print(f"\nInitial Balance : Rs.{funds['available_balance']:,.2f}")
    print(f"Holdings        : {len(holdings['holdings'])} stocks")
    for h in holdings["holdings"]:
        print(f"  - {h['tsym']}: {h['qty']} shares @ Rs.{h['avgprc']}")
    print()

    # ── Trade 1: BUY 10 shares of TCS at Rs.4,200 ──
    print("-" * 60)
    print("TRADE 1: BUY 10 shares of TCS (LIMIT @ Rs.4,200)")
    print("-" * 60)
    result1 = engine.place_order(
        tsym="TCS",
        side="BUY",
        qty=10,
        order_type="LIMIT",
        price=4200.0,
        product="D",
    )
    print(json.dumps(result1, indent=2))

    # ── Trade 2: BUY 25 shares of SUZLON at Rs.55 (market) ──
    print()
    print("-" * 60)
    print("TRADE 2: BUY 25 shares of SUZLON (LIMIT @ Rs.55)")
    print("-" * 60)
    result2 = engine.place_order(
        tsym="SUZLON",
        side="BUY",
        qty=25,
        order_type="LIMIT",
        price=55.0,
        product="D",
    )
    print(json.dumps(result2, indent=2))

    # ── Final state ──
    print()
    print("=" * 60)
    print("POST-TRADE STATE")
    print("=" * 60)
    funds = engine.get_funds()
    holdings = engine.get_holdings()
    orders = engine.get_orders()
    print(f"\nBalance : Rs.{funds['available_balance']:,.2f}")
    print(f"Holdings: {len(holdings['holdings'])} stocks")
    for h in holdings["holdings"]:
        print(f"  - {h['tsym']}: {h['qty']} shares @ Rs.{h['avgprc']}")
    print(f"\nOrders today: {len(orders['orders'])}")
    for o in orders["orders"]:
        print(f"  [{o['order_id']}] {o['side']} {o['qty']}x {o['tsym']} @ Rs.{o['price']} — {o['status']}")

    # ── Trade log ──
    log = engine.get_trade_log()
    print(f"\nTrade Log ({len(log['trades'])} entries):")
    for t in log["trades"]:
        print(f"  {t['placed_at']} | {t['side']} {t['qty']}x {t['tsym']} @ Rs.{t['price']} [{t['status']}]")

    print()
    print("All trades executed successfully!" if result1["ok"] and result2["ok"]
          else "Some trades failed — check errors above.")


if __name__ == "__main__":
    main()
