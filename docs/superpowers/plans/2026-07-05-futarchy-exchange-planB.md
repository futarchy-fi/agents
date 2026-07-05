# Futarchy Exchange — Plan B: Unified Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Expose venue B (the JointVenue built in Plan A) through the existing FastAPI exchange with real auth, credits, and admin controls — so both venues trade in credits via raw HTTP.

**Architecture:** Extend `core/api.py` (FastAPI, `app.state.{risk,me,auth_store,lock}` globals, `_save()` snapshot-per-mutation, `user: AuthUser` dependency for authed routes, `X-Admin-Key`-style admin guard as used by existing `/v1/admin/*`). New `/v1/net/*` route family wraps `app.state.joint` (a `JointVenue`). The venue is OPTIONAL at runtime: no seeds env → routes return 503, keeping tests fast and deploys de-riskable.

**Tech Stack:** FastAPI + pydantic models in `core/api_models.py`, error style `APIError(status, code, message)`, tests via the existing `core/test_api.py` client fixtures (pytest-asyncio; run with `.venv/bin/python -m pytest -q`; baseline 179 passed).

## Global Constraints

- Every mutating `/v1/net` route: `async with app.state.lock`, then venue call, then `_save()` — same discipline as `buy()` (core/api.py:748).
- `_save()`/`save_snapshot` MUST pass `joint_venue=app.state.joint` whenever a venue exists — a forgotten kwarg silently destroys venue state on the next write (final-review hazard). Audit EVERY `save_snapshot` call site in the task that wires the venue.
- Venue order dicts are live internal state — serialize via copies at the API boundary; never hand the internal dict to response models by reference mutation.
- Money is Decimal-as-str in every response. `accountId` in responses only for the caller's own resources.
- Error mapping (exact): `UnknownVariable`/`UnknownMarket` → 404 `unknown_market`; `InvalidTarget` → 400 `invalid_target`; `InsufficientCredits` → 400 `insufficient_credits`; `MarketClosed` → 409 `market_closed`; `ContextContradicted` → 409 `context_contradicted`; `WidthBudgetExceeded` → 422 `width_budget`; venue disabled → 503 `net_venue_disabled`.
- API-level target clamp: reject `target` outside [0.001, 0.999] with 400 `invalid_target` BEFORE touching the venue.
- Baseline 179 tests never regress. New env vars: `EXCHANGE_SEEDS_PATH` (unset = venue off), `JOINT_LIQUIDITY` (default "50"), `JOINT_MAX_WIDTH` (default "8"), `INITIAL_CREDITS` default changes "100" → "1000".
- GCP/deploy/frontend/CLI are Plans C–E — do NOT touch `deploy/`, `static/`, `cli/` in this plan.

## Tasks

### Task B1: Venue lifecycle wiring (app startup, restore, save, health)

**Files:** Modify `core/api.py` (create_app/startup section ~lines 60–130), `core/persistence.py` only if a helper is missing. Test: new `core/test_api_net.py` (module-scoped app fixture with tiny seeds via `EXCHANGE_SEEDS_PATH` pointing at a tmp JSON file written from the TINY_SEEDS dict — import it from `venues.joint.test_venue`).

**Interfaces produced:** `app.state.joint: JointVenue | None`; `_save()` includes the venue; health payload gains `"net": {"markets": int, "orders": int, "enabled": bool}`.

- [ ] Failing tests: (1) app WITHOUT `EXCHANGE_SEEDS_PATH` → `app.state.joint is None`, `GET /v1/health` has `net.enabled == false`; (2) app WITH tiny-seeds path → joint built (2 markets), health `net.enabled true, markets 2`; (3) restart fidelity: place an edit via the venue object directly, trigger `_save()`, rebuild app from the same STATE_PATH + seeds env → marginals and orders survive (venue restored via `JointVenue.from_snapshot` with the venues section from `load_snapshot`).
- [ ] Implement: startup reads env; if seeds path set: load snapshot's `venues.get("joint")` → `JointVenue.from_snapshot(data, risk, seeds_path)` else fresh `JointVenue(risk, seeds_path, liquidity=Decimal(env), max_width=int(env))`. Update `_save()` AND the shutdown save (api.py:125) — grep for every `save_snapshot(` call site in core/ and pass the venue at each.
- [ ] Full suite; commit `feat(api): joint venue lifecycle wiring`.

### Task B2: Net read endpoints

**Files:** Modify `core/api.py`, `core/api_models.py`. Test: `core/test_api_net.py`.

**Interfaces produced:** `GET /v1/net/markets` → `{"markets": [NetMarket], "count": int}` (NetMarket: id, variableId, title, description, status, outcomes, marginals {outcome: float}, parents: [variableId] — parents come from the seed record's cpt keys via the venue's market dict; omit if absent). `GET /v1/net/markets/{market_id}` → NetMarket. `GET /v1/net/marginal?variable=<id>&context=a%3Dyes|b%3Dno` → `{"variable", "context", "marginal": {outcome: float}}` (pipe-separated var=outcome pairs, same encoding bayes used).

- [ ] Failing tests: list returns both tiny markets with live marginals; detail 404s on unknown id; marginal endpoint returns 0.9 for gcx_b given gcx_a=yes; contradicted/unknown context → 409/404 per the global mapping; all three routes → 503 when venue disabled.
- [ ] Implement (read-only routes take the lock only if they touch mutable venue state — `get_market` reads live marginals; a brief `async with app.state.lock` is correct and cheap). Full suite; commit `feat(api): net venue read endpoints`.

### Task B3: Net trading endpoints (authed)

**Files:** Modify `core/api.py`, `core/api_models.py`. Test: `core/test_api_net.py` (mint credits to a test user via the existing test auth fixture pattern — see how test_api.py creates authed users and accounts).

**Interfaces produced:** `POST /v1/net/orders/preview` (authed) `{variableId, outcomeId, target, context?}` → `{"stake": str, "before": float, "after": float, "b": str}`. `POST /v1/net/orders` (authed, same body) → full order record (copy, not the live dict) plus `"balance": {"available": str, "frozen": str}` after placement. `GET /v1/net/orders/mine` (authed) → `{"orders": [...]}` newest-first.

- [ ] Failing tests: preview doesn't move balances or marginals; place freezes exact stake (assert via account read-back) and moves the marginal; insufficient credits → 400 `insufficient_credits` with zero state change; target 0.9995 → 400 `invalid_target` without venue call; edit on resolved variable → 409 `market_closed`; unauthenticated → 401; disabled venue → 503; orders/mine returns only the caller's orders.
- [ ] Implement mirroring `buy()`: parse/validate, clamp target, `async with app.state.lock:` → `app.state.joint.place_edit(...)` → `_save()`; translate VenueError subtypes per the global mapping (add a `translate_venue_error(e) -> APIError` helper next to `translate_engine_error`). Full suite; commit `feat(api): staked probability-edit orders over HTTP`.

### Task B4: Net admin endpoints

**Files:** Modify `core/api.py`, `core/api_models.py`. Test: `core/test_api_net.py`.

**Interfaces produced:** `POST /v1/net/markets/{market_id}/resolve` `{outcome}` and `POST /v1/net/markets/{market_id}/void` — admin-guarded exactly like existing `/v1/admin/*` (reuse the same dependency/check), route ids are venue market ids (e.g. "g1"), venue called with the market's variableId. Responses pass through the venue's settlement report (settled/calledOff/awaiting/treasuryDelta).

- [ ] Failing tests: non-admin → 401/403 (match existing admin tests' expectation); resolve pays a winner (balance delta == payout from msr, asserted via /v1/me or account read); double-resolve → 409 `market_closed`; void refunds in full; both persist across app rebuild (snapshot check).
- [ ] Implement under lock + `_save()`. Full suite; commit `feat(api): net market resolution and void (admin)`.

### Task B5: Credits grant + portfolio surface

**Files:** Modify `core/api.py` (INITIAL_CREDITS default "100"→"1000"), `core/api_models.py`. Test: `core/test_api_net.py` + adjust any existing test asserting the 100 default (core/test_api.py sets INITIAL_CREDITS=1000 env already — check for tests that assert literal 100).

**Interfaces produced:** `GET /v1/me/net` (authed) → `{"orders": [...], "openStake": str (sum of open+awaiting stakes), "settledPnl": str (sum of settled payouts)}`. `GET /v1/leaderboard` (public) → `{"entries": [{"login": str|null, "accountId": int, "total": str}], ...}` top 50 by account total, EXCLUDING AMM accounts, the venue treasury, and service accounts (determine how AMM/service accounts are distinguishable — MarketEngine creates AMM accounts per market and auth users map 1:1 to accounts; read the models and pick a robust exclusion, document it).

- [ ] Failing tests: fresh signup account total == 1000; /v1/me/net math (place two edits, resolve one → openStake and settledPnl exact); leaderboard ordering + exclusions (treasury's 1M must NOT appear).
- [ ] Implement; full suite; commit `feat(api): signup grant 1000, net portfolio, leaderboard`.

### Task B6: HTTP hardening + OpenAPI

**Files:** Modify `core/api.py`, `core/middleware.py`. Test: `core/test_api_net.py`.

- [ ] Failing tests: CORS preflight (`OPTIONS` with Origin) returns configured origin headers (env `CORS_ORIGINS`, default `*`, comma-separated); request body > 64 KB → 413; OpenAPI docs served at `/docs` with title "Futarchy Exchange API" and the /v1/net routes present in `/openapi.json`; rate limiting applies to `/v1/net/orders` (reuse the existing per-key token bucket — verify via the same technique existing rate-limit tests use).
- [ ] Implement: `CORSMiddleware`, a body-size ASGI middleware (starlette pattern, reject on Content-Length and on accumulated stream), FastAPI title/version/description, extend the middleware's rate-limited route set. Full suite; commit `feat(api): CORS, body caps, OpenAPI metadata, net rate limits`.

## Self-review notes

- Coverage vs roadmap: auth-on-writes (B3/B4 via AuthUser + admin guard), grant 1000 (B5), portfolio/leaderboard from settled ledger (B5), CORS/body/OpenAPI (B6), venue persistence wiring incl. the silent-destruction hazard (B1). Ownership enforcement is structural: account_id only ever comes from the authenticated user object, never from a request body.
- Deliberately NOT here: PR-market webhook changes, CLI, frontend, deployment (Plans C–E); order cancellation (orders settle or call off — no cancel in MSR semantics); pagination (909 markets in one response is ~200 KB, acceptable for now, revisit in Plan D).
