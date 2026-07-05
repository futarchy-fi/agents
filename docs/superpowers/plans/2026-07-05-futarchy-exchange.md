# Futarchy Exchange Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild futarchy.ai as a real multi-venue exchange with test money: the existing RiskEngine ledger as the single source of money truth, the existing per-market LMSR MarketEngine as venue A, and the 925-market combinatorial junction-tree net as venue B whose probability-edits are staked and settled in credits as log-MSR bets.

**Architecture:** One FastAPI service in this repo (branch `exchange-v2`). `core/` stays the money spine (RiskEngine, MarketEngine=venue A, auth, persistence). New `venues/joint/` package vendors the bayes inference engine (`FactoredMarket`) and wraps it as venue B: every probability-edit freezes its worst-case log-MSR loss as a lock, settlement happens on variable resolution via log-score payout from a venue treasury, conditional edits are called-off bets. The live bayes.futarchy.ai on farol is NOT touched until the apex swap.

**Tech Stack:** Python 3.14 (repo `.venv`), FastAPI, pytest; vendored pure-Python `FactoredMarket` (junction-tree LMSR) from farol `~/bayes-market/backend/inference/`; JSON snapshot persistence with versioned migrations.

## Global Constraints

- Money precision: `Decimal`, 6 dp (`CREDITS` convention in `core/models.py`). Rounding always favors the house/treasury: stakes round UP, positive payouts round DOWN.
- The only money inlet is `RiskEngine.mint()`. Every mutation appends a `Transaction`. Invariant: `frozen_balance == sum(lock.amount)` per account.
- Conservation: for any sequence of venue-B operations, `sum(all account totals) is constant` except explicit mints.
- Existing test baseline: **119 passed** (`.venv/bin/python -m pytest -q`). Never regress it.
- Python ≥3.11 rejects the vendored package's `mappingproxy` dataclass default — patch the vendored copy (Task 1); never "fix" by downgrading Python.
- GCP work uses project `futarchy-prod` with account `azsantos.k@gmail.com` via a named gcloud configuration — NEVER the active `kelvin@mnx.fi`/MNX config.
- Secrets (`GITHUB_CLIENT_SECRET`, admin key) come from farol `~/.openclaw/workspace/infra/secrets/env/secrets.env` → straight into server env files; never printed, never committed.

## Roadmap (sub-plans; each ships working software)

- **Plan A (THIS DOCUMENT, Tasks 1–6): Venue B engine.** Vendored inference + MSR staking/settlement against the real RiskEngine, snapshot persistence. Pure engine, fully tested, no API changes. → DoD: "joint net trades in credits".
- **Plan B: Unified service.** Venue routing in `core/api.py` (`/v1/net/*` endpoints for venue B: markets, graph+context, probability-edit orders), auth on every write with ownership enforcement, signup grant 1000 credits, portfolio/leaderboard from settled ledger, CORS + body caps + OpenAPI. → DoD: trade both venues via raw curl.
- **Plan C: Infra.** GCP VM (e2-small, europe-north1) in futarchy-prod, Caddy TLS, systemd, HMAC CD webhook (reuse `deploy/`), Cloudflare DNS `api.futarchy.ai` → VM, OAuth secrets wired, state backup cron, budget alert verified. → DoD: sign in with GitHub against live api.futarchy.ai; state survives deliberate restart.
- **Plan D: Frontend + CLI.** New apex landing (vision + live network map + "Sign in with GitHub"), trading UI for both venues, portfolio; adapt the bayes React frontend; update `cli/futarchy_cli` (login via OAuth device flow, `net edit` command, venue-aware `markets`). → DoD: two-minute stranger flow from web UI and CLI; landing links wiki.futarchy.ai.
- **Plan E: Ecosystem + DoD run.** PR markets (webhook receiver + rollover) on ≥2 live repos; forecaster service account on cron; list ≥1 question on both venues and run the arb loop with auditable trades; apex swap; full DoD verification script.

---

# Plan A: Venue B engine (`venues/joint/`)

**File structure:**
- Create: `venues/__init__.py` (empty), `venues/joint/__init__.py` (exports `JointVenue`)
- Create: `venues/joint/inference/` — vendored copy of farol `~/bayes-market/backend/inference/` (patched for py≥3.11)
- Create: `data/seeds_takeoff.json` — copied from farol `~/bayes-market/backend/seeds_takeoff.json` (1.5 MB, commit it)
- Create: `venues/joint/msr.py` — pure stake/payout math
- Create: `venues/joint/venue.py` — `JointVenue` (orders, staking, settlement, snapshot)
- Create: `venues/joint/test_msr.py`, `venues/joint/test_venue.py`
- Modify: `core/persistence.py` — snapshot version 3 with `venues.joint` section

**Interfaces produced (later plans rely on these exact names):**
- `JointVenue(risk_engine, seeds_path, liquidity=Decimal("50"), max_width=8)`
- `.market_ids() -> list[str]`, `.get_market(market_id) -> dict`, `.marginal(variable_id, context: dict[str,str]|None) -> dict[str,float]`
- `.place_edit(account_id: int, variable_id: str, outcome_id: str, target: float, context: dict[str,str]|None) -> dict` (order record; raises `VenueError` subtypes)
- `.preview_edit(...same args...) -> dict` (stake quote, no mutation)
- `.resolve_variable(variable_id: str, outcome_id: str) -> dict` (settlement report)
- `.void_variable(variable_id: str) -> dict`
- `.snapshot() -> dict`, `JointVenue.from_snapshot(data, risk_engine) -> JointVenue`
- `.treasury_account_id: int`

### Task 1: Vendor the inference engine + seeds; make it import on Python 3.14

**Files:** Create `venues/joint/inference/` (vendored), `data/seeds_takeoff.json`, `venues/joint/test_vendor.py`, `venues/__init__.py`, `venues/joint/__init__.py` (empty for now)

- [ ] **Step 1: Copy the package and seeds from farol**

```bash
cd ~/agents
rsync -a --exclude='__pycache__' --exclude='*.pyc' farol:~/bayes-market/backend/inference/ venues/joint/inference/
rsync -a farol:~/bayes-market/backend/seeds_takeoff.json data/seeds_takeoff.json
touch venues/__init__.py venues/joint/__init__.py
```

- [ ] **Step 2: Write the failing import + smoke test**

```python
# venues/joint/test_vendor.py
import json
from pathlib import Path

SEEDS = Path(__file__).resolve().parents[2] / "data" / "seeds_takeoff.json"

def test_inference_imports_on_modern_python():
    from venues.joint.inference.factored_market import FactoredMarket  # noqa: F401

def test_build_from_takeoff_seeds():
    from venues.joint.inference.factored_market import FactoredMarket
    seeds = json.loads(SEEDS.read_text())
    # build nodes the same way bayes server does: markets list with cpt/priors
    from venues.joint.venue import nodes_from_seeds  # implemented in this task
    nodes = nodes_from_seeds(seeds)
    fm = FactoredMarket.from_nodes(nodes, liquidity=50.0, max_width=8)
    assert len(fm.variables()) > 900
    m = fm.marginal("ftm_agi_by_2040")
    assert m is not None and 0.0 < m["yes"] < 1.0
```

- [ ] **Step 3: Run to verify failure** — `.venv/bin/python -m pytest venues/joint/test_vendor.py -x -q`. Expected: import error — on Python ≥3.11 the vendored package raises `ValueError: mutable default <class 'mappingproxy'> for field parent_marginals` (or an ImportError for intra-package imports).

- [ ] **Step 4: Patch the vendored copy.** Two known fixes: (a) rewrite intra-package imports `from backend.inference.X import` / `from .X import` so the package is importable as `venues.joint.inference` (prefer relative imports); (b) find the dataclass with the `mappingproxy` default (`grep -rn mappingproxy venues/joint/inference/`) and replace `field(default=MappingProxyType({}))` with `field(default_factory=dict)`. Then write `nodes_from_seeds(seeds) -> list` in a new minimal `venues/joint/venue.py`: replicate how the bayes server builds `from_nodes` input from seeds-v1 (each market has `variableId`, `outcomes`, optional `cpt` keyed by sorted parent contexts, `marginals` for roots). Reference implementation: farol `~/bayes-market/backend/server.py` — `build_network_nodes` (grep for it; port only what `from_nodes` needs: `(variable_id, outcomes, parents, cpt-or-prior)` tuples/dicts in the exact shape `FactoredMarket.from_nodes` expects — read `from_nodes` at `venues/joint/inference/factored_market.py:121` and match it).

- [ ] **Step 5: Run to verify pass** — same command. Expected: 2 passed (seeds build takes seconds; mark `test_build_from_takeoff_seeds` with `@pytest.mark.slow` only if >30 s).

- [ ] **Step 6: Verify baseline intact + commit**

```bash
.venv/bin/python -m pytest -q   # expect 119 + 2 passed
git add venues data/seeds_takeoff.json && git commit -m "feat(venues): vendor factored-market inference engine + takeoff seeds"
```

### Task 2: MSR stake/payout math (`venues/joint/msr.py`)

**Interfaces produced:** `stake_for_edit(b: Decimal, p: float, q: float) -> Decimal` (worst-case loss of moving P(outcome) from p to q, ≥0, rounded UP to 6 dp); `payout_for_edit(b: Decimal, p: float, q: float, won: bool) -> Decimal` (log-score settlement: `b*ln(q/p)` if the edited outcome won, `b*ln((1-q)/(1-p))` if it lost; positive payouts rounded DOWN, negative rounded UP in magnitude — house-favoring).

- [ ] **Step 1: Write failing tests**

```python
# venues/joint/test_msr.py
from decimal import Decimal
import math
from venues.joint.msr import stake_for_edit, payout_for_edit

B = Decimal("50")

def test_stake_is_worst_case_loss():
    # moving 0.5 -> 0.9: worst case is outcome NO: 50*ln(0.5/0.1)
    s = stake_for_edit(B, 0.5, 0.9)
    assert abs(float(s) - 50 * math.log(0.5 / 0.1)) < 1e-4
    # stake covers the worst payout exactly (within rounding, stake >= |loss|)
    lose = payout_for_edit(B, 0.5, 0.9, won=False)
    assert lose < 0 and s >= -lose

def test_no_edit_no_stake():
    assert stake_for_edit(B, 0.42, 0.42) == Decimal("0")

def test_downward_edit_worst_case_is_yes():
    s = stake_for_edit(B, 0.9, 0.4)
    assert abs(float(s) - 50 * math.log(0.9 / 0.4)) < 1e-4

def test_payout_signs():
    assert payout_for_edit(B, 0.3, 0.7, won=True) > 0
    assert payout_for_edit(B, 0.3, 0.7, won=False) < 0

def test_telescoping_two_traders():
    # A: 0.5->0.7, B: 0.7->0.9; outcome YES.
    # Total payout = 50*ln(0.9/0.5) within rounding.
    total = payout_for_edit(B, 0.5, 0.7, True) + payout_for_edit(B, 0.7, 0.9, True)
    assert abs(float(total) - 50 * math.log(0.9 / 0.5)) < 1e-3
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/python -m pytest venues/joint/test_msr.py -q`. Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement**

```python
# venues/joint/msr.py
"""Log-MSR staking math for probability-edit bets.

An edit moves P(outcome) from p to q with liquidity b. Settlement on
resolution: b*ln(q/p) if the edited outcome occurred, b*ln((1-q)/(1-p))
otherwise. The stake frozen at order time is the worst case of the two.
"""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math

PLACES = Decimal("0.000001")

def _round_up(x: float) -> Decimal:
    return Decimal(str(x)).quantize(PLACES, rounding=ROUND_CEILING)

def _round_down(x: float) -> Decimal:
    return Decimal(str(x)).quantize(PLACES, rounding=ROUND_FLOOR)

def _validate(p: float, q: float) -> None:
    if not (0.0 < p < 1.0 and 0.0 < q < 1.0):
        raise ValueError("probabilities must be strictly inside (0, 1)")

def _raw_payouts(b: Decimal, p: float, q: float) -> tuple[float, float]:
    # Stake and payout MUST derive from these same floats: computing
    # ln(p/q) separately from -ln(q/p) drifts across a rounding tick at
    # large b and breaks stake >= -payout (found in review, task 2).
    scale = float(b)
    return scale * math.log(q / p), scale * math.log((1 - q) / (1 - p))

def stake_for_edit(b: Decimal, p: float, q: float) -> Decimal:
    _validate(p, q)
    raw_won, raw_lost = _raw_payouts(b, p, q)
    return _round_up(max(-raw_won, -raw_lost, 0.0))

def payout_for_edit(b: Decimal, p: float, q: float, won: bool) -> Decimal:
    _validate(p, q)
    raw_won, raw_lost = _raw_payouts(b, p, q)
    raw = raw_won if won else raw_lost
    return _round_down(raw) if raw >= 0 else -_round_up(-raw)
```

- [ ] **Step 4: Run to verify pass**, then **Step 5: Commit** — `git add venues/joint/msr.py venues/joint/test_msr.py && git commit -m "feat(venues): log-MSR stake and payout math"`

### Task 3: JointVenue construction + read surface

**Interfaces produced:** `JointVenue(risk_engine, seeds_path, liquidity=Decimal("50"), max_width=8)`; `.market_ids()`, `.get_market(id)` (dict: id, variableId, title, description, outcomes, marginals, parents), `.marginal(variable_id, context)`, `.treasury_account_id`. Internal: `self._vb_lock_market_id(variable_id) -> int` — stable int ids for RiskEngine locks (enumeration order of seeds, offset 1_000_000). Treasury: created via `risk_engine.create_account()` + `mint(TREASURY_SEED)` where `TREASURY_SEED = Decimal("1000000")`.

- [ ] **Step 1: Failing tests** — build from a tiny inline 2-market seed (root `gcx_a` prior 0.6; child `gcx_b` CPT 0.9/0.2 — same shape as `test_graph_context.py` on farol), assert `market_ids() == ["g1","g2"]`, `get_market("g2")["marginals"]["yes"] ≈ 0.6*0.9+0.4*0.2 = 0.62`, `marginal("gcx_b", {"gcx_a": "yes"})["yes"] ≈ 0.9`, treasury exists with balance 1,000,000. Write the tiny seed as a module-level dict fixture `TINY_SEEDS` in `venues/joint/test_venue.py` (seeds-v1 shape: `{"version":"seeds-v1","markets":[{"id":"g1","variableId":"gcx_a","title":"A","outcomes":["yes","no"],"marginals":{"yes":0.6,"no":0.4}},{"id":"g2","variableId":"gcx_b","title":"B","outcomes":["yes","no"],"parents":["gcx_a"],"cpt":{"gcx_a=yes":{"yes":0.9,"no":0.1},"gcx_a=no":{"yes":0.2,"no":0.8}}}]}` — verify the exact field names against `nodes_from_seeds` from Task 1 and adjust the fixture, not the loader).
- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement in `venues/joint/venue.py`**: constructor loads seeds (path or dict), builds `FactoredMarket.from_nodes(nodes, liquidity=float(liquidity), max_width=max_width)`, creates treasury account, keeps `self._markets: dict[str, dict]` (seed metadata) and `self._var_to_market: dict[str, str]`. `get_market` merges live `fm.marginal(variableId)` into the seed record. Accept `seeds_path: str | Path | dict` (dict for tests).
- [ ] **Step 4: Run, verify pass. Step 5: Commit** `feat(venues): JointVenue construction and read surface`.

### Task 4: place_edit — staked probability edits

**Interfaces produced:** `.preview_edit(account_id, variable_id, outcome_id, target, context=None) -> {"stake", "before", "after", "b"}`; `.place_edit(...) -> order dict {"orderId": "vb_<n>", "accountId", "variableId", "outcomeId", "target", "context", "before": float, "after": float, "stake": str(Decimal), "lockId": int, "status": "open", "fill": <trade_to_probability return>}`. Errors: `VenueError` (base), `UnknownVariable`, `InsufficientCredits`, `WidthBudgetExceeded` (wrapping `JointMarketError` from re-triangulation) — all defined in `venues/joint/venue.py`.

- [ ] **Step 1: Failing tests** (tiny seed): (a) placing edit 0.6→0.8 on `gcx_a` freezes exactly `stake_for_edit(B, 0.6, 0.8)` (account frozen_balance == lock amount, available reduced); (b) marginal actually moved (`marginal("gcx_a")["yes"] ≈ 0.8`) AND the child repriced coherently (`get_market("g2")["marginals"]["yes"] ≈ 0.8*0.9+0.2*0.2 = 0.76`); (c) `InsufficientCredits` raised when stake > available, and NO state change (marginal unchanged, no lock); (d) conditional edit with context `{"gcx_a":"yes"}` on `gcx_b` records context and does not move P(gcx_a); (e) `preview_edit` mutates nothing.
- [ ] **Step 2: Run, verify failure. Step 3: Implement**: `before = fm.marginal(variable_id, context)[outcome_id]`; stake via `msr.stake_for_edit`; `risk_engine.check_available` then `risk_engine.lock(account_id, self._vb_lock_market_id(variable_id), stake, "msr_stake")`; then `fm.trade_to_probability(variable_id, outcome_id, target, context)` inside try — on `JointMarketError`, release the lock and re-raise as `WidthBudgetExceeded` (order-of-operations test: lock released, marginal unchanged). Append order to `self._orders` (list of dicts) and `self._orders_by_var[variable_id]`.
- [ ] **Step 4: Run all venue tests + full suite. Step 5: Commit** `feat(venues): staked probability-edit orders`.

### Task 5: Settlement — resolve_variable / void_variable

**Interfaces produced:** `.resolve_variable(variable_id, outcome_id) -> {"settled": [...], "calledOff": [...], "treasuryDelta": str}`; `.void_variable(variable_id) -> {"calledOff": [...]}`. Semantics: (1) `fm.condition(variable_id, outcome_id)`; (2) every OPEN order ON that variable: if its context was contradicted by earlier resolutions → called off (release lock, stake back); else settle: `payout = msr.payout_for_edit(b, before, after_target, won=(order.outcomeId==outcome_id if target edit raised that outcome... NO — simpler and correct: won = (resolved outcome == order["outcomeId"]))`; wait — the edit moved P(order.outcomeId) from `before` to `target`; `won=True` iff `outcome_id == order["outcomeId"]`. Settle via `risk_engine.settle_lock(lockId, payout= stake + payout, to available)`? NO — read `settle_lock` signature first (`core/risk_engine.py:162`) and use it as designed: release the stake lock back to available, then if payout > 0 `transfer_available(treasury, account, payout)`, if payout < 0 `transfer_available(account, treasury, -payout)` (guaranteed covered because stake ≥ -payout and stake was just released). (3) OPEN orders on OTHER variables whose context references this variable: contradicted context value → call off now; matching value → mark that context key satisfied (keep order open). Void: call off all open orders on the variable AND all open orders referencing it in context. Mark market status resolved/void in `self._markets`.

- [ ] **Step 1: Failing tests**: (a) winner gets `b*ln(q/p)` from treasury (balance-exact assertion using msr functions); (b) loser pays `|b*ln((1-q)/(1-p))|` ≤ stake, remainder of stake returned; (c) called-off conditional order (context var resolved to the other outcome) → full stake back, treasury untouched; (d) **conservation**: sum of all account totals (traders + treasury) unchanged across place+resolve for a 3-trader scenario; (e) void → everyone whole; (f) resolving `gcx_a=yes` leaves an open `gcx_b` order with context `{"gcx_a":"yes"}` still open, and it settles correctly when `gcx_b` later resolves.
- [ ] **Step 2–4: Run failing → implement → run full suite** (baseline 119 + all new must pass). **Step 5: Commit** `feat(venues): log-score settlement, called-off bets, void`.

### Task 6: Snapshot persistence (version 3)

**Files:** Modify `core/persistence.py` (CURRENT_VERSION 2→3, `_migrate_2_to_3` adds empty `venues` section), extend `save_snapshot`/`load_snapshot` to accept optional `joint_venue`; `JointVenue.snapshot()` returns `{"orders", "markets_status", "treasury_account_id", "fm": fm.snapshot(), "liquidity", "seeds_source"}`; `JointVenue.from_snapshot(data, risk_engine)` rebuilds via `FactoredMarket.from_snapshot` (fall back to seeds rebuild if structure mismatch — log, don't crash; same discipline as bayes).

- [ ] **Step 1: Failing roundtrip test** — place 2 edits (one conditional) on tiny seed, snapshot, rebuild venue + risk engine from snapshot, assert: marginals identical to 1e-9, open orders identical, resolve settles identically to the un-persisted twin. Plus: migration test — a version-2 snapshot loads and gets an empty venues section.
- [ ] **Step 2–4: fail → implement → full suite.** **Step 5: Commit** `feat(venues): snapshot persistence v3 with joint venue state`.

---

## Self-review notes

- Spec coverage: Plan A covers "joint net trades in credits" + conservation. Auth/API/infra/frontend/PR-markets deliberately in Plans B–E (roadmap above).
- Type consistency: `account_id: int` (RiskEngine convention), `variable_id/outcome_id: str`, money `Decimal`, probabilities `float`. Lock market ids for venue B: `1_000_000 + seed index` via `_vb_lock_market_id`.
- Known open question for Plan B: unified market listing shape across venues (venue field vs separate endpoints — leaning `/v1/net/*` separate, decided at Plan B writing time with the API in front of us).
