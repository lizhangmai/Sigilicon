"""Narrow seam for caller-owned native RDB diagnostic semantics."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any, Collection, Mapping

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

    def load_contract(
        self,
        raw: object,
        *,
        contract_path: Path,
        project_root: Path,
    ) -> Any:
        diagnostic = self.implementation.load_contract(
            raw,
            contract_path=contract_path,
            project_root=project_root,
        )
        support_sources = tuple(
            dict.fromkeys((*diagnostic.support_sources, self.source))
        )
        return replace(diagnostic, support_sources=support_sources)

    def validate_contract(self, diagnostic: Any, *, point_count: int) -> None:
        self.implementation.validate_contract(diagnostic, point_count=point_count)

    def validate_source(
        self, diagnostic: Any, setup_text: str
    ) -> tuple[str, ...]:
        return tuple(self.implementation.validate_source(diagnostic, setup_text))

    def nullable_scalar_names(self, diagnostic: Any) -> tuple[str, ...]:
        return tuple(self.implementation.nullable_scalar_names(diagnostic))

    def reconstruct(self, result: Mapping[str, Any], contract: Any) -> Any:
        return self.implementation.reconstruct(result, contract)

    def attestation_requirements(
        self, diagnostic: Any, tests: Collection[str]
    ) -> dict[str, Any]:
        value = self.implementation.attestation_requirements(diagnostic, tests)
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
