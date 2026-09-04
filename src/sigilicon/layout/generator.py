"""Process-isolated execution of project-owned layout generators."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import MappingProxyType, ModuleType
from typing import Any, Mapping

from sigilicon.domain.netlist import NetlistSnapshot
from sigilicon.external_tools import ProcessRequest, managed_process
from sigilicon.layout.ir import LayoutPlan


@dataclass(frozen=True)
class LayoutGeneratorInput:
    """Sealed design facts exposed to owner-authored layout code."""

    library: str
    cell: str
    view: str
    generator: str
    stage: str
    source_snapshot: NetlistSnapshot
    source_snapshots: tuple[NetlistSnapshot, ...]
    ports: tuple[str, ...]
    directions: Mapping[str, str]
    primitive_masters: tuple[str, ...]
    technology_library: str
    dbu_per_micron: int

    def payload(self) -> dict[str, object]:
        return {
            "library": self.library,
            "cell": self.cell,
            "view": self.view,
            "generator": self.generator,
            "stage": self.stage,
            "source_snapshot": _snapshot_payload(self.source_snapshot),
            "source_snapshots": [
                _snapshot_payload(snapshot) for snapshot in self.source_snapshots
            ],
            "ports": list(self.ports),
            "directions": dict(self.directions),
            "primitive_masters": list(self.primitive_masters),
            "technology_library": self.technology_library,
            "dbu_per_micron": self.dbu_per_micron,
        }

    @classmethod
    def from_payload(cls, value: object) -> "LayoutGeneratorInput":
        raw = _record(
            value,
            "layout generator input",
            {
                "library",
                "cell",
                "view",
                "generator",
                "stage",
                "source_snapshot",
                "source_snapshots",
                "ports",
                "directions",
                "primitive_masters",
                "technology_library",
                "dbu_per_micron",
            },
        )
        snapshots = tuple(
            _snapshot_from_payload(item, f"source_snapshots[{index}]")
            for index, item in enumerate(
                _array(raw["source_snapshots"], "source_snapshots")
            )
        )
        source_snapshot = _snapshot_from_payload(
            raw["source_snapshot"], "source_snapshot"
        )
        if source_snapshot not in snapshots:
            raise ValueError("source_snapshot must belong to source_snapshots")
        directions_raw = _record_mapping(raw["directions"], "directions")
        directions = MappingProxyType(
            {
                _text(name, "direction name"): _text(direction, f"directions.{name}")
                for name, direction in directions_raw.items()
            }
        )
        dbu = raw["dbu_per_micron"]
        if isinstance(dbu, bool) or not isinstance(dbu, int) or dbu <= 0:
            raise ValueError("dbu_per_micron must be a positive integer")
        return cls(
            library=_text(raw["library"], "library"),
            cell=_text(raw["cell"], "cell"),
            view=_text(raw["view"], "view"),
            generator=_text(raw["generator"], "generator"),
            stage=_text(raw["stage"], "stage"),
            source_snapshot=source_snapshot,
            source_snapshots=snapshots,
            ports=_strings(raw["ports"], "ports"),
            directions=directions,
            primitive_masters=_strings(raw["primitive_masters"], "primitive_masters"),
            technology_library=_text(
                raw["technology_library"], "technology_library"
            ),
            dbu_per_micron=dbu,
        )


def _snapshot_payload(snapshot: NetlistSnapshot) -> dict[str, object]:
    return {
        "source_path": str(snapshot.source_path),
        "text": snapshot.text,
        "interfaces": {
            name: list(ports) for name, ports in snapshot.interfaces.items()
        },
    }


def _snapshot_from_payload(value: object, label: str) -> NetlistSnapshot:
    raw = _record(value, label, {"source_path", "text", "interfaces"})
    interfaces_raw = _record_mapping(raw["interfaces"], f"{label}.interfaces")
    return NetlistSnapshot(
        source_path=Path(_text(raw["source_path"], f"{label}.source_path")),
        text=_text(raw["text"], f"{label}.text", empty=True),
        interfaces=MappingProxyType(
            {
                _text(name, f"{label}.interfaces key"): _strings(
                    ports, f"{label}.interfaces.{name}"
                )
                for name, ports in interfaces_raw.items()
            }
        ),
    )


def _record(
    value: object,
    label: str,
    fields: set[str],
) -> Mapping[str, Any]:
    raw = _record_mapping(value, label)
    if set(raw) != fields:
        raise ValueError(
            f"{label} fields disagree: missing={sorted(fields - set(raw))}, "
            f"unknown={sorted(set(raw) - fields)}"
        )
    return raw


def _record_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise ValueError(f"{label} must be an object")
    return value


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not empty):
        raise ValueError(f"{label} must be text")
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    result = tuple(_text(item, f"{label}[]") for item in _array(value, label))
    if len(set(result)) != len(result):
        raise ValueError(f"{label} contains duplicates")
    return result


def _load_generator_module(source: Path) -> ModuleType:
    module_spec = importlib.util.spec_from_file_location(
        "_sigilicon_layout_generator", source
    )
    if module_spec is None or module_spec.loader is None:
        raise ValueError(f"cannot load layout generator source: {source}")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module.__name__] = module
    module_spec.loader.exec_module(module)
    return module


def execute_layout_generator(
    spec: LayoutGeneratorInput,
    *,
    project_root: Path,
    generator_source: Path,
) -> LayoutPlan:
    """Worker-side generator execution inside one disposable interpreter."""

    root = project_root.absolute()
    source = generator_source.absolute()
    if root != root.resolve() or source != source.resolve():
        raise ValueError("layout generator paths must not traverse symlinks")
    if not source.is_file() or not source.is_relative_to(root):
        raise ValueError("layout generator must be a file inside the sealed project")
    sys.path.insert(0, str(root))
    module = _load_generator_module(source)
    entrypoint = getattr(module, "build_layout_plan", None)
    if not callable(entrypoint):
        raise ValueError(f"layout generator {source} must export build_layout_plan")
    plan = entrypoint(spec)
    if not isinstance(plan, LayoutPlan):
        raise TypeError(
            f"layout generator {source} returned {type(plan).__name__}, "
            "expected LayoutPlan"
        )
    identity = (plan.library, plan.cell, plan.view, plan.generator, plan.stage)
    expected = (spec.library, spec.cell, spec.view, spec.generator, spec.stage)
    if identity != expected:
        raise ValueError(
            f"layout generator changed spec identity: got={identity}, expected={expected}"
        )
    return plan


def build_layout_plan_from_sources(
    spec: LayoutGeneratorInput,
    *,
    project_root: Path,
    generator_source: Path,
) -> LayoutPlan:
    """Run sealed owner code through the JSON subprocess boundary."""

    if not isinstance(spec, LayoutGeneratorInput):
        raise TypeError("layout generator input must be LayoutGeneratorInput")
    root = project_root.resolve()
    source = generator_source.absolute()
    if source != source.resolve() or not source.is_relative_to(root):
        raise ValueError("layout generator must be inside the sealed project")
    request = {
        "schema": 1,
        "project_root": str(root),
        "generator_source": source.relative_to(root).as_posix(),
        "spec": spec.payload(),
    }
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=".sigilicon-layout-generator-",
        suffix=".json",
        dir=root,
        delete=False,
    ) as stream:
        json.dump(request, stream, sort_keys=True)
        request_path = Path(stream.name)
    try:
        completed = managed_process.run(
            ProcessRequest(
                argv=(
                    sys.executable,
                    "-I",
                    "-B",
                    "-m",
                    "sigilicon.layout._generator_worker",
                    str(request_path),
                ),
                cwd=root,
                environment={},
                timeout_seconds=120,
            )
        )
    finally:
        request_path.unlink(missing_ok=True)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "worker exited without diagnostics"
        raise RuntimeError(f"layout generator subprocess failed:\n{detail}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("layout generator returned invalid JSON") from exc
    return LayoutPlan.from_payload(payload)


__all__ = ["LayoutGeneratorInput", "build_layout_plan_from_sources"]
