"""Table formatting for terminal output. No external dependencies."""

from __future__ import annotations

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
PURPLE = "\033[35m"


def _trunc(text: str, width: int) -> str:
    s = str(text)
    if len(s) > width:
        return s[: width - 1] + "\u2026"
    return s


def _pad(text: str, width: int, right: bool = False) -> str:
    s = str(text)
    if right:
        return s.rjust(width)
    return s.ljust(width)


def _bar(yes: float, width: int = 20) -> str:
    filled = round(yes * width)
    empty = width - filled
    return f"{GREEN}{'█' * filled}{DIM}{'░' * empty}{RESET}"


def _signed(value: float) -> str:
    return f"{value:+,.2f}"


def markets_table(markets: list[dict]) -> str:
    if not markets:
        return f"\n  {DIM}No open markets.{RESET}\n"

    lines = [
        "",
        f"  {BOLD}{_pad('ID', 4)}{_pad('Market', 30)}{_pad('YES', 7)}{_pad('NO', 7)}Trades{RESET}",
        f"  {DIM}{'─' * 58}{RESET}",
    ]

    for m in markets:
        mid = str(m.get("market_id", m.get("id", "?")))
        question = m.get("question", "")

        # Extract short title from "Will PR #N 'title' merge by..." format
        title = question
        if "'" in question:
            parts = question.split("'")
            if len(parts) >= 2:
                pr_part = question.split("PR #")[1].split(" ")[0] if "PR #" in question else ""
                title = f"PR #{pr_part} {parts[1]}" if pr_part else parts[1]

        yes_p = float(m.get("prices", {}).get("yes", 0.5))
        no_p = float(m.get("prices", {}).get("no", 0.5))
        trades = m.get("num_trades", 0)

        yes_str = f"{yes_p:.2f}"
        no_str = f"{no_p:.2f}"

        lines.append(
            f"  {_pad(mid, 4)}"
            f"{_pad(_trunc(title, 28), 30)}"
            f"{GREEN}{_pad(yes_str, 7)}{RESET}"
            f"{RED}{_pad(no_str, 7)}{RESET}"
            f"{_pad(str(trades), 6, right=True)}"
        )

    lines.append("")
    return "\n".join(lines)


def market_detail(m: dict) -> str:
    mid = m.get("market_id", m.get("id", "?"))
    question = m.get("question", "")
    yes_p = float(m.get("prices", {}).get("yes", 0.5))
    no_p = float(m.get("prices", {}).get("no", 0.5))
    volume = m.get("volume", "0")
    deadline = m.get("deadline", "-")
    status = m.get("status", "-")
    trades_count = m.get("num_trades", 0)

    status_color = GREEN if status == "open" else YELLOW

    lines = [
        "",
        f"  {BOLD}#{mid}{RESET}  {question}",
        f"  {DIM}{'─' * 60}{RESET}",
        "",
        f"  Status     {status_color}{status}{RESET}",
        f"  Deadline   {deadline or '-'}",
        "",
        f"  {_bar(yes_p)}",
        f"  {GREEN}YES  {yes_p:.2f}{RESET}    {RED}NO  {no_p:.2f}{RESET}",
        "",
        f"  Volume     {float(volume):,.0f}",
        f"  Trades     {trades_count}",
    ]

    trades = m.get("trades", m.get("recent_trades", []))
    if trades:
        lines.append("")
        lines.append(f"  {BOLD}Recent Trades{RESET}")
        lines.append(f"  {DIM}{'─' * 50}{RESET}")
        lines.append(
            f"  {DIM}{_pad('Side', 6)}{_pad('Amount', 10)}{_pad('Price', 8)}{_pad('Time', 24)}{RESET}"
        )
        for t in trades[:10]:
            side = t.get("outcome", t.get("side", "?"))
            amount = t.get("amount", 0)
            price = t.get("price", 0)
            ts = t.get("created_at", t.get("time", "-"))
            if isinstance(ts, str) and "T" in ts:
                ts = ts.split("T")[0] + " " + ts.split("T")[1][:5]
            color = GREEN if side.lower() == "yes" else RED
            lines.append(
                f"  {color}{_pad(side.upper(), 6)}{RESET}"
                f"{_pad(f'{float(amount):.1f}', 10)}"
                f"{_pad(f'{float(price):.2f}', 8)}"
                f"{DIM}{ts}{RESET}"
            )

    lines.append("")
    return "\n".join(lines)


def user_info(data: dict) -> str:
    available = data.get("available", data.get("balance", "0"))
    frozen = data.get("frozen", "0")
    total = data.get("total", available)

    lines = [
        "",
        f"  {BOLD}Account{RESET}",
        f"  {DIM}{'─' * 40}{RESET}",
        f"  Available  {CYAN}{float(available):,.2f}{RESET}",
        f"  Frozen     {float(frozen):,.2f}",
        f"  Total      {BOLD}{float(total):,.2f}{RESET}",
    ]

    locks = data.get("locks", [])
    positions = data.get("positions", [])
    if locks:
        lines.append("")
        lines.append(f"  {BOLD}Locks{RESET}")
        lines.append(f"  {DIM}{'─' * 40}{RESET}")
        for lk in locks:
            mkt = lk.get("market_id", "?")
            amt = float(lk.get("amount", 0))
            lt = lk.get("lock_type", "")
            lines.append(f"  Market #{mkt}  {amt:,.2f}  {DIM}{lt}{RESET}")
    elif positions:
        lines.append("")
        lines.append(f"  {BOLD}Positions{RESET}")
        lines.append(f"  {DIM}{'─' * 40}{RESET}")
        for p in positions:
            mid = p.get("market_id", "?")
            side = p.get("outcome", p.get("side", "?"))
            shares = p.get("shares", p.get("amount", 0))
            color = GREEN if str(side).lower() == "yes" else RED
            lines.append(
                f"  #{_pad(str(mid), 4)} {color}{_pad(side, 4)}{RESET} {float(shares):,.1f}"
            )
    else:
        lines.append(f"\n  {DIM}No open positions.{RESET}")

    lines.append("")
    lines.append(f"  {DIM}Run futarchy activity for account history.{RESET}")
    lines.append("")
    return "\n".join(lines)


def activity_page(data: dict) -> str:
    entries = data.get("entries", [])
    if not entries:
        return f"\n  {DIM}No account activity yet.{RESET}\n"

    lines = [
        "",
        f"  {BOLD}Activity{RESET}",
        f"  {DIM}{'─' * 56}{RESET}",
    ]

    for entry in entries:
        ts = entry.get("created_at", "-")
        if isinstance(ts, str) and "T" in ts:
            ts = ts.split("T")[0] + " " + ts.split("T")[1][:5]

        summary = entry.get("summary", entry.get("reason", "activity"))
        market = entry.get("market_question")
        if not market and entry.get("market_id"):
            market = f"Market #{entry['market_id']}"

        avail_delta = float(entry.get("available_delta", 0))
        frozen_delta = float(entry.get("frozen_delta", 0))
        total_after = float(entry.get("total_after", 0))
        available_after = float(entry.get("available_after", 0))
        frozen_after = float(entry.get("frozen_after", 0))

        lines.append(f"  {BOLD}{summary}{RESET}  {DIM}{ts}{RESET}")
        if market:
            lines.append(f"  {DIM}{market}{RESET}")
        lines.append(
            f"  Avail {_signed(avail_delta)}  Frozen {_signed(frozen_delta)}"
        )
        lines.append(
            f"  Total {total_after:,.2f}  {DIM}(avail {available_after:,.2f}, frozen {frozen_after:,.2f}){RESET}"
        )
        lines.append("")

    if data.get("has_more"):
        cursor = data.get("next_before_tx_id")
        lines.append(
            f"  {DIM}Older entries available: futarchy activity --before-tx-id {cursor}{RESET}"
        )
        lines.append("")

    return "\n".join(lines)


def trade_result(data: dict) -> str:
    outcome = data.get("outcome", "?")
    amount = data.get("amount", data.get("shares", 0))
    price = data.get("price", 0)
    value = data.get("value", data.get("cost", 0))
    trade_id = data.get("trade_id", "")

    color = GREEN if str(outcome).lower() == "yes" else RED

    return (
        f"\n  {GREEN}Trade executed{RESET}"
        f"{'  #' + str(trade_id) if trade_id else ''}\n"
        f"  {DIM}{'─' * 30}{RESET}\n"
        f"  Side     {color}{outcome.upper()}{RESET}\n"
        f"  Tokens   {float(amount):,.1f}\n"
        f"  Price    {float(price):.4f}\n"
        f"  Value    {float(value):,.2f}\n"
    )
