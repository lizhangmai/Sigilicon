"""Exact identities and semantic selection of release artifact views."""

from __future__ import annotations

from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Mapping
from sigilicon.canonical import canonical_json


def view_condition(value: object) -> Mapping[str, str | int | float | bool]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) or not key
        or type(item) not in {str, int, float, bool}
        or (isinstance(item, str) and not item)
        or (isinstance(item, float) and not math.isfinite(item))
        for key, item in value.items()
    ):
        raise ValueError("view condition must map names to finite scalar values")
    return MappingProxyType(dict(sorted(value.items())))


@dataclass(frozen=True)
class ViewSelector:
    """Select one view by purpose and optional exact variant and condition."""

    role: str
    variant: str | None = None
    condition: Mapping[str, str | int | float | bool] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role:
            raise ValueError("view selector requires a role")
        if self.variant is not None and (not isinstance(self.variant, str) or not self.variant):
            raise ValueError("view selector variant must be non-empty text")
        if self.condition is not None:
            object.__setattr__(self, "condition", view_condition(self.condition))

    def matches(self, *, role: str, variant: str | None, condition: Mapping) -> bool:
        return (self.role == role and (self.variant is None or self.variant == variant)
                and (self.condition is None or canonical_json(dict(self.condition)) == canonical_json(dict(condition))))
