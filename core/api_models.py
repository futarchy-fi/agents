"""
Pydantic request/response models for the API.
All monetary values are strings to avoid IEEE 754 issues.
"""

from pydantic import BaseModel


# --- Auth ---

class GitHubAuthRequest(BaseModel):
    github_token: str

class DeviceFlowStartRequest(BaseModel):
    pass

class DeviceFlowPollRequest(BaseModel):
    device_code: str

class AuthResponse(BaseModel):
    api_key: str
    account_id: int
    github_login: str

class RegisterRequest(BaseModel):
    username: str

class RegisterResponse(BaseModel):
    api_key: str
    account_id: int
    username: str

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
    num_trades: int
    resolution: str | None
    created_at: str

class MarketDetail(MarketSummary):
    amm_account_id: int
    q: dict[str, str]
    volume: str
    deadline: str | None
    resolved_at: str | None
    metadata: dict

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

class HealthResponse(BaseModel):
    status: str
    markets: int
    accounts: int
