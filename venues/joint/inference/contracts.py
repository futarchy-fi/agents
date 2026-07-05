"""Compile/query contracts for the extracted Bayes inference seam."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True)
class CliqueSummary:
    """Summarize one clique in a compiled graphical model."""

    id: str
    nodes: tuple[str, ...]
    size: int
    states: int

    def __post_init__(self) -> None:
        normalized_nodes = tuple(str(node) for node in self.nodes)
        object.__setattr__(self, "nodes", normalized_nodes)

        if not self.id:
            raise ValueError("Clique id must be non-empty")
        if self.size != len(normalized_nodes):
            raise ValueError("Clique size must match node count")
        if self.size < 0:
            raise ValueError("Clique size must be non-negative")
        if self.states < 0:
            raise ValueError("Clique state count must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        """Convert the clique summary to a JSON-friendly dictionary."""
        return {
            "id": self.id,
            "nodes": list(self.nodes),
            "size": self.size,
            "states": self.states,
        }


@dataclass(frozen=True)
class CompileResult:
    """Describe the output of compiling a market snapshot for inference."""

    compile_id: str
    compile_type: str
    source_state_hash: str
    cliques: tuple[CliqueSummary, ...]
    compile_time_ms: float = 0.0
    memory_bytes: int = 0
    last_updated: str = ""
    artifact: Any | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "cliques", tuple(self.cliques))

        if not self.compile_id:
            raise ValueError("Compile result must include compile_id")
        if not self.compile_type:
            raise ValueError("Compile result must include compile_type")
        if not self.source_state_hash:
            raise ValueError("Compile result must include source_state_hash")
        if self.compile_time_ms < 0.0:
            raise ValueError("Compile time must be non-negative")
        if self.memory_bytes < 0:
            raise ValueError("Compile memory_bytes must be non-negative")
        if not self.last_updated:
            raise ValueError("Compile result must include last_updated")


@dataclass(frozen=True)
class MarginalQueryResult:
    """Return value for a marginal-probability query against a compiled artifact."""

    marginals: Mapping[str, float]
    runtime_ms: float = 0.0
    cache_hit: bool = False
    compile_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "marginals", {str(key): float(value) for key, value in self.marginals.items()})
        object.__setattr__(self, "metadata", dict(self.metadata))

        if self.runtime_ms < 0.0:
            raise ValueError("Query runtime must be non-negative")


@dataclass(frozen=True)
class AtomicEventQueryResult:
    """Return value for a single atomic-event probability query."""

    variable_id: str
    outcome_id: str
    probability: float
    runtime_ms: float = 0.0
    cache_hit: bool = False
    compile_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", dict(self.metadata))

        if not self.variable_id:
            raise ValueError("Atomic event query variable_id must be non-empty")
        if not self.outcome_id:
            raise ValueError("Atomic event query outcome_id must be non-empty")
        if not 0.0 <= self.probability <= 1.0:
            raise ValueError("Atomic event query probability must be between 0 and 1")
        if self.runtime_ms < 0.0:
            raise ValueError("Query runtime must be non-negative")


@runtime_checkable
class InferenceCompiler(Protocol):
    """Protocol for components that compile market snapshots into artifacts."""

    def compile_market(self, *, market_id: str, source_state_hash: str) -> CompileResult:
        """Compile a market-local inference artifact."""


@runtime_checkable
class InferenceQueryBackend(Protocol):
    """Protocol for components that answer queries over compiled artifacts."""

    def query_marginals(
        self,
        compile_result: CompileResult,
        *,
        context: Mapping[str, str] | None = None,
    ) -> MarginalQueryResult:
        """Query marginal probabilities from a compiled artifact."""

    def query_atomic_event(
        self,
        compile_result: CompileResult,
        *,
        variable_id: str,
        outcome_id: str,
        negated: bool = False,
    ) -> AtomicEventQueryResult:
        """Query a single EventTrade literal against a compiled artifact."""


__all__ = [
    "AtomicEventQueryResult",
    "CliqueSummary",
    "CompileResult",
    "InferenceCompiler",
    "InferenceQueryBackend",
    "MarginalQueryResult",
]
