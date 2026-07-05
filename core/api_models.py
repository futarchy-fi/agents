"""
Pydantic request/response models for the API.
All monetary values are strings to avoid IEEE 754 issues.
"""

from pydantic import BaseModel


# --- Auth ---

class DeviceFlowStartRequest(BaseModel):
    pass

class DeviceFlowPollRequest(BaseModel):
    device_code: str

class AuthResponse(BaseModel):
    api_key: str
    account_id: int
    github_login: str

class DeviceFlowResponse(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


# --- Account ---

class LockResponse(BaseModel):
    lock_id: int
    market_id: int
    amount: str
    lock_type: str

class AccountResponse(BaseModel):
    account_id: int
    available: str
    frozen: str
    total: str
    locks: list[LockResponse]

class AccountActivityEntry(BaseModel):
    tx_id: int
    created_at: str
    summary: str
    reason: str
    outcome: str | None = None
    available_delta: str
    frozen_delta: str
    total_delta: str
    available_after: str
    frozen_after: str
    total_after: str
    market_id: int | None = None
    market_question: str | None = None
    market_status: str | None = None
    market_resolution: str | None = None
    trade_id: int | None = None
    lock_id: int | None = None

class AccountActivityPage(BaseModel):
    entries: list[AccountActivityEntry]
    has_more: bool
    next_before_tx_id: int | None = None


# --- Markets ---

class MarketSummary(BaseModel):
    market_id: int
    question: str
    category: str
    category_id: str
    status: str
    outcomes: list[str]
    prices: dict[str, str]
    b: str
    liquidity: str  # max market maker loss = b * ln(n), the funding amount
    num_trades: int
    resolution: str | None
    created_at: str
    deadline: str | None = None
    resolved_at: str | None = None

class MarketDetail(MarketSummary):
    amm_account_id: int
    q: dict[str, str]
    volume: str
    resolved_at: str | None
    metadata: dict

class DepthEntry(BaseModel):
    target: str       # e.g. "60%"
    outcome: str      # e.g. "yes"
    cost: str         # credits to reach target
    shares: str       # tokens to buy

class DepthResponse(BaseModel):
    market_id: int
    rows: list[DepthEntry]

class PositionEntry(BaseModel):
    account_id: int
    positions: dict[str, str]
    locks: list[LockResponse]

class TradeResponse(BaseModel):
    trade_id: int
    market_id: int
    outcome: str
    amount: str
    price: str
    value: str
    buyer_account_id: int
    seller_account_id: int
    created_at: str


# --- Trading ---

class BuyRequest(BaseModel):
    outcome: str
    budget: str

class SellRequest(BaseModel):
    outcome: str
    amount: str

class TradeResult(BaseModel):
    trade_id: int
    outcome: str
    amount: str
    price: str
    value: str


# --- Admin ---

class CreateAccountResponse(BaseModel):
    account_id: int

class CreateServiceAccountRequest(BaseModel):
    username: str
    initial_credits: str | None = None

class CreateServiceAccountResponse(BaseModel):
    account_id: int
    username: str
    api_key: str

class MintRequest(BaseModel):
    account_id: int
    amount: str

class MintResponse(BaseModel):
    account_id: int
    available: str

class CreateMarketRequest(BaseModel):
    question: str
    category: str
    category_id: str
    b: str | None = None
    funding: str | None = None
    funding_account_id: int | None = None
    outcomes: list[str] | None = None
    deadline: str | None = None
    metadata: dict = {}

class CreateMarketResponse(BaseModel):
    market_id: int
    amm_account_id: int
    b: str

class ResolveRequest(BaseModel):
    outcome: str

class AddLiquidityRequest(BaseModel):
    amount: str
    funding_account_id: int | None = None

class AddLiquidityResponse(BaseModel):
    market_id: int
    b: str
    funding_added: str

class UpdateMetadataRequest(BaseModel):
    metadata: dict

class NetHealth(BaseModel):
    markets: int
    orders: int
    enabled: bool


# --- Net venue (Plan B: joint/factored market) ---

class NetMarket(BaseModel):
    id: str
    variableId: str
    title: str
    description: str | None = None
    status: str
    outcomes: list[dict]
    marginals: dict[str, float]
    parents: list[str] = []

class NetMarketList(BaseModel):
    markets: list[NetMarket]
    count: int

class NetMarginalResponse(BaseModel):
    variable: str
    context: dict[str, str]
    marginal: dict[str, float]


class NetOrderRequest(BaseModel):
    """Body shared by preview and place. Deliberately has no accountId field
    — the account is always ``user.account_id`` from the Bearer key, never
    client-supplied (see planB-constraints.md's security invariant)."""
    variableId: str
    outcomeId: str
    target: float
    context: dict[str, str] | None = None


class NetOrderPreviewResponse(BaseModel):
    stake: str
    before: float
    after: float
    b: str


class NetOrderBalance(BaseModel):
    available: str
    frozen: str


class NetOrder(BaseModel):
    """A copy of a venue order record — never the live dict by reference."""
    orderId: str
    accountId: int
    variableId: str
    outcomeId: str
    target: float
    context: dict[str, str]
    before: float
    after: float
    stake: str
    lockId: int
    status: str
    fill: dict[str, float]
    remainingContext: dict[str, str]


class NetOrderResponse(NetOrder):
    balance: NetOrderBalance


class NetResolveResponse(BaseModel):
    """Admin resolve response: the venue's settlement report plus the
    market/variable/outcome that were resolved."""
    marketId: str
    variableId: str
    outcome: str
    settled: list[str]
    calledOff: list[str]
    awaiting: list[str]
    treasuryDelta: str


class NetVoidResponse(BaseModel):
    """Admin void response: the venue's call-off report plus the
    market/variable that were voided."""
    marketId: str
    variableId: str
    calledOff: list[str]


class NetOrdersList(BaseModel):
    orders: list[NetOrder]


class NetPortfolioResponse(BaseModel):
    """The caller's net-venue portfolio: own orders plus aggregate stake/pnl.

    Unlike every other ``/v1/net/*`` route, ``GET /v1/me/net`` never 503s
    when the venue is disabled — it returns this same empty shape instead
    (see the route docstring in core/api.py for why)."""
    orders: list[NetOrder]
    openStake: str
    settledPnl: str


# --- Leaderboard (public) ---

class LeaderboardEntry(BaseModel):
    login: str | None
    accountId: int
    total: str


class LeaderboardResponse(BaseModel):
    entries: list[LeaderboardEntry]


class HealthResponse(BaseModel):
    status: str
    markets: int
    ledger_accounts: int
    users: int
    net: NetHealth


# --- Tracked Repos ---

class AddRepoRequest(BaseModel):
    repo: str                          # "snapshot-labs/sx-monorepo"
    webhook_secret: str | None = None  # HMAC secret for signature validation
    enabled: bool = True

class TrackedRepoResponse(BaseModel):
    repo: str
    enabled: bool
    has_webhook_secret: bool
    added_at: str

class WebhookResponse(BaseModel):
    action: str
    market_id: int | None = None
    resolution: str | None = None
    skipped: bool = False
    reason: str | None = None
