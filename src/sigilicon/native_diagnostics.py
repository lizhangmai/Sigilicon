"""Narrow seam for caller-owned native RDB diagnostic semantics."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from sigilicon.paths import ProjectContext


_REQUIRED_CALLABLES = (
    "load_contract",
    "validate_contract",
    "validate_source",
    "nullable_scalar_names",
    "reconstruct",
    "attestation_requirements",
)


@dataclass(frozen=True)
class NativeDiagnosticAdapter:
    """Loaded caller implementation for product-owned diagnostic semantics."""

    source: Path
    implementation: ModuleType

    def load_contract(self, *args: Any, **kwargs: Any) -> Any:
        diagnostic = self.implementation.load_contract(*args, **kwargs)
        support_sources = tuple(
            dict.fromkeys((*diagnostic.support_sources, self.source))
        )
        return replace(diagnostic, support_sources=support_sources)

    def validate_contract(self, *args: Any, **kwargs: Any) -> None:
        self.implementation.validate_contract(*args, **kwargs)

    def validate_source(self, *args: Any, **kwargs: Any) -> tuple[str, ...]:
        return tuple(self.implementation.validate_source(*args, **kwargs))

    def nullable_scalar_names(self, *args: Any, **kwargs: Any) -> tuple[str, ...]:
        return tuple(self.implementation.nullable_scalar_names(*args, **kwargs))

    def reconstruct(self, *args: Any, **kwargs: Any) -> Any:
        return self.implementation.reconstruct(*args, **kwargs)

    def attestation_requirements(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        value = self.implementation.attestation_requirements(*args, **kwargs)
        if not isinstance(value, dict):
            raise TypeError("native diagnostic attestation requirements must be a dict")
        return value


def load_native_diagnostic_adapter(
    context: ProjectContext,
    *,
    owner: str,
) -> NativeDiagnosticAdapter | None:
    """Load the owner adapter explicitly declared by the caller-owned context."""

    source = context.native_diagnostic_adapter_for(owner)
    if source is None:
        return None
    identity = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(
        f"_sigilicon_project_native_diagnostics_{identity}", source
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load native diagnostic adapter: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    missing = [name for name in _REQUIRED_CALLABLES if not callable(getattr(module, name, None))]
    if missing:
        raise RuntimeError(
            f"native diagnostic adapter {source} lacks callables: {', '.join(missing)}"
        )
    return NativeDiagnosticAdapter(source=source, implementation=module)
