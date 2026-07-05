"""Stable inference contracts and configuration for the Bayes engine seam."""

from .config import DEFAULT_ENGINE_CONFIG, EngineConfig
from .contracts import (
    AtomicEventQueryResult,
    CliqueSummary,
    CompileResult,
    InferenceCompiler,
    InferenceQueryBackend,
    MarginalQueryResult,
)
from .current_model import (
    CURRENT_MODEL_COMPILER,
    CURRENT_MODEL_QUERY_BACKEND,
    CURRENT_MODEL_EXACT_ELIGIBILITY_REASON,
    CurrentModelCompileArtifact,
    CurrentModelCompiler,
    CurrentModelQueryBackend,
    compile_current_market_artifact,
    compile_current_market_result,
    compile_current_model_artifact,
    compile_current_model_result,
)
from .cache_invalidation import CacheInvalidationManager, InvalidationResult
from .current_model import canonical_json_hash
from .errors import (
    InferenceCompileError,
    InferenceError,
    InferenceQueryError,
    InferenceUnsupportedQueryError,
)
from .factored_market import FactoredMarket
from .joint_market import JointMarket, JointMarketError
from .network_model import (
    BayesNetworkModel,
    NetworkModelError,
    build_market_network,
    build_network_nodes,
    parse_cpt_key,
)

__all__ = [
    "AtomicEventQueryResult",
    "BayesNetworkModel",
    "CacheInvalidationManager",
    "CURRENT_MODEL_COMPILER",
    "CURRENT_MODEL_QUERY_BACKEND",
    "CURRENT_MODEL_EXACT_ELIGIBILITY_REASON",
    "CliqueSummary",
    "CompileResult",
    "CurrentModelCompileArtifact",
    "CurrentModelCompiler",
    "CurrentModelQueryBackend",
    "DEFAULT_ENGINE_CONFIG",
    "EngineConfig",
    "InferenceCompileError",
    "InferenceCompiler",
    "InferenceError",
    "InferenceQueryBackend",
    "InferenceQueryError",
    "InferenceUnsupportedQueryError",
    "InvalidationResult",
    "JointMarket",
    "JointMarketError",
    "MarginalQueryResult",
    "NetworkModelError",
    "build_market_network",
    "build_network_nodes",
    "FactoredMarket",
    "canonical_json_hash",
    "parse_cpt_key",
    "compile_current_market_artifact",
    "compile_current_market_result",
    "compile_current_model_artifact",
    "compile_current_model_result",
]
