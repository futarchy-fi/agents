"""Table formatting for terminal output. No external dependencies."""

from __future__ import annotations

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
CYAN = "\033[36m"
YELLOW = "\033[33m"


def _col(text: str, width: int) -> str:
    s = str(text)
    if len(s) > width:
        return s[: width - 1] + "\u2026"
    return s.ljust(width)


def markets_table(markets: list[dict]) -> str:
    lines = []
    header = (
        f"  {BOLD}{_col('ID', 5)}"
        f"{_col('Market', 36)}"
        f"{_col('YES', 8)}"
        f"{_col('NO', 8)}"
        f"{_col('Volume', 10)}{RESET}"
    )
    lines.append(header)
    lines.append(f"  {'─' * 67}")

    for m in markets:
        mid = m.get("market_id", m.get("id", "?"))
        title = m.get("title", m.get("question", ""))
        yes_price = m.get("yes_price", m.get("prices", {}).get("yes", 0))
        no_price = m.get("no_price", m.get("prices", {}).get("no", 0))
        volume = m.get("volume", 0)

        yes_str = f"{float(yes_price):.2f}"
        no_str = f"{float(no_price):.2f}"
        vol_str = f"{int(volume):,}"

        lines.append(
            f"  {_col(mid, 5)}"
            f"{_col(title, 36)}"
            f"{GREEN}{_col(yes_str, 8)}{RESET}"
            f"{RED}{_col(no_str, 8)}{RESET}"
            f"{_col(vol_str, 10)}"
        )

    return "\n".join(lines)


def market_detail(m: dict) -> str:
    lines = []
    mid = m.get("market_id", m.get("id", "?"))
    title = m.get("title", m.get("question", ""))
    yes_price = m.get("yes_price", m.get("prices", {}).get("yes", 0))
    no_price = m.get("no_price", m.get("prices", {}).get("no", 0))
    volume = m.get("volume", 0)
    deadline = m.get("deadline", m.get("closes_at", "-"))
    status = m.get("status", "-")

    lines.append(f"\n  {BOLD}Market #{mid}: {title}{RESET}")
    lines.append(f"  {'─' * 50}")
    lines.append(f"  Status:   {status}")
    lines.append(f"  YES:      {GREEN}{float(yes_price):.2f}{RESET}")
    lines.append(f"  NO:       {RED}{float(no_price):.2f}{RESET}")
    lines.append(f"  Volume:   {int(volume):,}")
    lines.append(f"  Deadline: {deadline}")

    trades = m.get("trades", m.get("recent_trades", []))
    if trades:
        lines.append(f"\n  {BOLD}Recent Trades{RESET}")
        lines.append(f"  {'─' * 50}")
        lines.append(
            f"  {DIM}{_col('Side', 6)}{_col('Amount', 10)}{_col('Price', 8)}{_col('Time', 20)}{RESET}"
        )
        for t in trades[:10]:
            side = t.get("side", t.get("outcome", "?"))
            amount = t.get("amount", 0)
            price = t.get("price", 0)
            ts = t.get("time", t.get("created_at", "-"))
            color = GREEN if side.lower() == "yes" else RED
            lines.append(
                f"  {color}{_col(side, 6)}{RESET}"
                f"{_col(f'{float(amount):.1f}', 10)}"
                f"{_col(f'{float(price):.2f}', 8)}"
                f"{_col(str(ts), 20)}"
            )

    return "\n".join(lines)


def user_info(data: dict) -> str:
    lines = []
    lines.append(f"\n  {BOLD}Account{RESET}")
    lines.append(f"  {'─' * 40}")
    lines.append(f"  Balance: {CYAN}{data.get('balance', 0):.2f}{RESET}")

    positions = data.get("positions", [])
    if positions:
        lines.append(f"\n  {BOLD}Positions{RESET}")
        lines.append(f"  {'─' * 40}")
        lines.append(
            f"  {DIM}{_col('Market', 5)}{_col('Side', 6)}{_col('Shares', 10)}{_col('Avg Price', 10)}{RESET}"
        )
        for p in positions:
            mid = p.get("market_id", "?")
            side = p.get("outcome", p.get("side", "?"))
            shares = p.get("shares", p.get("amount", 0))
            avg = p.get("avg_price", 0)
            color = GREEN if str(side).lower() == "yes" else RED
            lines.append(
                f"  {_col(mid, 5)}"
                f"{color}{_col(side, 6)}{RESET}"
                f"{_col(f'{float(shares):.1f}', 10)}"
                f"{_col(f'{float(avg):.2f}', 10)}"
            )
    else:
        lines.append(f"\n  {DIM}No open positions.{RESET}")

    return "\n".join(lines)


def trade_result(data: dict) -> str:
    action = data.get("action", "trade")
    outcome = data.get("outcome", "?")
    shares = data.get("shares", 0)
    price = data.get("avg_price", data.get("price", 0))
    cost = data.get("cost", data.get("total", 0))
    market_id = data.get("market_id", "?")

    color = GREEN if str(outcome).lower() == "yes" else RED
    return (
        f"\n  {BOLD}{action.capitalize()} executed{RESET}\n"
        f"  Market:  #{market_id}\n"
        f"  Side:    {color}{outcome}{RESET}\n"
        f"  Shares:  {float(shares):.1f}\n"
        f"  Price:   {float(price):.4f}\n"
        f"  Cost:    {float(cost):.2f}\n"
    )
