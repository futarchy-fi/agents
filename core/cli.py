#!/usr/bin/env python3
"""
Futarchy engine CLI. Every invocation: lock → load → execute → save → unlock.

Usage:
    python3 -m core.cli create-account
    python3 -m core.cli mint ACCOUNT_ID AMOUNT
    python3 -m core.cli create-market QUESTION CATEGORY CATEGORY_ID
    python3 -m core.cli buy MARKET_ID ACCOUNT_ID OUTCOME BUDGET
    python3 -m core.cli sell MARKET_ID ACCOUNT_ID OUTCOME AMOUNT
    python3 -m core.cli resolve MARKET_ID OUTCOME
    python3 -m core.cli void MARKET_ID
    python3 -m core.cli account ACCOUNT_ID
    python3 -m core.cli market MARKET_ID
    python3 -m core.cli markets

Output: JSON, one line. {"ok": true, ...} or {"ok": false, "error": "..."}
State: FUTARCHY_STATE env var, default ./futarchy_state.json
"""

import argparse
import fcntl
import json
import os
import sys
from contextlib import contextmanager
from decimal import Decimal

from core.models import reset_counters, ZERO
from core.risk_engine import RiskEngine
from core.market_engine import MarketEngine
from core.persistence import save_snapshot, load_snapshot
from core.lmsr import prices


STATE_PATH = os.environ.get("FUTARCHY_STATE", "./futarchy_state.json")


@contextmanager
def file_lock(path):
    """Exclusive file lock. Prevents concurrent CLI invocations from corrupting state."""
    lock_path = path + ".lock"
    f = open(lock_path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def load_or_create(path):
    if os.path.exists(path):
        risk, me, _auth = load_snapshot(path)
        return risk, me
    reset_counters()
    risk = RiskEngine()
    me = MarketEngine(risk)
    return risk, me


def reply(data):
    print(json.dumps(data))


def cmd_create_account(risk, me, args):
    acc = risk.create_account()
    return {"ok": True, "account_id": acc.id}


def cmd_mint(risk, me, args):
    amount = Decimal(args.amount)
    risk.mint(args.account_id, amount)
    acc = risk.get_account(args.account_id)
    return {"ok": True, "account_id": args.account_id,
            "available": str(acc.available_balance)}


def cmd_create_market(risk, me, args):
    b = Decimal(args.b) if args.b else Decimal("100")
    market, amm = me.create_market(
        question=args.question,
        category=args.category,
        category_id=args.category_id,
        metadata={},
        b=b,
    )
    return {"ok": True, "market_id": market.id, "amm_account_id": amm.id,
            "b": str(market.b)}


def cmd_buy(risk, me, args):
    trade = me.buy(args.market_id, args.account_id,
                   args.outcome, Decimal(args.budget))
    return {"ok": True, "trade_id": trade.id,
            "amount": str(trade.amount), "price": str(trade.price),
            "value": str(trade.amount * trade.price)}


def cmd_sell(risk, me, args):
    trade = me.sell(args.market_id, args.account_id,
                    args.outcome, Decimal(args.amount))
    return {"ok": True, "trade_id": trade.id,
            "amount": str(trade.amount), "price": str(trade.price),
            "value": str(trade.amount * trade.price)}


def cmd_resolve(risk, me, args):
    me.resolve(args.market_id, args.outcome)
    return {"ok": True, "market_id": args.market_id,
            "resolution": args.outcome}


def cmd_void(risk, me, args):
    me.void(args.market_id)
    return {"ok": True, "market_id": args.market_id}


def cmd_account(risk, me, args):
    acc = risk.get_account(args.account_id)
    locks = [
        {"lock_id": l.lock_id, "market_id": l.market_id,
         "amount": str(l.amount), "lock_type": l.lock_type}
        for l in acc.locks
    ]
    return {"ok": True, "account_id": acc.id,
            "available": str(acc.available_balance),
            "frozen": str(acc.frozen_balance),
            "total": str(acc.total),
            "locks": locks}


def cmd_market(risk, me, args):
    market = me.markets.get(args.market_id)
    if market is None:
        return {"ok": False, "error": f"market {args.market_id} not found"}

    p = prices(market.q, market.b) if market.status == "open" else {}

    positions = {}
    for acc_id, pos in market.positions.items():
        positions[acc_id] = {o: str(v) for o, v in pos.items()}

    return {"ok": True, "market_id": market.id,
            "question": market.question,
            "status": market.status,
            "outcomes": market.outcomes,
            "prices": {o: str(v) for o, v in p.items()},
            "b": str(market.b),
            "positions": positions,
            "num_trades": len(market.trades),
            "resolution": market.resolution}


def cmd_markets(risk, me, args):
    result = []
    for m in me.markets.values():
        p = prices(m.q, m.b) if m.status == "open" else {}
        result.append({
            "market_id": m.id,
            "question": m.question,
            "status": m.status,
            "prices": {o: str(v) for o, v in p.items()},
            "num_trades": len(m.trades),
        })
    return {"ok": True, "markets": result}


# Commands that mutate state (need save after)
MUTATING = {"create-account", "mint", "create-market",
            "buy", "sell", "resolve", "void"}


def main():
    parser = argparse.ArgumentParser(description="Futarchy engine CLI")
    parser.add_argument("--state", default=STATE_PATH,
                        help="Path to state file")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("create-account")

    p = sub.add_parser("mint")
    p.add_argument("account_id", type=int)
    p.add_argument("amount")

    p = sub.add_parser("create-market")
    p.add_argument("question")
    p.add_argument("category")
    p.add_argument("category_id")
    p.add_argument("--b", default=None, help="Liquidity parameter (default 100)")

    p = sub.add_parser("buy")
    p.add_argument("market_id", type=int)
    p.add_argument("account_id", type=int)
    p.add_argument("outcome")
    p.add_argument("budget")

    p = sub.add_parser("sell")
    p.add_argument("market_id", type=int)
    p.add_argument("account_id", type=int)
    p.add_argument("outcome")
    p.add_argument("amount")

    p = sub.add_parser("resolve")
    p.add_argument("market_id", type=int)
    p.add_argument("outcome")

    p = sub.add_parser("void")
    p.add_argument("market_id", type=int)

    p = sub.add_parser("account")
    p.add_argument("account_id", type=int)

    p = sub.add_parser("market")
    p.add_argument("market_id", type=int)

    sub.add_parser("markets")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "create-account": cmd_create_account,
        "mint": cmd_mint,
        "create-market": cmd_create_market,
        "buy": cmd_buy,
        "sell": cmd_sell,
        "resolve": cmd_resolve,
        "void": cmd_void,
        "account": cmd_account,
        "market": cmd_market,
        "markets": cmd_markets,
    }

    state_path = args.state

    try:
        with file_lock(state_path):
            risk, me = load_or_create(state_path)
            result = commands[args.command](risk, me, args)

            if args.command in MUTATING:
                save_snapshot(risk, me, state_path)

            reply(result)
    except Exception as e:
        reply({"ok": False, "error": str(e)})
        sys.exit(1)


if __name__ == "__main__":
    main()
