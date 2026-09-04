"""Isolated execution seam for caller-owned native RDB diagnostics."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
from pathlib import Path
import sys
from types import MappingProxyType
from typing import Any, cast

from sigilicon.domain.source import TextSourceSnapshot, load_text_source_snapshot
from sigilicon.external_tools import ProcessRequest, managed_process, owned_sealed_input


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"native diagnostic value is not JSON-safe: {type(value).__name__}")


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class NativeDiagnosticContract:
    """Typed scalar-result contract returned by an isolated owner program."""

    kind: str
    settings: Mapping[str, object]
    scalar_outputs: tuple[tuple[str, str], ...]
    support_sources: tuple[Path, ...] = ()
    nullable_scalar_names: tuple[str, ...] = ()
    attestation_requirements: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )


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
        return dict(self.payload)


@dataclass(frozen=True)
class NativeDiagnosticProgram:
    """Immutable owner program invoked only in a supervised child interpreter."""

    source_snapshot: TextSourceSnapshot
    project_root: Path

    def __post_init__(self) -> None:
        root = self.project_root.resolve()
        if root != self.project_root or not self.source.is_relative_to(root):
            raise ValueError("native diagnostic program must stay inside its project")

    @property
    def source(self) -> Path:
        return self.source_snapshot.source_path

    def _invoke(self, action: str, payload: Mapping[str, object]) -> Mapping[str, Any]:
        request = {
            "action": action,
            "project_root": str(self.project_root),
            "source_path": str(self.source),
            "source_text": self.source_snapshot.text,
            **payload,
        }
        encoded = json.dumps(_json_value(request), separators=(",", ":")).encode()
        with owned_sealed_input(encoded, name="native-diagnostic.json") as owned:
            assert owned.fd is not None
            completed = managed_process.run(
                ProcessRequest(
                    argv=(
                        sys.executable,
                        "-m",
                        "sigilicon.domain._native_diagnostic_worker",
                        owned.child_path,
                    ),
                    executable=sys.executable,
                    cwd=self.project_root,
                    environment={},
                    timeout_seconds=30,
                    pass_fds=(owned.fd,),
                )
            )
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                f"native diagnostic program failed ({completed.returncode}): {detail}"
            )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("native diagnostic program returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise RuntimeError("native diagnostic program returned a non-object result")
        return result

    def describe(
        self,
        raw: object,
        *,
        contract_path: Path,
        source_documents: Mapping[Path, Mapping[str, Any]] | None,
        point_count: int,
        tests: tuple[str, ...],
        setup_text: str,
    ) -> NativeDiagnosticContract:
        documents = [] if source_documents is None else [
            {"path": str(path), "document": _json_value(document)}
            for path, document in source_documents.items()
        ]
        result = self._invoke(
            "describe",
            {
                "raw": _json_value(raw),
                "contract_path": str(contract_path),
                "source_documents": documents,
                "point_count": point_count,
                "tests": tests,
                "setup_text": setup_text,
            },
        )
        kind = result.get("kind")
        settings = _freeze(result.get("settings"))
        outputs = result.get("scalar_outputs")
        sources = result.get("support_sources")
        nullable = result.get("nullable_scalar_names")
        requirements = _freeze(result.get("attestation_requirements"))
        if not isinstance(kind, str) or not kind or not isinstance(settings, Mapping):
            raise RuntimeError("native diagnostic program returned an invalid contract")
        if not isinstance(outputs, list) or any(
            not isinstance(row, list)
            or len(row) != 2
            or any(not isinstance(value, str) or not value for value in row)
            for row in outputs
        ):
            raise RuntimeError("native diagnostic program returned invalid scalar outputs")
        if not isinstance(sources, list) or any(
            not isinstance(value, str) or not value for value in sources
        ):
            raise RuntimeError("native diagnostic program returned invalid support sources")
        if not isinstance(nullable, list) or any(
            not isinstance(value, str) or not value for value in nullable
        ):
            raise RuntimeError("native diagnostic program returned invalid nullable outputs")
        if not isinstance(requirements, Mapping):
            raise RuntimeError(
                "native diagnostic program returned invalid attestation requirements"
            )
        return NativeDiagnosticContract(
            kind=kind,
            settings=settings,
            scalar_outputs=tuple((row[0], row[1]) for row in outputs),
            support_sources=tuple(Path(value) for value in sources),
            nullable_scalar_names=tuple(nullable),
            attestation_requirements=requirements,
        )

    def reconstruct(
        self,
        result: Mapping[str, object],
        diagnostic: NativeDiagnosticContract,
        *,
        point_count: int,
        corners: tuple[str, ...],
        tests: tuple[str, ...],
    ) -> NativeDiagnosticReport:
        response = self._invoke(
            "reconstruct",
            {
                "result": _json_value(result),
                "diagnostic": {
                    "kind": diagnostic.kind,
                    "settings": _json_value(diagnostic.settings),
                    "scalar_outputs": diagnostic.scalar_outputs,
                    "support_sources": tuple(map(str, diagnostic.support_sources)),
                },
                "point_count": point_count,
                "corners": corners,
                "tests": tests,
            },
        )
        report = response.get("report")
        if not isinstance(report, Mapping):
            raise TypeError("native diagnostic reconstruction must return a mapping")
        return NativeDiagnosticReport(cast(Mapping[str, object], _freeze(dict(report))))


def load_native_diagnostic_program(
    source: Path, *, project_root: Path
) -> NativeDiagnosticProgram:
    source = source.resolve()
    root = project_root.resolve()
    if not source.is_relative_to(root):
        raise ValueError("native diagnostic program must stay inside its project")
    return NativeDiagnosticProgram(
        source_snapshot=load_text_source_snapshot(source),
        project_root=root,
    )
