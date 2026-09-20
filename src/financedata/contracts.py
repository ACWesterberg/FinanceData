"""Stable, serialisable data-contract types for optional provenance APIs."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class DataProvenance:
    """How a payload was obtained, without changing legacy payload shapes."""

    provider_names: tuple[str, ...]
    requested_start: str | None
    requested_end: str
    effective_start: str | None
    effective_end: str | None
    retrieved_at: str
    cache_state: str
    completeness: str
    warnings: tuple[str, ...] = ()
    historical_capability: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DataResult(Generic[T]):
    """Payload plus provenance; ``items`` is always the legacy return value."""

    items: T
    provenance: dict[str, DataProvenance] = field(default_factory=dict)

