"""The one real variability seam in managed execution."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from sigilicon.execution.model import (
    ContractError,
    PreflightCheck,
    Resources,
    Step,
    StepContext,
    StepResult,
    backend_identity,
)


@runtime_checkable
class Backend(Protocol):
    """Trusted package code selected by a Step's ``uses`` identity.

    A Backend is not a sandboxed owner plugin.  Implementations that launch an
    external tool must use Sigilicon's held-fd process and output primitives.
    """

    name: str

    def preflight(
        self,
        step: Step,
        resources: Resources,
    ) -> tuple[PreflightCheck, ...]: ...

    def run(self, context: StepContext) -> StepResult: ...


class Backends(Mapping[str, Backend]):
    """Immutable explicit backend set; it performs no global registration."""

    def __init__(self, values: Iterable[Backend] = ()) -> None:
        selected: dict[str, Backend] = {}
        for backend in values:
            name = getattr(backend, "name", None)
            try:
                name = backend_identity(name)
            except ContractError as exc:
                raise ContractError("backend must expose a canonical identity") from exc
            if not isinstance(backend, Backend):
                raise ContractError(f"backend {name!r} does not satisfy the Backend interface")
            if name in selected:
                raise ContractError(f"duplicate backend identity: {name!r}")
            selected[name] = backend
        self._values = MappingProxyType(selected)

    def __getitem__(self, name: str) -> Backend:
        return self._values[name]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


__all__ = ["Backend", "Backends"]
