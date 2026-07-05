"""Engine identity and config values shared across the Bayes backend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class EngineConfig:
    """Describe the exported identity and runtime settings of the inference engine."""

    mode: str
    backend: str
    version: str
    precision: str
    compile_type: str
    inference_sample_limit: int
    max_treewidth: int = 8

    def __post_init__(self) -> None:
        if not self.mode:
            raise ValueError("Engine mode must be non-empty")
        if not self.backend:
            raise ValueError("Engine backend must be non-empty")
        if not self.version:
            raise ValueError("Engine version must be non-empty")
        if not self.precision:
            raise ValueError("Engine precision must be non-empty")
        if not self.compile_type:
            raise ValueError("Engine compile_type must be non-empty")
        if self.inference_sample_limit < 0:
            raise ValueError("Engine inference_sample_limit must be non-negative")
        if self.max_treewidth < 0:
            raise ValueError("Engine max_treewidth must be non-negative")


DEFAULT_ENGINE_CONFIG: Final[EngineConfig] = EngineConfig(
    mode="EXACT",
    backend="junction_tree",
    version="0.1.0",
    precision="float64",
    compile_type="junction_tree",
    inference_sample_limit=100,
)

__all__: list[str] = ["DEFAULT_ENGINE_CONFIG", "EngineConfig"]
