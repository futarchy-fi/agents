"""CLI entry point — flat command dispatch with argparse."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from . import api as api_mod
from . import auth
from . import fmt


def _add_global_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", dest="json_output", action="store_true",
                        help="Output as JSON")
    parser.add_argument("--api-url", default=None,
                        help="Override API base URL")


def _client(args) -> api_mod.Client:
    url = args.api_url or auth.get_api_url()
    key = auth.get_api_key()
    return api_mod.Client(api_url=url, api_key=key)


def _authed_client(args) -> api_mod.Client:
    url = args.api_url or auth.get_api_url()
    key = auth.require_auth()
    return api_mod.Client(api_url=url, api_key=key)


def _output(args, data, formatter):
    if args.json_output:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(formatter(data))


# ── Command handlers ──

def cmd_markets(args) -> int:
    client = _client(args)
    markets = client.list_markets()
    _output(args, markets, fmt.markets_table)
    return 0


def cmd_market(args) -> int:
    client = _client(args)
    market = client.get_market(args.market_id)
    _output(args, market, fmt.market_detail)
    return 0


def cmd_login(args) -> int:
    url = args.api_url or auth.get_api_url()
    client = api_mod.Client(api_url=url)
    auth.login(client)
    return 0


def cmd_logout(args) -> int:
    auth.logout()
    return 0


def cmd_me(args) -> int:
    client = _authed_client(args)
    data = client.me()
    _output(args, data, fmt.user_info)
    return 0


def cmd_buy(args) -> int:
    client = _authed_client(args)
    result = client.buy(args.market_id, args.outcome, args.budget)
    _output(args, result, fmt.trade_result)
    return 0


def cmd_sell(args) -> int:
    client = _authed_client(args)
    result = client.sell(args.market_id, args.outcome, args.amount)
    _output(args, result, fmt.trade_result)
    return 0


def _sub(subparsers, name: str, **kwargs) -> argparse.ArgumentParser:
    """Create a subparser with global args inherited."""
    p = subparsers.add_parser(name, **kwargs)
    _add_global_args(p)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="futarchy",
        description="Futarchy — prediction markets for code",
    )
    parser.add_argument("--version", action="version",
                        version=f"futarchy {__version__}")
    _add_global_args(parser)
    sub = parser.add_subparsers(dest="command")

    # futarchy markets
    _sub(sub, "markets", help="List open markets")

    # futarchy market <id>
    p_market = _sub(sub, "market", help="Show market detail")
    p_market.add_argument("market_id", type=int, help="Market ID")

    # futarchy login
    _sub(sub, "login", help="Create an account")

    # futarchy logout
    _sub(sub, "logout", help="Clear saved credentials")

    # futarchy me
    _sub(sub, "me", help="Show balance and positions")

    # futarchy buy <id> <outcome> <budget>
    p_buy = _sub(sub, "buy", help="Buy outcome tokens")
    p_buy.add_argument("market_id", type=int, help="Market ID")
    p_buy.add_argument("outcome", choices=["yes", "no"], help="Outcome to buy")
    p_buy.add_argument("budget", type=float, help="Amount to spend")

    # futarchy sell <id> <outcome> <amount>
    p_sell = _sub(sub, "sell", help="Sell outcome tokens")
    p_sell.add_argument("market_id", type=int, help="Market ID")
    p_sell.add_argument("outcome", choices=["yes", "no"], help="Outcome to sell")
    p_sell.add_argument("amount", type=float, help="Number of tokens to sell")

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    dispatch = {
        "markets": cmd_markets,
        "market": cmd_market,
        "login": cmd_login,
        "logout": cmd_logout,
        "me": cmd_me,
        "buy": cmd_buy,
        "sell": cmd_sell,
    }

    try:
        return dispatch[args.command](args)
    except api_mod.APIError as e:
        if args.json_output:
            print(json.dumps({"error": e.detail, "status": e.status}))
        else:
            print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
