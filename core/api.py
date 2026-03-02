"""
FastAPI application. Agents-first HTTP API for the futarchy prediction market.

Public endpoints (no auth): health, markets, market detail, positions, trades.
User endpoints (API key): /me, buy, sell.
Admin endpoints (admin key): mint, create market, resolve, void.
"""

import asyncio
import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from math import log

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse

from core.api_errors import APIError, api_error_handler, translate_engine_error
from core.api_models import (
    GitHubAuthRequest, AuthResponse,
    RegisterRequest, RegisterResponse,
    DeviceFlowStartRequest, DeviceFlowResponse, DeviceFlowPollRequest,
    RegisterWebhookRepoRequest, WebhookRepoResponse,
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
from core.lmsr import max_loss, prices as lmsr_prices, cost_to_move_price
from core.market_engine import MarketEngine
from core.middleware import AuthUser, AdminDep, require_auth, rate_limiter
from core.models import ZERO, reset_counters
from core.persistence import save_snapshot, load_snapshot, load_webhook_repos
from core.risk_engine import RiskEngine, InsufficientBalance


STATE_PATH = os.environ.get("FUTARCHY_STATE", "./futarchy_state.json")
INITIAL_CREDITS = Decimal(os.environ.get("INITIAL_CREDITS", "100"))
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_repo_name(name: str) -> str:
    return name.strip().lower()


def _repo_name_from_payload(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return None
    repo = payload.get("repository")
    if isinstance(repo, dict):
        full_name = repo.get("full_name")
        if isinstance(full_name, str) and full_name.strip():
            return _normalize_repo_name(full_name)

    # Fallback for payloads lacking top-level repository block.
    pr = payload.get("pull_request")
    if isinstance(pr, dict):
        repo = pr.get("base", {}).get("repo")
        if isinstance(repo, dict):
            full_name = repo.get("full_name")
            if isinstance(full_name, str) and full_name.strip():
                return _normalize_repo_name(full_name)
    return None


def _resolve_webhook_secret(repo: dict) -> str | None:
    secret_env = repo.get("secret_env")
    if isinstance(secret_env, str) and secret_env.strip():
        return os.environ.get(secret_env)
    return repo.get("secret")


def _verify_signature(secret: str, body: bytes, signature: str) -> bool:
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature[7:])


def _public_webhook_repo(cfg: dict) -> dict:
    return {
        "name": cfg["name"],
        "enabled": cfg.get("enabled", True),
        "category": cfg.get("category", "pr_merge"),
        "funding": cfg.get("funding"),
        "b": cfg.get("b"),
        "deadline_hours": cfg.get("deadline_hours", 24),
        "outcomes": cfg.get("outcomes", ["yes", "no"]),
        "question_template": cfg.get("question_template",
                                   "Will PR #{pr_number} '{pr_title}' "
                                   "merge by {deadline}?"),
        "metadata": cfg.get("metadata", {}),
        "secret_env": cfg.get("secret_env"),
        "has_secret": bool(cfg.get("secret")) or bool(cfg.get("secret_env")),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load state
    if os.path.exists(STATE_PATH):
        risk, me, auth_store = load_snapshot(STATE_PATH)
        webhook_repos = load_webhook_repos(STATE_PATH)
    else:
        reset_counters()
        risk = RiskEngine()
        me = MarketEngine(risk)
        auth_store = AuthStore()
        webhook_repos = {}

    app.state.risk = risk
    app.state.me = me
    app.state.auth_store = auth_store or AuthStore()
    app.state.webhook_repos = webhook_repos
    app.state.lock = asyncio.Lock()
    yield


app = FastAPI(title="Futarchy API", version="0.2.0", lifespan=lifespan)
app.add_exception_handler(APIError, api_error_handler)


def _save():
    """Save state to disk. Called after every mutation."""
    save_snapshot(app.state.risk, app.state.me, STATE_PATH,
                  auth_store=app.state.auth_store,
                  webhook_repos=app.state.webhook_repos)


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _format_webhook_question(template: str, repo_name: str,
                            pull_request: dict) -> str:
    title = pull_request.get("title", "")
    title_snippet = title if len(title) < 180 else title[:180] + "…"
    mapping = {
        "repo_name": repo_name,
        "pr_number": pull_request.get("number", ""),
        "pr_title": title_snippet,
        "pr_url": pull_request.get("html_url", ""),
        "author": (pull_request.get("user") or {}).get("login", ""),
        "head_ref": ((pull_request.get("head") or {}).get("ref", "")),
        "base_ref": ((pull_request.get("base") or {}).get("ref", "")),
        "deadline": _now_iso(),
    }

    class _QuestionMap(dict):
        def __missing__(self, key):
            return f"{{{key}}}"

    return template.format_map(_QuestionMap(mapping))


def _webhook_deadline(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _webhook_outcomes(repo_cfg: dict) -> list[str]:
    outcomes = repo_cfg.get("outcomes")
    if isinstance(outcomes, list) and outcomes:
        cleaned = [str(x).strip() for x in outcomes if str(x).strip()]
        if len(cleaned) >= 2:
            return cleaned
    return ["yes", "no"]


def _webhook_market_b(repo_cfg: dict) -> Decimal:
    funding = repo_cfg.get("funding")
    outcomes = _webhook_outcomes(repo_cfg)
    if funding:
        funding_dec = Decimal(funding)
        if funding_dec <= ZERO:
            raise APIError(400, "invalid_request",
                           "Webhook funding must be positive")
        return funding_dec / Decimal(str(log(len(outcomes))))

    b = repo_cfg.get("b", "100")
    b_dec = Decimal(str(b))
    if b_dec <= ZERO:
        raise APIError(400, "invalid_request", "Webhook b must be positive")
    return b_dec


def _find_open_market_for_category(category_id: str):
    for m in app.state.me.markets.values():
        if m.category_id == category_id and m.status == "open":
            return m
    return None


def _find_any_market_for_category(category_id: str):
    for m in app.state.me.markets.values():
        if m.category_id == category_id:
            return m
    return None


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
            # New user: create account, mint initial credits
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


# ---------------------------------------------------------------------------
# Webhook registration (admin-only)
# ---------------------------------------------------------------------------

@app.get("/v1/admin/webhook-repos")
async def list_webhook_repos(_: AdminDep) -> list[WebhookRepoResponse]:
    repos = sorted(
        app.state.webhook_repos.values(),
        key=lambda r: r["name"],
    )
    return [WebhookRepoResponse(**_public_webhook_repo(r)) for r in repos]


@app.post("/v1/admin/webhook-repos")
async def register_webhook_repo(req: RegisterWebhookRepoRequest,
                               _: AdminDep) -> WebhookRepoResponse:
    repo_name = _normalize_repo_name(req.repo_name)
    if not repo_name:
        raise APIError(400, "invalid_request", "repo_name is required")

    existing = app.state.webhook_repos.get(repo_name, {})

    if req.secret and req.secret_env:
        raise APIError(
            400, "invalid_request",
            "Provide either secret or secret_env, not both")

    secret = req.secret
    secret_env = req.secret_env
    if secret_env is not None:
        secret_env = secret_env.strip()
        if not secret_env:
            raise APIError(400, "invalid_request",
                           "secret_env cannot be empty")
        if not os.environ.get(secret_env):
            raise APIError(400, "invalid_request",
                           f"Secret env var '{secret_env}' is not set")
        secret = None

    if secret is None and not secret_env:
        if not existing:
            raise APIError(400, "invalid_request",
                           "Provide either secret or secret_env")
        secret = existing.get("secret")
        secret_env = existing.get("secret_env")

    # Keep compatibility for updates that only toggle enabled/metadata.
    outcomes = req.outcomes if req.outcomes is not None else existing.get(
        "outcomes", ["yes", "no"])
    if not outcomes or len(outcomes) < 2:
        raise APIError(400, "invalid_request", "outcomes cannot be empty")

    funding = req.funding if req.funding is not None else existing.get("funding")
    b = req.b if req.b is not None else existing.get("b")
    if funding is not None and b is not None:
        raise APIError(400, "invalid_request",
                       "Provide either 'funding' or 'b', not both")

    try:
        if funding is not None:
            if Decimal(str(funding)) <= ZERO:
                raise InvalidOperation("zero")
        elif b is not None:
            if Decimal(str(b)) <= ZERO:
                raise InvalidOperation("zero")
        elif not existing:
            # New repo requires at least one funding parameter.
            req_b = Decimal(str(req.b or "100"))
            b = str(req_b)
    except InvalidOperation:
        raise APIError(400, "invalid_request", "funding/b must be numeric")

    deadline_hours = req.deadline_hours
    if deadline_hours <= 0:
        raise APIError(400, "invalid_request", "deadline_hours must be positive")

    metadata = req.metadata if req.metadata is not None else existing.get("metadata", {})
    if not isinstance(metadata, dict):
        raise APIError(400, "invalid_request", "metadata must be an object")

    repo_cfg = {
        "name": repo_name,
        "enabled": req.enabled,
        "secret": secret,
        "secret_env": secret_env,
        "category": req.category,
        "funding": str(funding) if funding is not None else None,
        "b": str(b) if b is not None else None,
        "deadline_hours": req.deadline_hours,
        "outcomes": outcomes,
        "question_template": req.question_template,
        "metadata": metadata,
    }
    if not req.category:
        repo_cfg["category"] = "pr_merge"

    if existing:
        # Preserve secret fallback when only toggling enabled/metadata.
        if secret is None and not secret_env:
            repo_cfg["secret"] = repo_cfg["secret"] or existing.get("secret")
            repo_cfg["secret_env"] = repo_cfg["secret_env"] or existing.get("secret_env")

    async with app.state.lock:
        app.state.webhook_repos[repo_name] = repo_cfg
        _save()

    return WebhookRepoResponse(**_public_webhook_repo(repo_cfg))


@app.delete("/v1/admin/webhook-repos/{repo_name:path}")
async def delete_webhook_repo(repo_name: str, _: AdminDep) -> dict:
    key = _normalize_repo_name(repo_name)
    if key not in app.state.webhook_repos:
        raise APIError(404, "webhook_repo_not_found",
                       f"Repo '{key}' is not registered")

    async with app.state.lock:
        app.state.webhook_repos.pop(key, None)
        _save()

    return {"name": key, "status": "deleted"}


@app.post("/v1/webhooks/github")
async def webhook_receiver(request: Request) -> dict:
    event = request.headers.get("X-GitHub-Event", "").lower()
    if event != "pull_request":
        raise APIError(400, "unsupported_event",
                       "Only pull_request events are supported")

    body = await request.body()
    if not body:
        raise APIError(400, "invalid_request", "Request body is empty")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise APIError(400, "invalid_payload", "Invalid JSON payload")

    if not isinstance(payload, dict):
        raise APIError(400, "invalid_payload", "Payload must be a JSON object")

    repo_name = _repo_name_from_payload(payload)
    if not repo_name:
        raise APIError(400, "invalid_payload",
                       "Missing repository name in payload")

    repo = app.state.webhook_repos.get(repo_name)
    if repo is None or not repo.get("enabled", True):
        raise APIError(403, "repo_not_approved",
                       f"Repository '{repo_name}' is not approved")

    secret = _resolve_webhook_secret(repo)
    if not secret:
        raise APIError(500, "webhook_secret_missing",
                       f"No webhook secret configured for '{repo_name}'")

    signature = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_signature(secret, body, signature):
        raise APIError(403, "invalid_signature", "Invalid webhook signature")

    action = payload.get("action")
    if action not in {"opened", "reopened", "synchronize", "closed"}:
        return {"status": "ignored", "action": action}

    pr = payload.get("pull_request")
    if not isinstance(pr, dict):
        raise APIError(400, "invalid_payload", "Missing pull_request payload")

    try:
        pr_number = int(pr.get("number"))
    except (TypeError, ValueError):
        raise APIError(400, "invalid_payload", "pull_request.number is required")

    category_id = f"{repo_name}#{pr_number}"

    if action == "closed":
        merged = bool(pr.get("merged"))
        outcome = "yes" if merged else "no"

        async with app.state.lock:
            market = _find_open_market_for_category(category_id)
            if market is None:
                return {
                    "status": "ignored",
                    "action": action,
                    "reason": "no_open_market",
                    "category_id": category_id,
                }

            if outcome not in market.outcomes:
                raise APIError(400, "invalid_request",
                               f"Configured outcomes do not include '{outcome}'")
            try:
                app.state.me.resolve(market.id, outcome)
                _save()
            except (ValueError, InsufficientBalance) as e:
                raise translate_engine_error(e)

        return {
            "status": "resolved",
            "market_id": market.id,
            "outcome": outcome,
        }

    existing_market = _find_any_market_for_category(category_id)
    if existing_market is not None:
        if existing_market.status == "open":
            return {
                "status": "exists",
                "market_id": existing_market.id,
                "category_id": category_id,
            }
        return {
            "status": "ignored",
            "action": action,
            "reason": "market_already_closed",
            "category_id": category_id,
            "market_id": existing_market.id,
        }

    outcomes = _webhook_outcomes(repo)
    question = _format_webhook_question(
        repo.get("question_template", ""),
        repo_name,
        pr,
    )
    metadata = dict(repo.get("metadata") or {})
    deadline = _webhook_deadline(int(repo.get("deadline_hours", 24)))
    metadata["webhook"] = {
        "repo": repo_name,
        "event": action,
        "pull_request": {
            "number": pr_number,
            "title": pr.get("title", ""),
            "state": pr.get("state"),
            "url": pr.get("html_url"),
            "deadline": deadline,
        },
    }

    try:
        b = _webhook_market_b(repo)
    except (InvalidOperation, ValueError) as e:
        raise APIError(400, "invalid_request", str(e))

    async with app.state.lock:
        try:
            market, _ = app.state.me.create_market(
                question=question,
                category=repo.get("category", "pr_merge"),
                category_id=category_id,
                metadata=metadata,
                b=b,
                outcomes=outcomes,
                deadline=deadline,
            )
            _save()
        except (ValueError, InsufficientBalance) as e:
            raise translate_engine_error(e)

    return {
        "status": "created",
        "market_id": market.id,
        "category_id": category_id,
    }


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
