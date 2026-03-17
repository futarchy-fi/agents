#!/usr/bin/env python3
"""naive-bayes: a simple base-rate forecaster for PR prediction markets.

Computes per-repo conditional merge rates from GitHub history, adjusts
per-PR using a few signals (size, merge conflicts, author history, ghost),
then trades toward its estimate using Kelly sizing with a virtual bankroll.

Designed to run as a cron (e.g., every hour). Idempotent — tracks positions
and only trades the incremental difference.

Usage:
    FUTARCHY_API_KEY=... python3 agents/naive_bayes.py [--dry-run]
"""

import argparse
import json
import logging
import math
import os
import subprocess
import sys

logging.basicConfig(
    level=logging.INFO,
    format="[naive-bayes] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_URL = os.environ.get("FUTARCHY_API_URL", "https://api.futarchy.ai")
API_KEY = os.environ.get("FUTARCHY_API_KEY", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
VIRTUAL_BANKROLL = float(os.environ.get("VIRTUAL_BANKROLL", "1000"))
MIN_TRADE = float(os.environ.get("MIN_TRADE", "0.50"))
MIN_EDGE = float(os.environ.get("MIN_EDGE", "0.10"))
KELLY_FRACTION = float(os.environ.get("KELLY_FRACTION", "0.5"))  # half-Kelly

# Cache for repo base rates (avoid re-querying within a run)
_repo_stats_cache: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def api_get(path: str) -> any:
    import urllib.request
    url = f"{API_URL}/v1{path}"
    req = urllib.request.Request(url)
    if API_KEY:
        req.add_header("Authorization", f"Bearer {API_KEY}")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def api_post(path: str, body: dict) -> any:
    import urllib.request
    url = f"{API_URL}/v1{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if API_KEY:
        req.add_header("Authorization", f"Bearer {API_KEY}")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        log.error("POST %s failed (%d): %s", path, e.code, error_body)
        return None


# ---------------------------------------------------------------------------
# GitHub data
# ---------------------------------------------------------------------------

def gh_command(*args) -> str:
    """Run a gh CLI command and return stdout."""
    env = os.environ.copy()
    if GH_TOKEN:
        env["GH_TOKEN"] = GH_TOKEN
    result = subprocess.run(
        ["gh"] + list(args),
        capture_output=True, text=True, env=env,
    )
    if result.returncode != 0:
        log.warning("gh %s failed: %s", " ".join(args), result.stderr.strip())
        return ""
    return result.stdout.strip()


def get_repo_stats(repo: str) -> dict:
    """Get merge/close counts for a repo. Cached per run."""
    if repo in _repo_stats_cache:
        return _repo_stats_cache[repo]

    output = gh_command(
        "api", "graphql", "-f", f"""query={{
  repository(owner: "{repo.split('/')[0]}", name: "{repo.split('/')[1]}") {{
    merged: pullRequests(states: [MERGED]) {{ totalCount }}
    closed: pullRequests(states: [CLOSED]) {{ totalCount }}
  }}
}}""",
        "--jq", ".data.repository",
    )

    if not output:
        stats = {"merged": 0, "closed": 0, "base_rate": 0.5}
    else:
        data = json.loads(output)
        merged = data["merged"]["totalCount"]
        closed = data["closed"]["totalCount"]
        total = merged + closed
        base_rate = merged / total if total > 0 else 0.5
        stats = {"merged": merged, "closed": closed, "base_rate": base_rate}

    _repo_stats_cache[repo] = stats
    log.info("Repo %s: merged=%d, closed=%d, base_rate=%.2f",
             repo, stats["merged"], stats["closed"], stats["base_rate"])
    return stats


def get_pr_details(repo: str, pr_num: int) -> dict | None:
    """Fetch per-PR signals from GitHub."""
    output = gh_command(
        "pr", "view", str(pr_num),
        "--repo", repo,
        "--json", "additions,deletions,author,isDraft,mergeable,reviews",
    )
    if not output:
        return None
    return json.loads(output)


def get_author_merge_rate(repo: str, author: str) -> float | None:
    """Check if this author has merged PRs in this repo before."""
    output = gh_command(
        "pr", "list",
        "--repo", repo,
        "--author", author,
        "--state", "merged",
        "--limit", "5",
        "--json", "number",
    )
    if not output:
        return None
    merged = json.loads(output)

    output2 = gh_command(
        "pr", "list",
        "--repo", repo,
        "--author", author,
        "--state", "closed",
        "--limit", "20",
        "--json", "number",
    )
    closed = json.loads(output2) if output2 else []

    total = len(merged) + len(closed)
    if total == 0:
        return None
    return len(merged) / total


# ---------------------------------------------------------------------------
# Forecaster
# ---------------------------------------------------------------------------

def estimate_merge_probability(repo: str, pr_num: int) -> float:
    """Estimate conditional merge probability for a PR."""
    stats = get_repo_stats(repo)
    prob = stats["base_rate"]

    pr = get_pr_details(repo, pr_num)
    if pr is None:
        return prob  # fall back to base rate

    # Signal 1: PR size
    churn = (pr.get("additions") or 0) + (pr.get("deletions") or 0)
    if churn < 50:
        prob += 0.05
    elif churn > 500:
        prob -= 0.05

    # Signal 2: Merge conflicts
    if pr.get("mergeable") == "CONFLICTING":
        prob -= 0.10

    # Signal 3: Draft
    if pr.get("isDraft"):
        prob -= 0.15

    # Signal 4: Ghost author
    author = pr.get("author", {})
    author_login = author.get("login") if author else None
    if not author_login or author_login == "ghost":
        prob -= 0.10
    else:
        # Signal 5: Author history
        author_rate = get_author_merge_rate(repo, author_login)
        if author_rate is not None:
            # Blend author rate with repo rate (author gets 30% weight)
            prob = 0.7 * prob + 0.3 * author_rate

    # Signal 6: Has review approvals
    reviews = pr.get("reviews", [])
    approvals = sum(1 for r in reviews if r.get("state") == "APPROVED")
    if approvals > 0:
        prob += 0.05

    changes_requested = sum(1 for r in reviews
                            if r.get("state") == "CHANGES_REQUESTED")
    if changes_requested > 0:
        prob -= 0.10

    return max(0.05, min(0.95, prob))


# ---------------------------------------------------------------------------
# Trading
# ---------------------------------------------------------------------------

def _lmsr_price(q_yes: float, q_no: float, b: float) -> float:
    """LMSR price of YES (numerically stable)."""
    max_q = max(q_yes, q_no)
    e_yes = math.exp((q_yes - max_q) / b)
    e_no = math.exp((q_no - max_q) / b)
    return e_yes / (e_yes + e_no)


def _lmsr_cost(q_yes: float, q_no: float, b: float) -> float:
    """LMSR cost function C(q) (numerically stable)."""
    max_q = max(q_yes, q_no)
    return max_q + b * math.log(
        math.exp((q_yes - max_q) / b) + math.exp((q_no - max_q) / b)
    )


def _cost_to_buy(q_yes: float, q_no: float, b: float,
                 outcome: str, delta: float) -> float:
    """Cost of buying delta shares of outcome."""
    if outcome == "yes":
        return _lmsr_cost(q_yes + delta, q_no, b) - _lmsr_cost(q_yes, q_no, b)
    else:
        return _lmsr_cost(q_yes, q_no + delta, b) - _lmsr_cost(q_yes, q_no, b)


def _optimal_delta(q_yes: float, q_no: float, b: float,
                   prob: float, W: float, outcome: str) -> float:
    """Find optimal shares to buy by maximizing expected log utility.

    The agent maximizes:
        E[U] = p * log(W - cost + delta) + (1-p) * log(W - cost)

    where cost = LMSR cost of buying delta shares, and p is the
    agent's belief about the outcome it's buying.

    The FOC is:
        p * (1 - pi) / (W - c + delta) = (1 - p) * pi / (W - c)

    where pi = marginal price after buying delta shares.
    Solved by binary search over delta.
    """
    # p here is the agent's belief for the outcome being traded
    p = prob if outcome == "yes" else (1.0 - prob)

    lo, hi = 0.0, W * 2  # upper bound: can't spend more than W
    best_delta = 0.0

    for _ in range(80):
        delta = (lo + hi) / 2
        if delta < 1e-6:
            break

        cost = _cost_to_buy(q_yes, q_no, b, outcome, delta)

        # Can't afford
        if cost >= W:
            hi = delta
            continue

        # Marginal price after trade
        if outcome == "yes":
            pi = _lmsr_price(q_yes + delta, q_no, b)
        else:
            pi = _lmsr_price(q_yes, q_no + delta, b)
            # For NO outcome, pi is price of YES — we need price of NO
            pi = 1.0 - _lmsr_price(q_yes, q_no + delta, b)

        wealth_after_loss = W - cost
        wealth_after_win = W - cost + delta

        if wealth_after_loss <= 1e-9 or wealth_after_win <= 1e-9:
            hi = delta
            continue

        # FOC: p*(1-pi)/(W-c+δ) - (1-p)*pi/(W-c) = 0
        # Positive → buy more, negative → buy less
        foc = p * (1.0 - pi) / wealth_after_win - (1.0 - p) * pi / wealth_after_loss

        if foc > 1e-12:
            lo = delta
            best_delta = delta
        else:
            hi = delta

    # Apply Kelly fraction for conservatism
    best_delta *= KELLY_FRACTION

    # Compute actual cost for the chosen delta
    if best_delta < 1e-6:
        return 0.0
    return best_delta


def compute_trade(prob: float, q_yes: float, q_no: float, b: float,
                  virtual_bankroll: float) -> tuple[str, float]:
    """Compute optimal trade given belief, LMSR state, and wealth.

    Returns (outcome, budget) — the credits to spend.
    """
    yes_price = _lmsr_price(q_yes, q_no, b)

    if prob > yes_price + MIN_EDGE:
        delta = _optimal_delta(q_yes, q_no, b, prob, virtual_bankroll, "yes")
        if delta < 1e-6:
            return ("", 0.0)
        cost = _cost_to_buy(q_yes, q_no, b, "yes", delta)
        return ("yes", cost)

    elif prob < yes_price - MIN_EDGE:
        delta = _optimal_delta(q_yes, q_no, b, prob, virtual_bankroll, "no")
        if delta < 1e-6:
            return ("", 0.0)
        cost = _cost_to_buy(q_yes, q_no, b, "no", delta)
        return ("no", cost)

    else:
        return ("", 0.0)


def get_my_account_id() -> int | None:
    """Get the agent's account ID."""
    me = api_get("/me")
    if me and "account_id" in me:
        return me["account_id"]
    return None


def get_my_position(market_id: int, account_id: int) -> dict[str, float]:
    """Get agent's current position in a market."""
    positions = api_get(f"/markets/{market_id}/positions")
    if not positions:
        return {"yes": 0.0, "no": 0.0}
    for pos in positions:
        if pos.get("account_id") == account_id:
            return {
                "yes": float(pos.get("yes", 0)),
                "no": float(pos.get("no", 0)),
            }
    return {"yes": 0.0, "no": 0.0}


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(dry_run: bool = False):
    if not API_KEY:
        log.error("FUTARCHY_API_KEY not set")
        sys.exit(1)

    account_id = get_my_account_id()
    if account_id is None:
        log.error("Could not fetch account info — check API key")
        sys.exit(1)

    me = api_get("/me")
    balance = float(me.get("available", 0))
    log.info("Account #%d, balance: %.2f credits", account_id, balance)

    # Fetch all open pr_merge markets
    markets = api_get("/markets?category=pr_merge&status=open")
    if not markets:
        log.info("No open markets")
        return

    log.info("Found %d open markets", len(markets))

    trades_made = 0
    for market in markets:
        market_id = market["market_id"]
        repo = market.get("metadata", {}).get("repo")
        pr_num = market.get("metadata", {}).get("pr_number")

        if not repo or not pr_num:
            # Try parsing from category_id: "owner/repo#num@date"
            cid = market.get("category_id", "")
            if "#" in cid:
                repo = cid.split("#")[0]
                pr_num = int(cid.split("#")[1].split("@")[0])
            else:
                log.warning("Market %d: can't determine repo/PR, skipping", market_id)
                continue

        # Fetch market detail for LMSR state (q, b)
        detail = api_get(f"/markets/{market_id}")
        if not detail:
            log.warning("Market %d: can't fetch detail, skipping", market_id)
            continue

        q = detail.get("q", {})
        q_yes = float(q.get("yes", 0))
        q_no = float(q.get("no", 0))
        b = float(detail.get("b", 100))
        yes_price = _lmsr_price(q_yes, q_no, b)

        # Check existing position
        position = get_my_position(market_id, account_id)
        has_position = position["yes"] > 0 or position["no"] > 0

        # Estimate
        prob = estimate_merge_probability(repo, pr_num)

        # Optimal trade: maximize expected log utility over LMSR
        outcome, budget = compute_trade(prob, q_yes, q_no, b, VIRTUAL_BANKROLL)

        if budget < MIN_TRADE:
            if not has_position:
                log.info("Market %d (%s#%d): est=%.2f, price=%.2f, edge too small or no profitable trade",
                         market_id, repo, pr_num, prob, yes_price)
            continue

        # Reduce budget if we already have a position in the same direction
        if has_position:
            existing_value = position.get(outcome, 0.0)
            if existing_value > 0:
                budget = max(0, budget - existing_value * yes_price)
                if budget < MIN_TRADE:
                    log.info("Market %d: already positioned, skipping", market_id)
                    continue

        # Check actual balance
        if budget > balance:
            budget = balance
        if budget < MIN_TRADE:
            log.warning("Insufficient balance, stopping")
            break

        log.info("Market %d (%s#%d): est=%.2f, price=%.2f, b=%.1f → %s %.2f credits",
                 market_id, repo, pr_num, prob, yes_price, b, outcome.upper(), budget)

        if dry_run:
            continue

        result = api_post(f"/markets/{market_id}/buy", {
            "outcome": outcome,
            "budget": f"{budget:.2f}",
        })

        if result:
            log.info("  Trade executed: %s %s tokens @ %s avg price",
                     result.get("amount", "?"), outcome,
                     result.get("price", "?"))
            trades_made += 1
            balance -= budget
        else:
            log.warning("  Trade failed for market %d", market_id)

    log.info("Done. %d trades made.", trades_made)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="naive-bayes PR forecaster")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute estimates and log trades without executing")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
