"""Child-process protocol for source-owned native diagnostic programs."""

from __future__ import annotations

from contextlib import redirect_stdout
import json
from pathlib import Path
import sys
from types import MappingProxyType, ModuleType, SimpleNamespace
from typing import Any, Mapping

from sigilicon.contracts import freeze_toml_document
from sigilicon.domain.native_diagnostics import NativeDiagnosticContract
from sigilicon.project_modules import project_import_path


_REQUIRED_CALLABLES = (
    "load_contract",
    "validate_contract",
    "validate_source",
    "nullable_scalar_names",
    "reconstruct",
    "attestation_requirements",
)


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"native diagnostic result is not JSON-safe: {type(value).__name__}")


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _load_program(payload: Mapping[str, Any]) -> ModuleType:
    source = Path(payload["source_path"])
    module = ModuleType("_sigilicon_owner_native_diagnostics")
    module.__file__ = str(source)
    module.__package__ = ""
    exec(compile(payload["source_text"], str(source), "exec"), module.__dict__)
    missing = [
        name for name in _REQUIRED_CALLABLES if not callable(getattr(module, name, None))
    ]
    if missing:
        raise RuntimeError(
            f"native diagnostic program lacks callables: {', '.join(missing)}"
        )
    return module


def _diagnostic(value: Mapping[str, Any]) -> NativeDiagnosticContract:
    return NativeDiagnosticContract(
        kind=str(value["kind"]),
        settings=_freeze(value["settings"]),
        scalar_outputs=tuple(tuple(row) for row in value["scalar_outputs"]),
        support_sources=tuple(Path(path) for path in value["support_sources"]),
    )


def _describe(module: ModuleType, payload: Mapping[str, Any]) -> dict[str, object]:
    documents = payload["source_documents"]
    source_documents = None if not documents else MappingProxyType(
        {
            Path(row["path"]): freeze_toml_document(row["document"])
            for row in documents
        }
    )
    diagnostic = module.load_contract(
        payload["raw"],
        contract_path=Path(payload["contract_path"]),
        project_root=Path(payload["project_root"]),
        source_documents=source_documents,
    )
    if not isinstance(diagnostic, NativeDiagnosticContract):
        raise TypeError("native diagnostic loader returned an invalid contract")
    module.validate_contract(diagnostic, point_count=payload["point_count"])
    missing = tuple(module.validate_source(diagnostic, payload["setup_text"]))
    if missing:
        raise ValueError(
            "native RDB contract is not consistent with setup.il: "
            + ", ".join(map(str, missing))
        )
    nullable = tuple(module.nullable_scalar_names(diagnostic))
    requirements = module.attestation_requirements(
        diagnostic, set(payload["tests"])
    )
    if not isinstance(requirements, dict):
        raise TypeError("native diagnostic attestation requirements must be a dict")
    support = tuple(
        dict.fromkeys((*diagnostic.support_sources, Path(payload["source_path"])))
    )
    return {
        "kind": diagnostic.kind,
        "settings": diagnostic.settings,
        "scalar_outputs": diagnostic.scalar_outputs,
        "support_sources": tuple(map(str, support)),
        "nullable_scalar_names": nullable,
        "attestation_requirements": requirements,
    }


def _reconstruct(module: ModuleType, payload: Mapping[str, Any]) -> dict[str, object]:
    diagnostic = _diagnostic(payload["diagnostic"])
    module.validate_contract(diagnostic, point_count=payload["point_count"])
    contract = SimpleNamespace(
        diagnostic_equivalence=diagnostic,
        point_count=payload["point_count"],
        corners=tuple(payload["corners"]),
        tests=tuple(payload["tests"]),
        scalar_outputs=diagnostic.scalar_outputs,
    )
    report = module.reconstruct(payload["result"], contract)
    if not isinstance(report, Mapping):
        raise TypeError("native diagnostic reconstruction must return a mapping")
    return {"report": report}


def main() -> int:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    root = Path(payload["project_root"])
    with project_import_path(root), redirect_stdout(sys.stderr):
        module = _load_program(payload)
        if payload["action"] == "describe":
            result = _describe(module, payload)
        elif payload["action"] == "reconstruct":
            result = _reconstruct(module, payload)
        else:
            raise ValueError(
                f"unsupported native diagnostic action {payload['action']!r}"
            )
    sys.stdout.write(json.dumps(_json_value(result), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
