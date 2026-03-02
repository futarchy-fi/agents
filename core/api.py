"""
FastAPI application. Agents-first HTTP API for the futarchy prediction market.

Public endpoints (no auth): health, markets, market detail, positions, trades.
User endpoints (API key): /me, buy, sell.
Admin endpoints (admin key): mint, create market, resolve, void.
"""

import asyncio
import hashlib
import hmac
import os
import json
import math
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse

from core.api_errors import APIError, api_error_handler, translate_engine_error
from core.api_models import (
    GitHubAuthRequest, AuthResponse,
    RegisterRequest, RegisterResponse,
    DeviceFlowStartRequest, DeviceFlowResponse, DeviceFlowPollRequest,
    AccountResponse, LockResponse,
    MarketSummary, MarketDetail, PositionEntry, TradeResponse,
    DepthEntry, DepthResponse,
    BuyRequest, SellRequest, TradeResult,
    CreateAccountResponse,
    MintRequest, MintResponse,
    CreateMarketRequest, CreateMarketResponse,
    ResolveRequest, HealthResponse,
    AddLiquidityRequest, AddLiquidityResponse,
    UpdateMetadataRequest,
)
from core.auth import (
    AuthStore, validate_github_token,
    start_device_flow, poll_device_flow,
)
from core.reputation import calculate_credits
from core.lmsr import max_loss, prices as lmsr_prices, cost_to_move_price
from core.market_engine import MarketEngine
from core.middleware import AuthUser, AdminDep, require_auth, rate_limiter
from core.models import ZERO, reset_counters
from core.persistence import save_snapshot, load_snapshot
from core.risk_engine import RiskEngine, InsufficientBalance


STATE_PATH = os.environ.get("FUTARCHY_STATE", "./futarchy_state.json")
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
WEBHOOK_CONFIG_PATH = os.environ.get(
    "FUTARCHY_WEBHOOK_CONFIG_PATH",
    os.path.join(os.path.dirname(__file__), "webhook_repos.json"),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load state
    if os.path.exists(STATE_PATH):
        risk, me, auth_store = load_snapshot(STATE_PATH)
    else:
        reset_counters()
        risk = RiskEngine()
        me = MarketEngine(risk)
        auth_store = AuthStore()

    app.state.risk = risk
    app.state.me = me
    app.state.auth_store = auth_store or AuthStore()
    app.state.lock = asyncio.Lock()
    app.state.webhook_seen_delivery_ids = set()
    yield


app = FastAPI(title="Futarchy API", version="0.2.0", lifespan=lifespan)
app.add_exception_handler(APIError, api_error_handler)


def _save():
    """Save state to disk. Called after every mutation."""
    save_snapshot(app.state.risk, app.state.me, STATE_PATH,
                  auth_store=app.state.auth_store)


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
def _coerce_int(value, field_name: str) -> int | None:
    """Parse integer config fields, return None if empty."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            return int(value)
        except ValueError as exc:
            raise APIError(400, "invalid_webhook_config",
                           f"{field_name} must be an integer: {value}") from exc
    raise APIError(400, "invalid_webhook_config",
                   f"{field_name} must be an integer: {value}")


def _coerce_decimal(value, field_name: str) -> Decimal:
    """Parse decimal config fields."""
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise APIError(400, "invalid_webhook_request",
                       f"{field_name} must be a decimal: {value}") from exc
    if decimal_value <= 0:
        raise APIError(400, "invalid_webhook_request",
                       f"{field_name} must be positive: {value}")
    return decimal_value


def _load_webhook_repo_configs() -> list[dict]:
    """Load repository-level webhook config.

    Config precedence:
    1) FUTARCHY_WEBHOOK_CONFIG (JSON string)
    2) FUTARCHY_WEBHOOK_CONFIG_PATH (JSON file)
    3) fallback empty list
    """
    raw = os.environ.get("FUTARCHY_WEBHOOK_CONFIG")
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise APIError(500, "invalid_webhook_config",
                           "Invalid FUTARCHY_WEBHOOK_CONFIG JSON") from exc
    else:
        if not os.path.exists(WEBHOOK_CONFIG_PATH):
            return []
        try:
            with open(WEBHOOK_CONFIG_PATH) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise APIError(500, "invalid_webhook_config",
                           "Invalid FUTARCHY_WEBHOOK_CONFIG file") from exc

    if isinstance(data, dict):
        repos = data.get("repositories", [])
    else:
        repos = data

    if not isinstance(repos, list):
        raise APIError(500, "invalid_webhook_config",
                       "Webhook config must include a `repositories` array")
    return [r for r in repos if isinstance(r, dict)]


def _get_webhook_repo_config(repo_name: str) -> dict | None:
    for cfg in _load_webhook_repo_configs():
        if cfg.get("name") == repo_name:
            return cfg
    return None


def _resolve_webhook_secret(cfg: dict) -> str | None:
    """Resolve a repo's webhook secret from config or environment."""
    static_secret = cfg.get("secret")
    if isinstance(static_secret, str) and static_secret:
        return static_secret

    env_name = cfg.get("secret_env")
    if not isinstance(env_name, str) or not env_name:
        return None
    return os.environ.get(env_name)


def _verify_github_signature(secret: str, signature: str, body: bytes) -> None:
    if not signature:
        raise APIError(401, "missing_signature",
                       "X-Hub-Signature-256 is required")
    if not signature.startswith("sha256="):
        raise APIError(400, "invalid_signature",
                       "Unsupported signature format")
    expected = hmac.new(
        secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, f"sha256={expected}"):
        raise APIError(401, "invalid_signature",
                       "Webhook signature verification failed")


def _event_id_seen_or_record(delivery_id: str | None) -> bool:
    """Return True if this delivery ID was already processed."""
    if not delivery_id:
        return False
    seen = getattr(app.state, "webhook_seen_delivery_ids", set())
    if delivery_id in seen:
        return True
    seen.add(delivery_id)
    app.state.webhook_seen_delivery_ids = seen
    return False


def _format_deadline(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _pr_category_id(repo_name: str, pr_num: int, event_date: str | None = None) -> str:
    if not event_date:
        event_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{repo_name}#{pr_num}@{event_date}"


def _market_category(cfg: dict) -> str:
    category = cfg.get("category")
    return category if isinstance(category, str) and category else "pr_merge"


def _market_outcomes(cfg: dict) -> list[str]:
    outcomes = cfg.get("outcomes")
    if isinstance(outcomes, list) and outcomes:
        return [str(o) for o in outcomes if str(o)]
    return ["yes", "no"]


def _market_funding_and_account(cfg: dict) -> tuple[Decimal, int | None, int]:
    funding_raw = cfg.get("funding")
    b_raw = cfg.get("b")
    if funding_raw is not None and b_raw is not None:
        raise APIError(400, "invalid_webhook_config",
                       "Provide either `funding` or `b`, not both")

    outcomes = _market_outcomes(cfg)
    n_outcomes = len(outcomes)

    if funding_raw is not None:
        funding = _coerce_decimal(funding_raw, "funding")
        b = funding / Decimal(str(math.log(n_outcomes)))
    else:
        b_value = b_raw if b_raw is not None else "100"
        b = _coerce_decimal(b_value, "b")

    account_id = _coerce_int(cfg.get("funding_account_id"), "funding_account_id")
    env_name = cfg.get("funding_account_id_env")
    if account_id is None and isinstance(env_name, str):
        account_id = _coerce_int(os.environ.get(env_name), env_name)

    return b, account_id, n_outcomes


def _is_supported_pr_action(action: str | None) -> bool:
    return action in {"opened", "reopened", "closed"}


def _resolve_market_metadata(pr_num: int, pr_url: str, cfg: dict) -> dict:
    metadata = dict(cfg.get("metadata", {}))
    metadata.update({
        "pr_number": pr_num,
        "pr_url": pr_url,
    })
    return metadata


def _render_template(template: str, **kwargs) -> str:
    try:
        return template.format(**kwargs)
    except KeyError as exc:
        raise APIError(400, "invalid_webhook_config",
                       f"Template placeholder missing: {exc.args[0]}")


# ---------------------------------------------------------------------------
# Landing page + Health (public)
# ---------------------------------------------------------------------------

@app.get("/")
@app.get("/landing")
async def landing():
    return FileResponse(STATIC_DIR / "landing.html", media_type="text/html")

@app.get("/dashboard")
async def dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html", media_type="text/html")

@app.get("/install.sh")
async def install_script():
    return FileResponse(
        STATIC_DIR / "install.sh", media_type="text/plain; charset=utf-8")

@app.get("/v1/health")
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        markets=len(app.state.me.markets),
        accounts=len(app.state.risk.accounts),
    )


# ---------------------------------------------------------------------------
# Auth (no API key required)
# ---------------------------------------------------------------------------

@app.post("/v1/auth/github")
async def auth_github(req: GitHubAuthRequest) -> AuthResponse:
    """Exchange a GitHub token for an API key."""
    try:
        gh = await validate_github_token(req.github_token)
    except ValueError as e:
        code = str(e)
        if code == "github_token_invalid":
            raise APIError(401, "github_token_invalid",
                           "GitHub token is invalid or expired")
        raise APIError(502, "github_api_error",
                       f"GitHub API error: {code}")

    async with app.state.lock:
        auth_store = app.state.auth_store
        existing = auth_store.get_by_github_id(gh["id"])

        if existing:
            # Re-auth: rotate key, same account
            user, raw_key = auth_store.create_user(
                gh["id"], gh["login"], existing.account_id)
        else:
            # New user: create account, mint reputation-based credits
            acc = app.state.risk.create_account()
            credits = calculate_credits(
                gh["created_at"], gh["public_repos"], gh["followers"])
            if credits > ZERO:
                app.state.risk.mint(acc.id, credits)
            user, raw_key = auth_store.create_user(
                gh["id"], gh["login"], acc.id)

        _save()

    return AuthResponse(
        api_key=raw_key,
        account_id=user.account_id,
        github_login=user.github_login,
    )


@app.post("/v1/auth/register")
async def auth_register(req: RegisterRequest) -> RegisterResponse:
    """Register with just a username. No GitHub required."""
    username = req.username.strip()
    if not username or len(username) > 40:
        raise APIError(400, "invalid_username",
                       "Username must be 1-40 characters")

    async with app.state.lock:
        auth_store = app.state.auth_store
        try:
            acc = app.state.risk.create_account()
            if INITIAL_CREDITS > ZERO:
                app.state.risk.mint(acc.id, INITIAL_CREDITS)
            user, raw_key = auth_store.register_user(username, acc.id)
        except ValueError as e:
            if str(e) == "username_taken":
                raise APIError(409, "username_taken",
                               f"Username '{username}' is already taken")
            raise
        _save()

    return RegisterResponse(
        api_key=raw_key,
        account_id=user.account_id,
        username=username,
    )


@app.post("/v1/auth/device")
async def auth_device_start(req: DeviceFlowStartRequest) -> DeviceFlowResponse:
    """Start GitHub OAuth device flow."""
    if not GITHUB_CLIENT_ID:
        raise APIError(501, "device_flow_unavailable",
                       "GITHUB_CLIENT_ID not configured")
    try:
        data = await start_device_flow(GITHUB_CLIENT_ID)
    except ValueError as e:
        raise APIError(502, "github_api_error", str(e))

    return DeviceFlowResponse(
        device_code=data["device_code"],
        user_code=data["user_code"],
        verification_uri=data["verification_uri"],
        expires_in=data["expires_in"],
        interval=data.get("interval", 5),
    )


@app.post("/v1/auth/device/token")
async def auth_device_poll(req: DeviceFlowPollRequest) -> AuthResponse:
    """Poll GitHub OAuth device flow for completion."""
    if not GITHUB_CLIENT_ID:
        raise APIError(501, "device_flow_unavailable",
                       "GITHUB_CLIENT_ID not configured")

    try:
        token_data = await poll_device_flow(GITHUB_CLIENT_ID, req.device_code)
    except ValueError as e:
        code = str(e)
        if code == "device_flow_pending":
            raise APIError(202, "device_flow_pending",
                           "Authorization pending. Keep polling.")
        if code == "device_flow_expired":
            raise APIError(410, "device_flow_expired",
                           "Device code expired. Start a new flow.")
        raise APIError(502, "github_api_error", str(e))

    # We have an access_token — exchange it for our API key
    access_token = token_data["access_token"]
    try:
        gh = await validate_github_token(access_token)
    except ValueError:
        raise APIError(502, "github_api_error",
                       "Failed to validate GitHub access token")

    async with app.state.lock:
        auth_store = app.state.auth_store
        existing = auth_store.get_by_github_id(gh["id"])

        if existing:
            user, raw_key = auth_store.create_user(
                gh["id"], gh["login"], existing.account_id)
        else:
            acc = app.state.risk.create_account()
            credits = calculate_credits(
                gh["created_at"], gh["public_repos"], gh["followers"])
            if credits > ZERO:
                app.state.risk.mint(acc.id, credits)
            user, raw_key = auth_store.create_user(
                gh["id"], gh["login"], acc.id)

        _save()

    return AuthResponse(
        api_key=raw_key,
        account_id=user.account_id,
        github_login=user.github_login,
    )


# ---------------------------------------------------------------------------
# Public market data (no auth required)
# ---------------------------------------------------------------------------

@app.get("/v1/markets")
async def list_markets(
    category: str | None = None,
    category_id: str | None = None,
    status: str | None = None,
) -> list[MarketSummary]:
    """List all markets with current prices.

    Optional filters:
    - category: exact match on market category
    - category_id: prefix match (e.g. "pr_merge/repo#7" matches
      "pr_merge/repo#7@2026-02-24")
    - status: exact match or comma-separated list (e.g. "resolved,void")
    """
    status_set = set(status.split(",")) if status else None
    result = []
    for m in app.state.me.markets.values():
        if category is not None and m.category != category:
            continue
        if category_id is not None and not m.category_id.startswith(category_id):
            continue
        if status_set is not None and m.status not in status_set:
            continue
        p = lmsr_prices(m.q, m.b) if m.status == "open" else {}
        result.append(MarketSummary(
            market_id=m.id,
            question=m.question,
            category=m.category,
            category_id=m.category_id,
            status=m.status,
            outcomes=m.outcomes,
            prices={o: str(v) for o, v in p.items()},
            b=str(m.b),
            liquidity=str(max_loss(m.b, len(m.outcomes))),
            num_trades=len(m.trades),
            resolution=m.resolution,
            created_at=m.created_at,
            deadline=m.deadline,
            resolved_at=m.resolved_at,
        ))
    return result


@app.get("/v1/markets/{market_id}")
async def get_market(market_id: int) -> MarketDetail:
    """Get full market detail including LMSR state."""
    m = app.state.me.markets.get(market_id)
    if m is None:
        raise APIError(404, "market_not_found", f"Market {market_id} not found")

    p = lmsr_prices(m.q, m.b) if m.status == "open" else {}

    # Compute volume (sum of all trade values)
    volume = sum(t.amount * t.price for t in m.trades)

    return MarketDetail(
        market_id=m.id,
        question=m.question,
        category=m.category,
        category_id=m.category_id,
        status=m.status,
        outcomes=m.outcomes,
        prices={o: str(v) for o, v in p.items()},
        b=str(m.b),
        liquidity=str(max_loss(m.b, len(m.outcomes))),
        num_trades=len(m.trades),
        resolution=m.resolution,
        created_at=m.created_at,
        deadline=m.deadline,
        amm_account_id=m.amm_account_id,
        q={o: str(v) for o, v in m.q.items()},
        volume=str(volume),
        resolved_at=m.resolved_at,
        metadata=m.metadata,
    )


@app.get("/v1/markets/{market_id}/positions")
async def get_market_positions(market_id: int) -> list[PositionEntry]:
    """Get all positions in a market. Public — shows all participants."""
    m = app.state.me.markets.get(market_id)
    if m is None:
        raise APIError(404, "market_not_found", f"Market {market_id} not found")

    result = []
    for acc_id, pos in m.positions.items():
        # Skip AMM account and zero positions
        if acc_id == m.amm_account_id:
            continue
        if all(v == ZERO for v in pos.values()):
            continue
        acc = app.state.risk.get_account(acc_id)
        locks = [
            LockResponse(
                lock_id=lk.lock_id, market_id=lk.market_id,
                amount=str(lk.amount), lock_type=lk.lock_type,
            )
            for lk in acc.locks_for_market(market_id)
        ]
        result.append(PositionEntry(
            account_id=acc_id,
            positions={o: str(v) for o, v in pos.items()},
            locks=locks,
        ))
    return result


@app.get("/v1/markets/{market_id}/depth")
async def get_market_depth(market_id: int) -> DepthResponse:
    """Synthetic depth table: cost to move each outcome to target prices.

    Computed server-side from the LMSR cost function with exact Decimal math.
    Only available for open markets.
    """
    m = app.state.me.markets.get(market_id)
    if m is None:
        raise APIError(404, "market_not_found", f"Market {market_id} not found")
    if m.status != "open":
        return DepthResponse(market_id=m.id, rows=[])

    targets = [Decimal("0.6"), Decimal("0.7"), Decimal("0.8"),
               Decimal("0.9"), Decimal("0.95")]
    rows = []
    for outcome in m.outcomes:
        for tp in targets:
            try:
                amount, trade_cost = cost_to_move_price(m.q, m.b, outcome, tp)
            except (ValueError, ZeroDivisionError):
                continue
            if amount <= ZERO:
                continue
            rows.append(DepthEntry(
                target=f"{int(tp * 100)}%",
                outcome=outcome,
                cost=str(trade_cost),
                shares=str(amount),
            ))
    return DepthResponse(market_id=m.id, rows=rows)


@app.get("/v1/markets/{market_id}/trades")
async def get_market_trades(market_id: int) -> list[TradeResponse]:
    """Get all trades in a market. Public."""
    m = app.state.me.markets.get(market_id)
    if m is None:
        raise APIError(404, "market_not_found", f"Market {market_id} not found")

    return [
        TradeResponse(
            trade_id=t.id,
            market_id=t.market_id,
            outcome=t.outcome,
            amount=str(t.amount),
            price=str(t.price),
            value=str(t.amount * t.price),
            buyer_account_id=t.buyer.account_id,
            seller_account_id=t.seller.account_id,
            created_at=t.created_at,
        )
        for t in m.trades
    ]


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

@app.post("/v1/webhooks/github")
async def github_webhook(request: Request) -> dict:
    """Process GitHub pull request webhooks and auto-create/resolve markets."""
    body = await request.body()
    if not body:
        raise APIError(400, "empty_payload", "Missing request body")

    event = request.headers.get("x-github-event")
    delivery_id = request.headers.get("x-github-delivery")
    signature = request.headers.get("x-hub-signature-256")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise APIError(400, "invalid_json",
                       "Webhook payload must be valid JSON") from exc

    if event == "ping":
        return {"status": "ignored", "reason": "ping"}

    if event != "pull_request":
        return {"status": "ignored", "reason": "unsupported_event"}

    if _event_id_seen_or_record(delivery_id):
        return {"status": "ignored", "reason": "duplicate_delivery"}

    action = payload.get("action")
    if action is None:
        raise APIError(400, "invalid_payload", "Missing pull_request action")
    if not _is_supported_pr_action(action):
        return {"status": "ignored", "reason": "unsupported_action"}

    pull = payload.get("pull_request") or {}
    repo = (payload.get("repository") or {})

    repo_name = repo.get("full_name")
    if not isinstance(repo_name, str):
        raise APIError(400, "invalid_payload", "Missing repository.full_name")

    cfg = _get_webhook_repo_config(repo_name)
    if cfg is None:
        raise APIError(403, "repo_not_tracked",
                       f"Repository not approved for webhook events: {repo_name}")
    if not cfg.get("enabled", True):
        raise APIError(403, "repo_not_approved",
                       f"Webhook not enabled for repository: {repo_name}")

    secret = _resolve_webhook_secret(cfg)
    if not secret:
        raise APIError(400, "invalid_webhook_secret",
                       f"No webhook secret configured for {repo_name}")

    _verify_github_signature(secret, signature, body)

    if not isinstance(pull.get("number"), int):
        raise APIError(400, "invalid_payload", "Missing pull_request.number")
    pr_num = pull["number"]
    pr_title = pull.get("title") or f"PR #{pr_num}"
    pr_url = pull.get("html_url") or ""

    category = _market_category(cfg)
    outcomes = _market_outcomes(cfg)
    b, funding_account_id, _ = _market_funding_and_account(cfg)

    category_prefix = _pr_category_id(repo_name, pr_num)

    if action in {"opened", "reopened"}:
        deadline_hours = _coerce_int(cfg.get("deadline_hours"), "deadline_hours") or 24
        if deadline_hours <= 0:
            raise APIError(400, "invalid_webhook_config",
                           "deadline_hours must be positive")

        question_template = (
            cfg.get("question_template")
            or "Will PR #{pr_number} '{pr_title}' merge by {deadline}?"
        )
        deadline = _format_deadline(deadline_hours)
        question = _render_template(
            question_template,
            repo=repo_name,
            pr_number=pr_num,
            pr_title=pr_title,
            deadline=deadline,
        )

        # Avoid creating duplicate open markets for the same PR/day
        for existing in app.state.me.markets.values():
            if (existing.category == category and existing.status == "open"
                    and existing.category_id == category_prefix):
                return {
                    "status": "ignored",
                    "reason": "market_already_exists",
                    "category_id": category_prefix,
                }

        metadata = _resolve_market_metadata(pr_num, pr_url, cfg)
        async with app.state.lock:
            market, _ = app.state.me.create_market(
                question=question,
                category=category,
                category_id=category_prefix,
                metadata=metadata,
                b=b,
                outcomes=outcomes,
                deadline=deadline,
                funding_account_id=funding_account_id,
            )
            _save()

        return {
            "status": "created",
            "market_id": market.id,
            "category_id": category_prefix,
            "outcome": "yes",
            "action": action,
        }

    if action == "closed":
        prefix = f"{repo_name}#{pr_num}"
        merged = bool(pull.get("merged"))
        outcome = "yes" if merged else "no"

        open_market_ids = [
            m.id for m in app.state.me.markets.values()
            if m.category == category
            and m.status == "open"
            and m.category_id.startswith(prefix)
        ]

        if not open_market_ids:
            return {"status": "ignored", "reason": "no_open_market"}

        async with app.state.lock:
            for market_id in open_market_ids:
                app.state.me.resolve(market_id, outcome)
            _save()

        return {
            "status": "resolved",
            "resolved_count": len(open_market_ids),
            "outcome": outcome,
            "market_ids": open_market_ids,
        }

    return {"status": "ignored", "reason": "unsupported_action"}


# ---------------------------------------------------------------------------
# User endpoints (API key required)
# ---------------------------------------------------------------------------

@app.get("/v1/me")
async def get_me(user: AuthUser) -> AccountResponse:
    """Get authenticated user's account."""
    acc = app.state.risk.get_account(user.account_id)
    locks = [
        LockResponse(
            lock_id=lk.lock_id, market_id=lk.market_id,
            amount=str(lk.amount), lock_type=lk.lock_type,
        )
        for lk in acc.locks
    ]
    return AccountResponse(
        account_id=acc.id,
        available=str(acc.available_balance),
        frozen=str(acc.frozen_balance),
        total=str(acc.total),
        locks=locks,
    )


@app.post("/v1/markets/{market_id}/buy")
async def buy(market_id: int, req: BuyRequest, user: AuthUser) -> TradeResult:
    """Buy outcome tokens."""
    try:
        budget = Decimal(req.budget)
    except InvalidOperation:
        raise APIError(400, "invalid_amount", f"Invalid budget: {req.budget}")
    if budget <= ZERO:
        raise APIError(400, "invalid_amount", "Budget must be positive")

    async with app.state.lock:
        try:
            trade = app.state.me.buy(
                market_id, user.account_id, req.outcome, budget)
            _save()
        except (ValueError, InsufficientBalance) as e:
            raise translate_engine_error(e)

    return TradeResult(
        trade_id=trade.id,
        outcome=trade.outcome,
        amount=str(trade.amount),
        price=str(trade.price),
        value=str(trade.amount * trade.price),
    )


@app.post("/v1/markets/{market_id}/sell")
async def sell(market_id: int, req: SellRequest, user: AuthUser) -> TradeResult:
    """Sell outcome tokens."""
    try:
        amount = Decimal(req.amount)
    except InvalidOperation:
        raise APIError(400, "invalid_amount", f"Invalid amount: {req.amount}")
    if amount <= ZERO:
        raise APIError(400, "invalid_amount", "Amount must be positive")

    async with app.state.lock:
        try:
            trade = app.state.me.sell(
                market_id, user.account_id, req.outcome, amount)
            _save()
        except (ValueError, InsufficientBalance) as e:
            raise translate_engine_error(e)

    return TradeResult(
        trade_id=trade.id,
        outcome=trade.outcome,
        amount=str(trade.amount),
        price=str(trade.price),
        value=str(trade.amount * trade.price),
    )


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@app.post("/v1/admin/accounts")
async def admin_create_account(_: AdminDep) -> CreateAccountResponse:
    """Create a new account (e.g. treasury). Returns account_id."""
    async with app.state.lock:
        acc = app.state.risk.create_account()
        _save()
    return CreateAccountResponse(account_id=acc.id)


@app.post("/v1/admin/mint")
async def admin_mint(req: MintRequest, _: AdminDep) -> MintResponse:
    """Mint credits to an account."""
    try:
        amount = Decimal(req.amount)
    except InvalidOperation:
        raise APIError(400, "invalid_amount", f"Invalid amount: {req.amount}")
    if amount <= ZERO:
        raise APIError(400, "invalid_amount", "Amount must be positive")

    async with app.state.lock:
        try:
            app.state.risk.mint(req.account_id, amount)
            _save()
        except ValueError as e:
            raise translate_engine_error(e)

    acc = app.state.risk.get_account(req.account_id)
    return MintResponse(account_id=acc.id, available=str(acc.available_balance))


@app.post("/v1/admin/markets")
async def admin_create_market(req: CreateMarketRequest,
                              _: AdminDep) -> CreateMarketResponse:
    """Create a new market. Supply either `b` (LMSR parameter) or `funding`
    (dollar amount — converted to appropriate b)."""
    outcomes = req.outcomes or ["yes", "no"]
    funding_config = {"funding": req.funding, "b": req.b,
                      "funding_account_id": req.funding_account_id}
    b, funding_account_id, _ = _market_funding_and_account(funding_config)

    async with app.state.lock:
        try:
            market, amm = app.state.me.create_market(
                question=req.question,
                category=req.category,
                category_id=req.category_id,
                metadata=req.metadata,
                b=b,
                outcomes=outcomes,
                deadline=req.deadline,
                funding_account_id=funding_account_id,
            )
        except (ValueError, InsufficientBalance) as e:
            raise translate_engine_error(e)
        _save()

    return CreateMarketResponse(
        market_id=market.id,
        amm_account_id=amm.id,
        b=str(market.b),
    )


@app.post("/v1/admin/markets/{market_id}/resolve")
async def admin_resolve(market_id: int, req: ResolveRequest,
                        _: AdminDep) -> dict:
    """Resolve a market."""
    async with app.state.lock:
        try:
            app.state.me.resolve(market_id, req.outcome)
            _save()
        except ValueError as e:
            raise translate_engine_error(e)

    return {"market_id": market_id, "resolution": req.outcome}


@app.post("/v1/admin/markets/{market_id}/void")
async def admin_void(market_id: int, _: AdminDep) -> dict:
    """Void a market."""
    async with app.state.lock:
        try:
            app.state.me.void(market_id)
            _save()
        except ValueError as e:
            raise translate_engine_error(e)

    return {"market_id": market_id, "status": "void"}


@app.post("/v1/admin/markets/{market_id}/add-liquidity")
async def admin_add_liquidity(market_id: int, req: AddLiquidityRequest,
                              _: AdminDep) -> AddLiquidityResponse:
    """Add liquidity to a market. AMM must have sufficient available balance."""
    try:
        amount = Decimal(req.amount)
    except InvalidOperation:
        raise APIError(400, "invalid_amount", f"Invalid amount: {req.amount}")
    if amount <= ZERO:
        raise APIError(400, "invalid_amount", "Amount must be positive")

    async with app.state.lock:
        try:
            app.state.me.add_liquidity(
                market_id, amount,
                funding_account_id=req.funding_account_id)
            _save()
        except (ValueError, InsufficientBalance) as e:
            raise translate_engine_error(e)

    m = app.state.me.markets[market_id]
    return AddLiquidityResponse(
        market_id=market_id,
        b=str(m.b),
        funding_added=str(amount),
    )


@app.patch("/v1/admin/markets/{market_id}/metadata")
async def admin_update_metadata(market_id: int, req: UpdateMetadataRequest,
                                _: AdminDep) -> dict:
    """Merge keys into a market's metadata."""
    m = app.state.me.markets.get(market_id)
    if m is None:
        raise APIError(404, "market_not_found", f"Market {market_id} not found")

    async with app.state.lock:
        m.metadata.update(req.metadata)
        _save()

    return {"market_id": market_id, "metadata": m.metadata}
