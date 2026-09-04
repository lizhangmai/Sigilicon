"""Process-isolated execution of project-owned layout generators."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys
from types import MappingProxyType
from typing import Mapping

from sigilicon.domain.netlist import NetlistSnapshot
from sigilicon.external_tools import (
    ProcessRequest,
    managed_process,
    owned_sealed_input,
)
from sigilicon.layout.ir import LayoutPlan
from sigilicon.layout._json import (
    array as _array,
    record as _record,
    record_mapping as _record_mapping,
    strings as _strings,
    text as _text,
)


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
    payload = json.dumps(request, sort_keys=True).encode("utf-8")
    with owned_sealed_input(payload, name="layout-generator.json") as request_file:
        descriptor = request_file.fd
        if descriptor is None:
            raise RuntimeError("layout generator request is not retained")
        completed = managed_process.run(
            ProcessRequest(
                argv=(
                    sys.executable,
                    "-I",
                    "-B",
                    "-m",
                    "sigilicon.layout._generator_worker",
                    str(descriptor),
                ),
                cwd=root,
                environment={},
                timeout_seconds=120,
                pass_fds=(descriptor,),
                before_spawn=request_file.require_sealed,
            )
        )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "worker exited without diagnostics"
        raise RuntimeError(f"layout generator subprocess failed:\n{detail}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("layout generator returned invalid JSON") from exc
    return LayoutPlan.from_payload(payload)


__all__ = ["LayoutGeneratorInput", "build_layout_plan_from_sources"]
