"""Current independent-market compile artifact for the Bayes inference seam."""

from __future__ import annotations

import hashlib
import json
import math
import time
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .config import DEFAULT_ENGINE_CONFIG
from .contracts import AtomicEventQueryResult, CliqueSummary, CompileResult, InferenceQueryBackend, MarginalQueryResult
from .errors import InferenceCompileError, InferenceQueryError, InferenceUnsupportedQueryError

CURRENT_MODEL_EXACT_ELIGIBILITY_REASON = "independent_market_baseline"
_COMPILE_ERROR_CODE = "compile_snapshot_invalid"


def canonical_json_hash(data: object) -> str:
    """Return a stable SHA-256 digest for JSON-serializable data."""
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _compile_error(message: str, **details: object) -> InferenceCompileError:
    return InferenceCompileError(message, code=_COMPILE_ERROR_CODE, details=details)


def _freeze_json_value(value: Any, *, path: str) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key in sorted(value, key=str):
            if not isinstance(key, str):
                raise _compile_error("Compile snapshot keys must be strings", path=path)
            frozen[key] = _freeze_json_value(value[key], path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item, path=f"{path}[{index}]") for index, item in enumerate(value))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise _compile_error("Compile snapshot values must be JSON-serializable", path=path)


def thaw_json_value(value: Any) -> Any:
    """Convert frozen JSON-like structures back into mutable Python containers."""
    if isinstance(value, Mapping):
        return {key: thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json_value(item) for item in value]
    return value


def _require_mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _compile_error("Compile snapshot field must be an object", field=field)
    return value


def _require_sequence(value: object, *, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise _compile_error("Compile snapshot field must be an array", field=field)
    return value


def _require_string(mapping: Mapping[str, Any], field: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _compile_error("Compile snapshot field must be a non-empty string", field=field)
    return value


def _normalize_outcomes(market_snapshot: Mapping[str, Any]) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
    raw_outcomes = _require_sequence(market_snapshot.get("outcomes"), field="outcomes")
    normalized_outcomes: list[Mapping[str, Any]] = []
    outcome_ids: list[str] = []
    seen: set[str] = set()

    for index, raw_outcome in enumerate(raw_outcomes):
        outcome = _require_mapping(raw_outcome, field=f"outcomes[{index}]")
        outcome_id = _require_string(outcome, "id")
        if outcome_id in seen:
            raise _compile_error("Compile snapshot outcomes must have unique ids", field="outcomes")
        seen.add(outcome_id)
        normalized_outcomes.append(deepcopy(dict(outcome)))
        outcome_ids.append(outcome_id)

    if not normalized_outcomes:
        raise _compile_error("Compile snapshot outcomes must be non-empty", field="outcomes")

    return tuple(normalized_outcomes), tuple(outcome_ids)


def _normalize_probability_slice(
    raw_slice: object,
    *,
    field: str,
    outcome_ids: tuple[str, ...],
) -> dict[str, float]:
    probability_slice = _require_mapping(raw_slice, field=field)
    unexpected_outcomes = sorted(str(key) for key in probability_slice.keys() if key not in outcome_ids)
    missing_outcomes = [outcome_id for outcome_id in outcome_ids if outcome_id not in probability_slice]

    if missing_outcomes or unexpected_outcomes:
        raise _compile_error(
            "Compile snapshot probabilities must match outcome ids",
            field=field,
            missing=missing_outcomes,
            unexpected=unexpected_outcomes,
        )

    normalized: dict[str, float] = {}
    for outcome_id in outcome_ids:
        value = probability_slice[outcome_id]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _compile_error("Compile snapshot probabilities must be numeric", field=field, outcomeId=outcome_id)
        probability = float(value)
        if not 0.0 <= probability <= 1.0:
            raise _compile_error(
                "Compile snapshot probabilities must be between 0 and 1",
                field=field,
                outcomeId=outcome_id,
            )
        normalized[outcome_id] = probability

    if not math.isclose(sum(normalized.values()), 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise _compile_error("Compile snapshot probabilities must sum to 1", field=field)

    return normalized


def _normalize_conditional_marginals(
    raw_conditionals: object,
    *,
    outcome_ids: tuple[str, ...],
) -> dict[str, dict[str, float]]:
    if raw_conditionals is None:
        return {}

    conditional_marginals = _require_mapping(raw_conditionals, field="conditionalMarginals")
    normalized: dict[str, dict[str, float]] = {}
    for context_key in sorted(conditional_marginals, key=str):
        if not isinstance(context_key, str) or not context_key:
            raise _compile_error("Conditional marginal keys must be non-empty strings", field="conditionalMarginals")
        normalized[context_key] = _normalize_probability_slice(
            conditional_marginals[context_key],
            field=f"conditionalMarginals.{context_key}",
            outcome_ids=outcome_ids,
        )
    return normalized


def _parse_context_key_variable_ids(context_key: str) -> list[str]:
    """Extract variable ids from a serialized context-state key."""
    if not context_key:
        return []

    variable_ids: list[str] = []
    for assignment in context_key.split("|"):
        variable_id, separator, _outcome_id = assignment.partition("=")
        if separator and variable_id:
            variable_ids.append(variable_id)
    return variable_ids


def _build_singleton_cliques(market_id: str, variable_id: str, outcome_count: int) -> tuple[CliqueSummary, ...]:
    return (
        CliqueSummary(
            id=f"{market_id}-c1",
            nodes=(variable_id,),
            size=1,
            states=outcome_count,
        ),
    )


def _build_cliques(
    market_id: str,
    variable_id: str,
    outcome_count: int,
    conditional_marginals: Mapping[str, Mapping[str, float]],
    market_outcomes_by_variable: Mapping[str, int],
) -> tuple[CliqueSummary, ...]:
    """Build clique summaries from a market's variable and its conditional contexts."""
    raw_cliques: set[tuple[str, ...]] = {(variable_id,)}

    for context_key in conditional_marginals:
        context_variable_ids = _parse_context_key_variable_ids(context_key)
        clique_nodes = {variable_id, *context_variable_ids}
        raw_cliques.add(tuple(sorted(clique_nodes)))

    cliques: list[CliqueSummary] = []
    for index, nodes in enumerate(sorted(raw_cliques), start=1):
        state_count = 1
        for node_id in nodes:
            node_outcomes = market_outcomes_by_variable.get(node_id, outcome_count if node_id == variable_id else 1)
            state_count *= max(node_outcomes, 1)
        cliques.append(
            CliqueSummary(
                id=f"{market_id}-c{index}",
                nodes=nodes,
                size=len(nodes),
                states=state_count,
            )
        )

    return tuple(cliques)


def _estimate_memory_bytes(cliques: tuple[CliqueSummary, ...]) -> int:
    total = 0
    for clique in cliques:
        total += int(clique.states) * 32 + int(clique.size) * 64
    return total


def _compile_id_from_hash(source_state_hash: str) -> str:
    digest = source_state_hash.split(":", 1)[-1]
    return f"comp-{digest[:12]}"


def _require_current_model_artifact(compile_result: CompileResult) -> CurrentModelCompileArtifact:
    artifact = compile_result.artifact
    if artifact is None:
        raise InferenceQueryError(
            "Compile result does not include a queryable artifact",
            details={"compileId": compile_result.compile_id},
        )
    if not isinstance(artifact, CurrentModelCompileArtifact):
        raise InferenceQueryError(
            "Compile result artifact is not a current-model artifact",
            details={
                "compileId": compile_result.compile_id,
                "artifactType": type(artifact).__name__,
            },
        )
    if compile_result.compile_id != artifact.compile_id:
        raise InferenceQueryError(
            "Compile result metadata does not match artifact compile_id",
            details={
                "compileId": compile_result.compile_id,
                "artifactCompileId": artifact.compile_id,
            },
        )
    if compile_result.source_state_hash != artifact.source_state_hash:
        raise InferenceQueryError(
            "Compile result metadata does not match artifact source_state_hash",
            details={
                "compileId": compile_result.compile_id,
                "sourceStateHash": compile_result.source_state_hash,
                "artifactSourceStateHash": artifact.source_state_hash,
            },
        )
    if compile_result.compile_type != artifact.compile_type:
        raise InferenceQueryError(
            "Compile result metadata does not match artifact compile_type",
            details={
                "compileId": compile_result.compile_id,
                "compileType": compile_result.compile_type,
                "artifactCompileType": artifact.compile_type,
            },
        )
    if not artifact.exact_eligible:
        raise InferenceUnsupportedQueryError(
            "Compiled artifact is not eligible for exact query execution",
            details={
                "compileId": compile_result.compile_id,
                "eligibilityReason": artifact.eligibility_reason,
            },
        )
    return artifact


def _parse_context_key_assignments(context_key: str) -> dict[str, str] | None:
    """Parse a serialized context key ("a=1|b=2") into an assignment map."""
    if not context_key:
        return None
    assignments: dict[str, str] = {}
    for part in context_key.split("|"):
        variable_id, separator, outcome_id = part.partition("=")
        if not separator or not variable_id or not outcome_id:
            return None
        assignments[variable_id] = outcome_id
    return assignments


def _marginalize_partial_context(
    artifact: "CurrentModelCompileArtifact",
    context: Mapping[str, str],
) -> dict[str, float] | None:
    """Marginalize the CPT over parents not fixed by the context.

    P(X | e) = sum_u P(X | e, u) * P(u), mixing full-assignment CPT rows
    weighted by the priors of the unassigned parents. Parents are treated
    as independent (the current-model baseline). Returns None when the
    context cannot be covered by any CPT scope or a needed prior is
    missing, in which case the caller falls back to unconditional
    marginals — the correct answer for evidence on unrelated variables.
    """
    if not artifact.conditional_marginals or not context:
        return None

    # Group CPT rows by their variable scope; choose the smallest scope
    # that covers every context variable.
    scopes: dict[frozenset[str], list[tuple[dict[str, str], Mapping[str, float]]]] = {}
    for key, row in artifact.conditional_marginals.items():
        assignment = _parse_context_key_assignments(key)
        if assignment is None:
            continue
        scopes.setdefault(frozenset(assignment), []).append((assignment, row))

    # Evidence on variables outside every CPT scope cannot move this market
    # under the independent-parent model — drop it rather than falling back
    # and discarding the relevant evidence with it.
    scope_union: set[str] = set().union(*scopes)
    relevant_context = {var: out for var, out in context.items() if var in scope_union}
    if not relevant_context:
        return None

    context_vars = set(relevant_context)
    covering = [scope for scope in scopes if context_vars <= scope]
    if not covering:
        return None
    scope = min(covering, key=len)

    accumulated = {str(outcome_id): 0.0 for outcome_id in artifact.marginals}
    total_weight = 0.0
    for assignment, row in scopes[scope]:
        if any(assignment[var] != outcome for var, outcome in relevant_context.items()):
            continue
        weight = 1.0
        for var, outcome in assignment.items():
            if var in context_vars:
                continue
            prior = artifact.parent_marginals.get(var, {}).get(outcome)
            if prior is None:
                return None
            weight *= float(prior)
        total_weight += weight
        for outcome_id, probability in row.items():
            accumulated[str(outcome_id)] += weight * float(probability)

    if total_weight <= 0.0:
        return None
    return {outcome_id: value / total_weight for outcome_id, value in accumulated.items()}


def _context_mapping_key(context: Mapping[str, str] | None) -> str:
    if not context:
        return ""

    normalized_pairs: list[tuple[str, str]] = []
    for raw_variable_id in sorted(context, key=str):
        if not isinstance(raw_variable_id, str) or not raw_variable_id.strip():
            raise InferenceQueryError(
                "Query context variable ids must be non-empty strings",
                details={"field": "context.variableId"},
            )
        raw_outcome_id = context[raw_variable_id]
        if not isinstance(raw_outcome_id, str) or not raw_outcome_id.strip():
            raise InferenceQueryError(
                "Query context outcome ids must be non-empty strings",
                details={"field": f"context[{raw_variable_id!r}]"},
            )
        normalized_pairs.append((raw_variable_id.strip(), raw_outcome_id.strip()))

    return "|".join(f"{variable_id}={outcome_id}" for variable_id, outcome_id in normalized_pairs)


@dataclass(frozen=True)
class CurrentModelCompileArtifact:
    """Immutable compile artifact for the current independent-market model."""

    market_id: str
    variable_id: str
    outcomes: tuple[Mapping[str, Any], ...]
    marginals: Mapping[str, float]
    conditional_marginals: Mapping[str, Mapping[str, float]]
    source_state_inputs: Mapping[str, Any]
    source_state_hash: str
    compile_id: str
    compile_type: str
    cliques: tuple[CliqueSummary, ...]
    junction_tree_width: int
    exact_eligible: bool
    eligibility_reason: str
    memory_bytes: int
    # Priors of the context (parent) variables referenced by
    # conditional_marginals keys; used to marginalize partial contexts.
    # default_factory, not MappingProxyType({}): Python <=3.12 rejects a
    # mappingproxy dataclass default as mutable (deploy target is 3.11).
    parent_marginals: Mapping[str, Mapping[str, float]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcomes", tuple(self.outcomes))
        object.__setattr__(self, "cliques", tuple(self.cliques))

        if not self.market_id:
            raise ValueError("Compile artifact must include market_id")
        if not self.variable_id:
            raise ValueError("Compile artifact must include variable_id")
        if not self.source_state_hash:
            raise ValueError("Compile artifact must include source_state_hash")
        if not self.compile_id:
            raise ValueError("Compile artifact must include compile_id")
        if not self.compile_type:
            raise ValueError("Compile artifact must include compile_type")
        if self.junction_tree_width < 0:
            raise ValueError("Compile artifact junction_tree_width must be non-negative")
        if self.memory_bytes < 0:
            raise ValueError("Compile artifact memory_bytes must be non-negative")
        if not self.eligibility_reason:
            raise ValueError("Compile artifact must include eligibility_reason")

    def source_state_payload(self) -> dict[str, Any]:
        """Return the thawed source-state payload used to build the artifact."""
        return thaw_json_value(self.source_state_inputs)

    def to_compile_result(self, *, compile_time_ms: float = 0.0, last_updated: str) -> CompileResult:
        """Convert the artifact into the shared compile-result contract."""
        return CompileResult(
            compile_id=self.compile_id,
            compile_type=self.compile_type,
            source_state_hash=self.source_state_hash,
            cliques=self.cliques,
            compile_time_ms=compile_time_ms,
            memory_bytes=self.memory_bytes,
            last_updated=last_updated,
            artifact=self,
        )


@dataclass(frozen=True)
class CurrentModelCompiler:
    """Compile current-market snapshots into immutable query artifacts."""

    compile_type: str = DEFAULT_ENGINE_CONFIG.compile_type
    eligibility_reason: str = CURRENT_MODEL_EXACT_ELIGIBILITY_REASON
    max_treewidth: int = DEFAULT_ENGINE_CONFIG.max_treewidth

    def compile_artifact(
        self,
        *,
        market_snapshot: Mapping[str, Any],
        conditional_marginals: Mapping[str, Mapping[str, float]] | None = None,
        market_outcomes_by_variable: Mapping[str, int] | None = None,
        parent_marginals: Mapping[str, Mapping[str, float]] | None = None,
    ) -> CurrentModelCompileArtifact:
        """Normalize a market snapshot into a frozen current-model artifact."""
        normalized_market = _require_mapping(market_snapshot, field="market")
        market_id = _require_string(normalized_market, "id")
        variable_id = _require_string(normalized_market, "variableId")
        outcomes, outcome_ids = _normalize_outcomes(normalized_market)
        marginals = _normalize_probability_slice(normalized_market.get("marginals"), field="marginals", outcome_ids=outcome_ids)
        normalized_conditionals = _normalize_conditional_marginals(conditional_marginals or {}, outcome_ids=outcome_ids)

        source_state_inputs_plain = {
            "market": deepcopy(dict(normalized_market)),
            "conditionalMarginals": deepcopy(dict(conditional_marginals or {})),
        }
        # Only added when present so artifacts without parent priors keep
        # their historical source_state_hash.
        if parent_marginals:
            source_state_inputs_plain["parentMarginals"] = deepcopy(
                {str(var): dict(slice_) for var, slice_ in parent_marginals.items()}
            )

        try:
            source_state_hash = canonical_json_hash(source_state_inputs_plain)
            frozen_source_state_inputs = _freeze_json_value(source_state_inputs_plain, path="sourceStateInputs")
            frozen_outcomes = tuple(_freeze_json_value(outcome, path=f"outcomes[{index}]") for index, outcome in enumerate(outcomes))
            frozen_marginals = _freeze_json_value(marginals, path="marginals")
            frozen_conditionals = _freeze_json_value(normalized_conditionals, path="conditionalMarginals")
        except (TypeError, ValueError) as exc:
            raise _compile_error("Unable to freeze compile snapshot state", field="sourceStateInputs") from exc

        outcome_count = len(outcome_ids)
        if normalized_conditionals and market_outcomes_by_variable is not None:
            cliques = _build_cliques(
                market_id, variable_id, outcome_count,
                normalized_conditionals, market_outcomes_by_variable,
            )
        else:
            cliques = _build_singleton_cliques(market_id, variable_id, outcome_count)

        max_clique_size = max(c.size for c in cliques)
        junction_tree_width = max(0, max_clique_size - 1)
        memory_bytes = _estimate_memory_bytes(cliques)

        if junction_tree_width > self.max_treewidth:
            exact_eligible = False
            eligibility_reason = "treewidth_exceeds_bound"
        else:
            exact_eligible = True
            eligibility_reason = self.eligibility_reason

        try:
            frozen_parent_marginals = _freeze_json_value(
                {str(var): dict(slice_) for var, slice_ in (parent_marginals or {}).items()},
                path="parentMarginals",
            )
        except (TypeError, ValueError) as exc:
            raise _compile_error("Unable to freeze parent marginals", field="parentMarginals") from exc

        return CurrentModelCompileArtifact(
            market_id=market_id,
            variable_id=variable_id,
            outcomes=frozen_outcomes,
            marginals=frozen_marginals,
            conditional_marginals=frozen_conditionals,
            source_state_inputs=frozen_source_state_inputs,
            source_state_hash=source_state_hash,
            compile_id=_compile_id_from_hash(source_state_hash),
            compile_type=self.compile_type,
            cliques=cliques,
            junction_tree_width=junction_tree_width,
            exact_eligible=exact_eligible,
            eligibility_reason=eligibility_reason,
            memory_bytes=memory_bytes,
            parent_marginals=frozen_parent_marginals,
        )

    def compile_result(
        self,
        *,
        market_snapshot: Mapping[str, Any],
        conditional_marginals: Mapping[str, Mapping[str, float]] | None = None,
        market_outcomes_by_variable: Mapping[str, int] | None = None,
        parent_marginals: Mapping[str, Mapping[str, float]] | None = None,
        compile_time_ms: float = 0.0,
        last_updated: str,
    ) -> CompileResult:
        """Compile a market snapshot and wrap it in the shared result contract."""
        artifact = self.compile_artifact(
            market_snapshot=market_snapshot,
            conditional_marginals=conditional_marginals,
            market_outcomes_by_variable=market_outcomes_by_variable,
            parent_marginals=parent_marginals,
        )
        return artifact.to_compile_result(
            compile_time_ms=round(float(compile_time_ms), 3),
            last_updated=last_updated,
        )


CURRENT_MODEL_COMPILER = CurrentModelCompiler()


@dataclass(frozen=True)
class CurrentModelQueryBackend(InferenceQueryBackend):
    """Execute exact marginal and atomic queries against current-model artifacts."""

    def query_marginals(
        self,
        compile_result: CompileResult,
        *,
        context: Mapping[str, str] | None = None,
    ) -> MarginalQueryResult:
        """Return unconditional or conditional marginals for a compiled artifact."""
        started_at = time.perf_counter()
        artifact = _require_current_model_artifact(compile_result)
        context_key = _context_mapping_key(context)

        resolution_source = "unconditional"
        marginals = artifact.marginals
        if context_key:
            conditional_marginals = artifact.conditional_marginals.get(context_key)
            if conditional_marginals is not None:
                resolution_source = "conditional"
                marginals = conditional_marginals
            else:
                # Partial parent assignment: marginalize the CPT over the
                # unassigned parents using their priors.
                marginalized = _marginalize_partial_context(artifact, dict(context or {}))
                if marginalized is not None:
                    resolution_source = "conditional_marginalized"
                    marginals = marginalized

        return MarginalQueryResult(
            marginals=marginals,
            runtime_ms=round((time.perf_counter() - started_at) * 1000.0, 3),
            cache_hit=False,
            compile_id=compile_result.compile_id,
            metadata={
                "contextKey": context_key,
                "resolutionSource": resolution_source,
                "eligibilityReason": artifact.eligibility_reason,
            },
        )

    def query_atomic_event(
        self,
        compile_result: CompileResult,
        *,
        variable_id: str,
        outcome_id: str,
        negated: bool = False,
    ) -> AtomicEventQueryResult:
        """Return the probability of a single atomic event from the artifact."""
        started_at = time.perf_counter()
        artifact = _require_current_model_artifact(compile_result)

        if not isinstance(variable_id, str) or not variable_id.strip():
            raise InferenceQueryError("Atomic event query variable_id must be a non-empty string")
        if not isinstance(outcome_id, str) or not outcome_id.strip():
            raise InferenceQueryError("Atomic event query outcome_id must be a non-empty string")
        if negated:
            raise InferenceUnsupportedQueryError(
                "Negated atomic event queries are not supported",
                details={"variableId": variable_id, "outcomeId": outcome_id},
            )

        normalized_variable_id = variable_id.strip()
        normalized_outcome_id = outcome_id.strip()

        if normalized_variable_id != artifact.variable_id:
            raise InferenceUnsupportedQueryError(
                "Current-model query backend only supports the compiled market variable",
                details={
                    "variableId": normalized_variable_id,
                    "compiledVariableId": artifact.variable_id,
                },
            )
        if normalized_outcome_id not in artifact.marginals:
            raise InferenceQueryError(
                "Atomic event query outcome_id does not exist in the compiled market",
                details={
                    "variableId": normalized_variable_id,
                    "outcomeId": normalized_outcome_id,
                    "allowedOutcomeIds": sorted(artifact.marginals),
                },
            )

        return AtomicEventQueryResult(
            variable_id=normalized_variable_id,
            outcome_id=normalized_outcome_id,
            probability=float(artifact.marginals[normalized_outcome_id]),
            runtime_ms=round((time.perf_counter() - started_at) * 1000.0, 3),
            cache_hit=False,
            compile_id=compile_result.compile_id,
            metadata={
                "resolutionSource": "unconditional",
                "eligibilityReason": artifact.eligibility_reason,
            },
        )


CURRENT_MODEL_QUERY_BACKEND = CurrentModelQueryBackend()


def compile_current_market_artifact(
    *,
    market_snapshot: Mapping[str, Any],
    conditional_marginals: Mapping[str, Mapping[str, float]] | None = None,
    market_outcomes_by_variable: Mapping[str, int] | None = None,
) -> CurrentModelCompileArtifact:
    """Compile a market snapshot into a current-model artifact."""
    return CURRENT_MODEL_COMPILER.compile_artifact(
        market_snapshot=market_snapshot,
        conditional_marginals=conditional_marginals,
        market_outcomes_by_variable=market_outcomes_by_variable,
    )


def compile_current_model_artifact(
    *,
    market_snapshot: Mapping[str, Any],
    conditional_marginals: Mapping[str, Mapping[str, float]] | None = None,
    market_outcomes_by_variable: Mapping[str, int] | None = None,
) -> CurrentModelCompileArtifact:
    """Alias for compiling a market snapshot into a current-model artifact."""
    return compile_current_market_artifact(
        market_snapshot=market_snapshot,
        conditional_marginals=conditional_marginals,
        market_outcomes_by_variable=market_outcomes_by_variable,
    )


def compile_current_market_result(
    *,
    market_snapshot: Mapping[str, Any],
    conditional_marginals: Mapping[str, Mapping[str, float]] | None = None,
    market_outcomes_by_variable: Mapping[str, int] | None = None,
    compile_time_ms: float = 0.0,
    last_updated: str,
) -> CompileResult:
    """Compile a market snapshot and return the exported result wrapper."""
    return CURRENT_MODEL_COMPILER.compile_result(
        market_snapshot=market_snapshot,
        conditional_marginals=conditional_marginals,
        market_outcomes_by_variable=market_outcomes_by_variable,
        compile_time_ms=compile_time_ms,
        last_updated=last_updated,
    )


def compile_current_model_result(
    *,
    market_snapshot: Mapping[str, Any],
    conditional_marginals: Mapping[str, Mapping[str, float]] | None = None,
    market_outcomes_by_variable: Mapping[str, int] | None = None,
    compile_time_ms: float = 0.0,
    last_updated: str,
) -> CompileResult:
    """Alias for compiling a market snapshot into the exported result wrapper."""
    return compile_current_market_result(
        market_snapshot=market_snapshot,
        conditional_marginals=conditional_marginals,
        market_outcomes_by_variable=market_outcomes_by_variable,
        compile_time_ms=compile_time_ms,
        last_updated=last_updated,
    )


__all__ = [
    "CURRENT_MODEL_COMPILER",
    "CURRENT_MODEL_QUERY_BACKEND",
    "CURRENT_MODEL_EXACT_ELIGIBILITY_REASON",
    "CurrentModelCompileArtifact",
    "CurrentModelCompiler",
    "CurrentModelQueryBackend",
    "canonical_json_hash",
    "compile_current_market_artifact",
    "compile_current_market_result",
    "compile_current_model_artifact",
    "compile_current_model_result",
    "thaw_json_value",
]
