"""Narrow seam for caller-owned native RDB diagnostic semantics."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
import sys
from types import MappingProxyType, ModuleType
from typing import Any, Collection, Mapping, cast
import uuid

from sigilicon.domain.source import TextSourceSnapshot, load_text_source_snapshot


_REQUIRED_CALLABLES = (
    "load_contract",
    "validate_contract",
    "validate_source",
    "nullable_scalar_names",
    "reconstruct",
    "attestation_requirements",
)


@contextmanager
def _project_import_path(project_root: Path) -> Iterator[None]:
    """Expose the explicitly selected project only while loading its processor."""

    root = str(project_root.resolve())
    already_present = root in sys.path
    if not already_present:
        sys.path.insert(0, root)
    try:
        yield
    finally:
        if not already_present:
            sys.path.remove(root)


@dataclass(frozen=True)
class NativeDiagnosticContract:
    """Typed scalar-result contract supplied by one testbench."""

    kind: str
    settings: Mapping[str, object]
    scalar_outputs: tuple[tuple[str, str], ...]
    support_sources: tuple[Path, ...] = ()


@dataclass(frozen=True)
class NativeDiagnosticReport:
    """Owner diagnostic payload with a mandatory top-level verdict."""

    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        payload = MappingProxyType(dict(self.payload))
        if not isinstance(payload.get("passed"), bool):
            raise ValueError(
                "native diagnostic reconstruction must return a top-level passed boolean"
            )
        object.__setattr__(self, "payload", payload)

    @property
    def passed(self) -> bool:
        return cast(bool, self.payload["passed"])

    def as_dict(self) -> dict[str, object]:
        """Return the JSON-serializable owner report without weakening its verdict."""

        return dict(self.payload)


@dataclass(frozen=True)
class NativeDiagnosticProcessor:
    """Loaded testbench processor consumed by native result lifecycle stages."""

    source: Path
    implementation: ModuleType
    source_snapshot: TextSourceSnapshot | None = None

    def __post_init__(self) -> None:
        if (
            self.source_snapshot is not None
            and self.source_snapshot.source_path != self.source
        ):
            raise ValueError("native diagnostic processor source identity drift")

    def load_contract(
        self,
        raw: object,
        *,
        contract_path: Path,
        project_root: Path,
        source_documents: Mapping[Path, Mapping[str, Any]] | None = None,
    ) -> NativeDiagnosticContract:
        diagnostic = self.implementation.load_contract(
            raw,
            contract_path=contract_path,
            project_root=project_root,
            source_documents=source_documents,
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
    ) -> NativeDiagnosticReport:
        value = self.implementation.reconstruct(result, contract)
        if not isinstance(value, Mapping):
            raise TypeError("native diagnostic reconstruction must return a mapping")
        return NativeDiagnosticReport(value)

    def attestation_requirements(
        self, diagnostic: NativeDiagnosticContract, tests: Collection[str]
    ) -> dict[str, object]:
        value = self.implementation.attestation_requirements(diagnostic, tests)
        if not isinstance(value, dict):
            raise TypeError("native diagnostic attestation requirements must be a dict")
        return value


def load_native_diagnostic_processor(
    source: Path, *, project_root: Path
) -> NativeDiagnosticProcessor:
    """Load one processor explicitly selected by its native RDB contract."""

    source = source.resolve()
    root = project_root.resolve()
    if not source.is_relative_to(root):
        raise ValueError("native diagnostic processor must stay inside its project")
    snapshot = load_text_source_snapshot(source)
    module_name = f"_sigilicon_project_native_diagnostics_{uuid.uuid4().hex}"
    module = ModuleType(module_name)
    module.__file__ = str(source)
    module.__package__ = ""
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        with _project_import_path(root):
            exec(compile(snapshot.text, str(source), "exec"), module.__dict__)
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    missing = [name for name in _REQUIRED_CALLABLES if not callable(getattr(module, name, None))]
    if missing:
        raise RuntimeError(
            f"native diagnostic processor {source} lacks callables: {', '.join(missing)}"
        )
    return NativeDiagnosticProcessor(
        source=source,
        implementation=module,
        source_snapshot=snapshot,
    )
