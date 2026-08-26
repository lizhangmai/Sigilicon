"""Narrow seam for caller-owned native RDB diagnostic semantics."""

from __future__ import annotations

from dataclasses import dataclass, replace
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Collection, Mapping
import uuid

from sigilicon.domain.repository import RepositoryContext


_REQUIRED_CALLABLES = (
    "load_contract",
    "validate_contract",
    "validate_source",
    "nullable_scalar_names",
    "reconstruct",
    "attestation_requirements",
)


@dataclass(frozen=True)
class NativeDiagnosticContract:
    """Typed scalar-result contract supplied by one testbench."""

    kind: str
    settings: Mapping[str, object]
    scalar_outputs: tuple[tuple[str, str], ...]
    support_sources: tuple[Path, ...] = ()


@dataclass(frozen=True)
class NativeDiagnosticProcessor:
    """Loaded testbench processor consumed by native result lifecycle stages."""

    source: Path
    implementation: ModuleType

    def load_contract(
        self,
        raw: object,
        *,
        contract_path: Path,
        project_root: Path,
    ) -> NativeDiagnosticContract:
        diagnostic = self.implementation.load_contract(
            raw,
            contract_path=contract_path,
            project_root=project_root,
        )
        if not isinstance(diagnostic, NativeDiagnosticContract):
            raise TypeError("native diagnostic loader returned an invalid contract")
        support_sources = tuple(
            dict.fromkeys((*diagnostic.support_sources, self.source))
        )
        return replace(diagnostic, support_sources=support_sources)

    def validate_contract(
        self, diagnostic: NativeDiagnosticContract, *, point_count: int
    ) -> None:
        self.implementation.validate_contract(diagnostic, point_count=point_count)

    def validate_source(
        self, diagnostic: NativeDiagnosticContract, setup_text: str
    ) -> tuple[str, ...]:
        return tuple(self.implementation.validate_source(diagnostic, setup_text))

    def nullable_scalar_names(
        self, diagnostic: NativeDiagnosticContract
    ) -> tuple[str, ...]:
        return tuple(self.implementation.nullable_scalar_names(diagnostic))

    def reconstruct(
        self,
        result: Mapping[str, object],
        contract: object,
    ) -> Mapping[str, object]:
        value = self.implementation.reconstruct(result, contract)
        if not isinstance(value, Mapping):
            raise TypeError("native diagnostic reconstruction must return a mapping")
        return value

    def attestation_requirements(
        self, diagnostic: NativeDiagnosticContract, tests: Collection[str]
    ) -> dict[str, object]:
        value = self.implementation.attestation_requirements(diagnostic, tests)
        if not isinstance(value, dict):
            raise TypeError("native diagnostic attestation requirements must be a dict")
        return value


def load_native_diagnostic_processor(source: Path) -> NativeDiagnosticProcessor:
    """Load one processor explicitly selected by its native RDB contract."""

    source = source.resolve()
    spec = importlib.util.spec_from_file_location(
        f"_sigilicon_project_native_diagnostics_{uuid.uuid4().hex}", source
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load native diagnostic processor: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    missing = [name for name in _REQUIRED_CALLABLES if not callable(getattr(module, name, None))]
    if missing:
        raise RuntimeError(
            f"native diagnostic processor {source} lacks callables: {', '.join(missing)}"
        )
    return NativeDiagnosticProcessor(source=source, implementation=module)


def load_owner_native_diagnostic_processor(
    repository: RepositoryContext,
    *,
    owner_path: Path,
) -> NativeDiagnosticProcessor | None:
    """Load the legacy owner default for unmodified downstream projects."""

    source = repository.owner_file(owner_path, "native_diagnostics")
    if source is None:
        return None
    return load_native_diagnostic_processor(source)
