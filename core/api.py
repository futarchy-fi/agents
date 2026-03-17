"""
FastAPI application. Agents-first HTTP API for the futarchy prediction market.

Public endpoints (no auth): health, markets, market detail, positions, trades.
User endpoints (API key): /me, buy, sell.
Admin endpoints (admin key): mint, create market, resolve, void, tracked repos.
Webhook: POST /v1/hooks/github — receive GitHub PR events for tracked repos.
"""

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, RedirectResponse

from core.api_errors import APIError, api_error_handler, translate_engine_error
from core.api_models import (
    AuthResponse,
    DeviceFlowStartRequest, DeviceFlowResponse, DeviceFlowPollRequest,
    AccountResponse, AccountActivityEntry, AccountActivityPage, LockResponse,
    MarketSummary, MarketDetail, PositionEntry, TradeResponse,
    DepthEntry, DepthResponse,
    BuyRequest, SellRequest, TradeResult,
    CreateAccountResponse,
    CreateServiceAccountRequest, CreateServiceAccountResponse,
    MintRequest, MintResponse,
    CreateMarketRequest, CreateMarketResponse,
    ResolveRequest, HealthResponse,
    AddLiquidityRequest, AddLiquidityResponse,
    UpdateMetadataRequest,
    AddRepoRequest, TrackedRepoResponse, WebhookResponse,
)
from core.auth import (
    AuthStore, validate_github_token,
    start_device_flow, poll_device_flow,
)
from core.lmsr import max_loss, prices as lmsr_prices, cost_to_move_price
from core.market_engine import MarketEngine
from core.middleware import AuthUser, AdminDep, require_auth, rate_limiter
from core.models import ZERO, TrackedRepo, reset_counters
from core.persistence import save_snapshot, load_snapshot
from core.risk_engine import RiskEngine, InsufficientBalance

logger = logging.getLogger(__name__)


STATE_PATH = os.environ.get("FUTARCHY_STATE", "./futarchy_state.json")
INITIAL_CREDITS = Decimal(os.environ.get("INITIAL_CREDITS", "100"))
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
TREASURY_ACCOUNT_ID = os.environ.get("FUTARCHY_TREASURY_ID", "")
GITHUB_OAUTH_REDIRECT_URI = os.environ.get(
    "GITHUB_OAUTH_REDIRECT_URI",
    "https://api.futarchy.ai/v1/auth/callback",
)
DASHBOARD_URL = os.environ.get(
    "FUTARCHY_DASHBOARD_URL",
    "https://api.futarchy.ai/dashboard",
)
GITHUB_OAUTH_STATE_TTL = timedelta(minutes=10)

# Liquidity settings (matching pr-market.yml defaults)
LIQUIDITY_INITIAL = os.environ.get("LIQUIDITY_INITIAL", "40")
LIQUIDITY_STEP = os.environ.get("LIQUIDITY_STEP", "40")
LIQUIDITY_RAMP_STEPS = int(os.environ.get("LIQUIDITY_RAMP_STEPS", "4"))
LIQUIDITY_RAMP_INTERVAL_MINUTES = int(os.environ.get("LIQUIDITY_RAMP_INTERVAL_MINUTES", "30"))
LIQUIDITY_BUDGET = os.environ.get("LIQUIDITY_BUDGET", "200")
MARKET_EXPIRY_CHECK_INTERVAL_SECONDS = float(
    os.environ.get("MARKET_EXPIRY_CHECK_INTERVAL_SECONDS", "60")
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load state
    if os.path.exists(STATE_PATH):
        risk, me, auth_store, tracked_repos = load_snapshot(STATE_PATH)
    else:
        reset_counters()
        risk = RiskEngine()
        me = MarketEngine(risk)
        auth_store = AuthStore()
        tracked_repos = {}

    app.state.risk = risk
    app.state.me = me
    app.state.auth_store = auth_store or AuthStore()
    app.state.tracked_repos = tracked_repos
    app.state.github_oauth_states = {}
    app.state.lock = asyncio.Lock()
    await _reconcile_expired_markets_once()

    app.state.expiry_stop_event = asyncio.Event()
    app.state.expiry_task = None
    if MARKET_EXPIRY_CHECK_INTERVAL_SECONDS > 0:
        app.state.expiry_task = asyncio.create_task(
            _expired_market_reconciler(app.state.expiry_stop_event)
        )

    try:
        yield
    finally:
        app.state.expiry_stop_event.set()
        expiry_task = getattr(app.state, "expiry_task", None)
        if expiry_task is not None:
            await expiry_task


app = FastAPI(title="Futarchy API", version="0.2.0", lifespan=lifespan)
app.add_exception_handler(APIError, api_error_handler)


def _save():
    """Save state to disk. Called after every mutation."""
    save_snapshot(app.state.risk, app.state.me, STATE_PATH,
                  auth_store=app.state.auth_store,
                  tracked_repos=app.state.tracked_repos)


def _outcome_from_reason(reason: str) -> str | None:
    for prefix in (
        "lock:position:",
        "increase_lock:position:",
        "decrease_lock:position:",
    ):
        if reason.startswith(prefix):
            return reason[len(prefix):]
    return None


def _tx_outcome(tx, market) -> str | None:
    if tx.trade_id is not None and market is not None:
        for trade in market.trades:
            if trade.id == tx.trade_id:
                return trade.outcome
    return _outcome_from_reason(tx.reason)


def _activity_summary(tx, market, outcome: str | None) -> str:
    reason = tx.reason
    outcome_label = outcome.upper() if outcome else "position"

    if reason == "mint":
        return "Initial credits"

    if reason.startswith("lock:position:"):
        return f"Bought {outcome_label}"
    if reason.startswith("increase_lock:position:"):
        return f"Bought more {outcome_label}"
    if reason.startswith("decrease_lock:position:"):
        if market is not None and market.status == "void":
            return f"Void refund for {outcome_label}"
        return f"Released {outcome_label} collateral"

    if reason == "lock:conditional_loss":
        return "Sale loss reserved"
    if reason == "increase_lock:conditional_loss":
        return "Additional sale loss reserved"
    if reason == "decrease_lock:conditional_loss":
        if market is not None and market.status == "void":
            return "Void refund"
        return "Loss offset released"

    if reason == "trade_pnl:in":
        return "Sale profit reserved"
    if reason == "trade_pnl:out":
        return "Sale profit paid out"
    if reason == "pnl_net:in":
        return "Loss offset received"
    if reason == "pnl_net:out":
        return "Profit offset returned"
    if reason == "void_return_cp:out":
        return "Void profit return"
    if reason == "void_return_cp:in":
        return "Void profit reclaimed"

    if reason == "settlement":
        if market is not None and market.status == "void":
            return "Void settlement"
        if market is not None and market.status == "resolved":
            if tx.available_delta > ZERO:
                if outcome and market.resolution == outcome:
                    return f"Resolved {outcome_label} payout"
                return "Resolved market payout"
            return "Resolved market loss"
        return "Market settlement"

    return reason.replace("_", " ").replace(":", " ")


def _build_account_activity(account_id: int) -> list[AccountActivityEntry]:
    account_txs = [
        tx for tx in app.state.risk.transactions
        if tx.account_id == account_id
    ]
    available = ZERO
    frozen = ZERO
    entries: list[AccountActivityEntry] = []

    for tx in account_txs:
        available += tx.available_delta
        frozen += tx.frozen_delta
        market = app.state.me.markets.get(tx.market_id) if tx.market_id else None
        outcome = _tx_outcome(tx, market)
        total_delta = tx.available_delta + tx.frozen_delta
        entries.append(
            AccountActivityEntry(
                tx_id=tx.id,
                created_at=tx.created_at,
                summary=_activity_summary(tx, market, outcome),
                reason=tx.reason,
                outcome=outcome,
                available_delta=str(tx.available_delta),
                frozen_delta=str(tx.frozen_delta),
                total_delta=str(total_delta),
                available_after=str(available),
                frozen_after=str(frozen),
                total_after=str(available + frozen),
                market_id=tx.market_id,
                market_question=market.question if market else None,
                market_status=market.status if market else None,
                market_resolution=market.resolution if market else None,
                trade_id=tx.trade_id,
                lock_id=tx.lock_id,
            )
        )

    entries.reverse()
    return entries


def _github_oauth_states() -> dict[str, datetime]:
    states = getattr(app.state, "github_oauth_states", None)
    if states is None:
        states = {}
        app.state.github_oauth_states = states
    return states


def _prune_github_oauth_states(now: datetime | None = None) -> None:
    current = now or datetime.now(timezone.utc)
    cutoff = current - GITHUB_OAUTH_STATE_TTL
    states = _github_oauth_states()
    expired = [
        state
        for state, created_at in states.items()
        if created_at <= cutoff
    ]
    for state in expired:
        states.pop(state, None)


async def _exchange_github_oauth_code(code: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
            },
            headers={"Accept": "application/json"},
            timeout=10.0,
        )

    if resp.status_code != 200:
        raise ValueError(f"github_api_error:{resp.status_code}")

    data = resp.json()
    if "error" in data:
        raise ValueError(f"github_api_error:{data['error']}")

    access_token = data.get("access_token")
    if not access_token:
        raise ValueError("github_api_error:missing_access_token")

    return access_token


async def _authenticate_github_identity(gh: dict) -> AuthResponse:
    async with app.state.lock:
        auth_store = app.state.auth_store
        existing = auth_store.get_by_github_id(gh["id"])

        if existing:
            user, raw_key = auth_store.create_user(
                gh["id"], gh["login"], existing.account_id)
        else:
            acc = app.state.risk.create_account()
            if INITIAL_CREDITS > ZERO:
                app.state.risk.mint(acc.id, INITIAL_CREDITS)
            user, raw_key = auth_store.create_user(
                gh["id"], gh["login"], acc.id)

        _save()

    return AuthResponse(
        api_key=raw_key,
        account_id=user.account_id,
        github_login=user.github_login,
    )


def _parse_deadline(deadline: str | None) -> datetime | None:
    if not deadline:
        return None

    normalized = deadline
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        logger.warning("Skipping market with invalid deadline: %s", deadline)
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def _reconcile_expired_markets_once(
    now: datetime | None = None,
) -> list[int]:
    current = now or datetime.now(timezone.utc)
    expired_ids: list[int] = []

    async with app.state.lock:
        for market in list(app.state.me.markets.values()):
            if market.status != "open":
                continue

            deadline = _parse_deadline(market.deadline)
            if deadline is None or deadline > current:
                continue

            try:
                app.state.me.void(market.id)
                expired_ids.append(market.id)
            except ValueError:
                continue

        if expired_ids:
            _save()

    if expired_ids:
        logger.info("Voided %d expired markets: %s", len(expired_ids), expired_ids)

    return expired_ids


async def _expired_market_reconciler(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await _reconcile_expired_markets_once()
        except Exception:
            logger.exception("Expired market reconciliation failed")

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=MARKET_EXPIRY_CHECK_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            continue


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


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
    auth_store = app.state.auth_store
    return HealthResponse(
        status="ok",
        markets=len(app.state.me.markets),
        ledger_accounts=len(app.state.risk.accounts),
        users=(
            len(auth_store.users) +
            len(getattr(auth_store, "local_users", {}))
        ),
    )


# ---------------------------------------------------------------------------
# Auth (no API key required)
# ---------------------------------------------------------------------------

@app.get("/v1/auth/github/login")
async def auth_github_login(prompt: str | None = None) -> RedirectResponse:
    """Start GitHub OAuth web flow."""
    if not GITHUB_CLIENT_ID:
        raise APIError(501, "github_oauth_unavailable",
                       "GITHUB_CLIENT_ID not configured")
    if prompt is not None and prompt != "select_account":
        raise APIError(400, "github_oauth_invalid_prompt",
                       "Unsupported OAuth prompt")

    state = secrets.token_urlsafe(32)
    async with app.state.lock:
        _prune_github_oauth_states()
        _github_oauth_states()[state] = datetime.now(timezone.utc)

    params = {
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": GITHUB_OAUTH_REDIRECT_URI,
        "state": state,
    }
    if prompt:
        params["prompt"] = prompt
    return RedirectResponse(
        url=f"https://github.com/login/oauth/authorize?{urlencode(params)}",
        status_code=302,
    )


@app.get("/v1/auth/callback")
async def auth_github_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Finish GitHub OAuth web flow and redirect to the dashboard."""
    if error:
        raise APIError(400, "github_oauth_denied",
                       f"GitHub authorization failed: {error}")
    if not code or not state:
        raise APIError(400, "github_oauth_invalid_request",
                       "Missing OAuth code or state")
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        raise APIError(501, "github_oauth_unavailable",
                       "GitHub OAuth not fully configured")

    async with app.state.lock:
        _prune_github_oauth_states()
        issued_at = _github_oauth_states().pop(state, None)

    if issued_at is None:
        raise APIError(400, "github_oauth_invalid_state",
                       "Invalid or expired OAuth state")

    try:
        access_token = await _exchange_github_oauth_code(code)
        gh = await validate_github_token(access_token)
    except ValueError as e:
        raise APIError(502, "github_api_error", str(e))

    auth = await _authenticate_github_identity(gh)
    fragment = urlencode({
        "auth": auth.api_key,
        "account_id": auth.account_id,
        "login": auth.github_login,
    })
    return RedirectResponse(url=f"{DASHBOARD_URL}#{fragment}", status_code=302)


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

    return await _authenticate_github_identity(gh)


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


@app.get("/v1/me/activity")
async def get_my_activity(
    user: AuthUser,
    limit: int = Query(50, ge=1, le=200),
    before_tx_id: int | None = Query(None, ge=1),
) -> AccountActivityPage:
    """Get authenticated user's account activity with cursor pagination."""
    entries = _build_account_activity(user.account_id)
    if before_tx_id is not None:
        entries = [entry for entry in entries if entry.tx_id < before_tx_id]

    page_entries = entries[:limit]
    has_more = len(entries) > limit
    next_before_tx_id = page_entries[-1].tx_id if has_more and page_entries else None
    return AccountActivityPage(
        entries=page_entries,
        has_more=has_more,
        next_before_tx_id=next_before_tx_id,
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


@app.post("/v1/admin/service-accounts")
async def admin_create_service_account(
        req: CreateServiceAccountRequest, _: AdminDep
) -> CreateServiceAccountResponse:
    """Create a service account (bot/agent) with a username and API key.

    Optionally mint initial credits. Returns the raw API key once.
    """
    username = req.username.strip()
    if not username or len(username) > 40:
        raise APIError(400, "invalid_username",
                       "Username must be 1-40 characters")

    async with app.state.lock:
        auth_store = app.state.auth_store
        if username in auth_store.local_users:
            raise APIError(409, "username_taken",
                           f"Username '{username}' is already taken")

        acc = app.state.risk.create_account()

        if req.initial_credits:
            try:
                amount = Decimal(req.initial_credits)
            except InvalidOperation:
                raise APIError(400, "invalid_amount",
                               f"Invalid credits: {req.initial_credits}")
            if amount > ZERO:
                app.state.risk.mint(acc.id, amount)

        import hashlib
        import secrets
        raw_key = secrets.token_urlsafe(32)
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

        from core.auth import User, _now
        user = User(
            github_id=0,
            github_login=username,
            account_id=acc.id,
            api_key_hash=key_hash,
        )
        auth_store.local_users[username] = user
        auth_store.key_to_user[key_hash] = user

        _save()

    return CreateServiceAccountResponse(
        account_id=acc.id,
        username=username,
        api_key=raw_key,
    )


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
    import math as _math

    if req.funding is not None and req.b is not None:
        raise APIError(400, "invalid_request",
                       "Provide either 'b' or 'funding', not both")

    n_outcomes = len(req.outcomes) if req.outcomes else 2

    if req.funding is not None:
        try:
            funding = Decimal(req.funding)
        except InvalidOperation:
            raise APIError(400, "invalid_amount",
                           f"Invalid funding: {req.funding}")
        if funding <= ZERO:
            raise APIError(400, "invalid_amount", "Funding must be positive")
        # b = funding / ln(n)
        b = funding / Decimal(str(_math.log(n_outcomes)))
    else:
        b_str = req.b or "100"
        try:
            b = Decimal(b_str)
        except InvalidOperation:
            raise APIError(400, "invalid_amount", f"Invalid b: {b_str}")

    async with app.state.lock:
        try:
            market, amm = app.state.me.create_market(
                question=req.question,
                category=req.category,
                category_id=req.category_id,
                metadata=req.metadata,
                b=b,
                outcomes=req.outcomes,
                deadline=req.deadline,
                funding_account_id=req.funding_account_id,
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


@app.patch("/v1/admin/markets/{market_id}/status")
async def admin_override_status(market_id: int, req: dict,
                                _: AdminDep) -> dict:
    """Admin override: correct a market's status.

    Only allowed on markets with 0 trades (no settlement reversal needed).
    Accepts {"status": "void"} to correct a wrongly-resolved market.
    """
    m = app.state.me.markets.get(market_id)
    if m is None:
        raise APIError(404, "market_not_found", f"Market {market_id} not found")

    new_status = req.get("status")
    if new_status not in ("void", "resolved", "open"):
        raise APIError(400, "invalid_status",
                       "Status must be 'void', 'resolved', or 'open'")

    if len(m.trades) > 0:
        raise APIError(409, "has_trades",
                       f"Market {market_id} has {len(m.trades)} trades; "
                       "status override not safe without settlement reversal")

    async with app.state.lock:
        old_status = m.status
        old_resolution = m.resolution
        m.status = new_status
        if new_status == "void":
            m.resolution = None

        # Record in the ledger — every state change must be auditable.
        from core.models import Transaction
        tx = Transaction.new(
            account_id=m.amm_account_id,
            available_delta=ZERO,
            frozen_delta=ZERO,
            reason="admin_status_override",
            market_id=market_id,
        )
        app.state.risk.transactions.append(tx)

        _save()

    return {"market_id": market_id, "old_status": old_status,
            "old_resolution": old_resolution,
            "new_status": new_status, "tx_id": tx.id}


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


# ---------------------------------------------------------------------------
# Admin: Tracked Repos
# ---------------------------------------------------------------------------

@app.get("/v1/admin/repos")
async def admin_list_repos(_: AdminDep) -> list[TrackedRepoResponse]:
    """List tracked repos."""
    return [
        TrackedRepoResponse(
            repo=r.repo,
            enabled=r.enabled,
            has_webhook_secret=r.webhook_secret is not None,
            added_at=r.added_at,
        )
        for r in app.state.tracked_repos.values()
    ]


@app.post("/v1/admin/repos")
async def admin_add_repo(req: AddRepoRequest, _: AdminDep) -> TrackedRepoResponse:
    """Add a tracked repo for webhook-based PR markets."""
    slug = req.repo.strip().lower()
    if "/" not in slug or len(slug.split("/")) != 2:
        raise APIError(400, "invalid_repo",
                       "Repo must be in 'owner/name' format")

    async with app.state.lock:
        repo = TrackedRepo.new(
            repo=slug,
            webhook_secret=req.webhook_secret,
            enabled=req.enabled,
        )
        app.state.tracked_repos[slug] = repo
        _save()

    return TrackedRepoResponse(
        repo=repo.repo,
        enabled=repo.enabled,
        has_webhook_secret=repo.webhook_secret is not None,
        added_at=repo.added_at,
    )


@app.delete("/v1/admin/repos/{repo_slug:path}")
async def admin_delete_repo(repo_slug: str, _: AdminDep) -> dict:
    """Remove a tracked repo. Use URL-encoded slug (e.g. snapshot-labs%2Fsx-monorepo)."""
    slug = repo_slug.strip().lower()
    async with app.state.lock:
        if slug not in app.state.tracked_repos:
            raise APIError(404, "repo_not_found",
                           f"Repo '{slug}' is not tracked")
        del app.state.tracked_repos[slug]
        _save()

    return {"deleted": slug}


# ---------------------------------------------------------------------------
# GitHub Webhook
# ---------------------------------------------------------------------------

def _verify_webhook_signature(payload: bytes, signature: str,
                              secret: str) -> bool:
    """Verify GitHub HMAC-SHA256 webhook signature."""
    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.post("/v1/hooks/github")
async def github_webhook(request: Request) -> WebhookResponse:
    """Receive GitHub pull_request webhook events for tracked repos."""
    body = await request.body()
    event_type = request.headers.get("x-github-event", "")

    # Ping event — GitHub sends this on webhook creation.
    # Handle before JSON parsing since GitHub may send form-encoded pings.
    if event_type == "ping":
        return WebhookResponse(
            action="pong", skipped=True,
            reason="Webhook configured successfully")

    if event_type != "pull_request":
        return WebhookResponse(
            action="ignored", skipped=True,
            reason=f"Event type '{event_type}' is not pull_request")

    # Parse payload — supports both JSON and form-encoded (payload= field)
    content_type = request.headers.get("content-type", "")
    try:
        if "application/json" in content_type:
            payload = await request.json()
        else:
            # GitHub form-encoded: body is payload=<url-encoded JSON>
            from urllib.parse import parse_qs
            form = parse_qs(body.decode())
            import json as _json
            payload = _json.loads(form["payload"][0])
    except Exception:
        raise APIError(400, "invalid_payload",
                       "Invalid payload. Set webhook content type to "
                       "application/json.")

    action = payload.get("action", "")
    pr = payload.get("pull_request", {})
    repo_full = payload.get("repository", {}).get("full_name", "")
    repo_slug = repo_full.strip().lower()

    # Look up tracked repo
    tracked = app.state.tracked_repos.get(repo_slug)
    if tracked is None:
        raise APIError(404, "repo_not_tracked",
                       f"Repo '{repo_full}' is not tracked")
    if not tracked.enabled:
        return WebhookResponse(
            action=action, skipped=True,
            reason=f"Repo '{repo_full}' is disabled")

    # Validate HMAC signature
    if tracked.webhook_secret:
        signature = request.headers.get("x-hub-signature-256", "")
        if not signature:
            raise APIError(401, "signature_missing",
                           "X-Hub-Signature-256 header required")
        if not _verify_webhook_signature(body, signature,
                                         tracked.webhook_secret):
            raise APIError(401, "signature_invalid",
                           "Webhook signature verification failed")

    # Route by action
    if action == "opened":
        return await _handle_pr_opened(tracked, pr, repo_slug)
    elif action == "closed":
        return await _handle_pr_closed(pr, repo_slug)
    else:
        return WebhookResponse(
            action=action, skipped=True,
            reason=f"Action '{action}' is not handled")


async def _handle_pr_opened(tracked: TrackedRepo, pr: dict,
                            repo_slug: str) -> WebhookResponse:
    """Create a market for a newly opened PR."""
    import math as _math

    pr_num = pr.get("number")
    pr_title = pr.get("title", "")
    pr_url = pr.get("html_url", "")

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    deadline = tomorrow.isoformat().replace("+00:00", "Z")
    next_liquidity = (now + timedelta(minutes=LIQUIDITY_RAMP_INTERVAL_MINUTES)
                      ).strftime("%Y-%m-%dT%H:%M:%SZ")

    category_id = f"{repo_slug}#{pr_num}@{today}"

    # Idempotency: check if market already exists
    for m in app.state.me.markets.values():
        if m.category == "pr_merge" and m.category_id == category_id:
            return WebhookResponse(
                action="opened", market_id=m.id, skipped=True,
                reason=f"Market already exists for {category_id}")

    question = f"Will PR #{pr_num} '{pr_title}' merge by {deadline}?"
    funding = Decimal(LIQUIDITY_INITIAL)
    b = funding / Decimal(str(_math.log(2)))

    # Determine funding source
    funding_account_id = int(TREASURY_ACCOUNT_ID) if TREASURY_ACCOUNT_ID else None

    metadata = {
        "pr_number": pr_num,
        "pr_url": pr_url,
        "repo": repo_slug,
        "liquidity_budget": LIQUIDITY_BUDGET,
        "liquidity_step": LIQUIDITY_STEP,
        "liquidity_steps_remaining": LIQUIDITY_RAMP_STEPS,
        "next_liquidity_at": next_liquidity,
    }

    async with app.state.lock:
        try:
            market, amm = app.state.me.create_market(
                question=question,
                category="pr_merge",
                category_id=category_id,
                metadata=metadata,
                b=b,
                deadline=deadline,
                funding_account_id=funding_account_id,
            )
        except (ValueError, InsufficientBalance) as e:
            raise translate_engine_error(e)
        _save()

    logger.info("Webhook created market %d for %s", market.id, category_id)
    return WebhookResponse(action="opened", market_id=market.id)


async def _handle_pr_closed(pr: dict, repo_slug: str) -> WebhookResponse:
    """Resolve all open markets for a closed PR."""
    pr_num = pr.get("number")
    merged = pr.get("merged", False)
    outcome = "yes" if merged else "no"
    category_prefix = f"{repo_slug}#{pr_num}"

    resolved_ids = []
    async with app.state.lock:
        for m in list(app.state.me.markets.values()):
            if (m.category == "pr_merge"
                    and m.category_id.startswith(category_prefix)
                    and m.status == "open"):
                try:
                    app.state.me.resolve(m.id, outcome)
                    resolved_ids.append(m.id)
                except ValueError:
                    pass  # already resolved/void
        if resolved_ids:
            _save()

    if not resolved_ids:
        return WebhookResponse(
            action="closed", skipped=True, resolution=outcome,
            reason=f"No open markets found for {category_prefix}")

    logger.info("Webhook resolved %d markets for %s as %s",
                len(resolved_ids), category_prefix, outcome)
    return WebhookResponse(
        action="closed", market_id=resolved_ids[0], resolution=outcome)
