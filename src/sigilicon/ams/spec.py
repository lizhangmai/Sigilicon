"""Load and validate separate design, AMS-test, and PDK configuration."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sigilicon.domain.design import DesignSpec, load_design_spec
from sigilicon.domain.netlist import discover_subckts


IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
TOKEN_RE = re.compile(r"[A-Za-z0-9_.+\-]+\Z")
CAPACITANCE_RE = re.compile(
    r"(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+\-]?\d+)?)"
    r"(?P<suffix>a|f|p|n|u|m|k|meg|g)?\Z",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CheckerSpec:
    settle: str


@dataclass(frozen=True)
class TimingSpec:
    stop: str
    maxstep: str


@dataclass(frozen=True)
class InterfaceSpec:
    vdd: float
    load_cap: str
    rise_time: str
    vthi: float
    vtlo: float
    connect_rules: str


@dataclass(frozen=True)
class StandaloneBackendSpec:
    dump_vcd: bool


@dataclass(frozen=True)
class AdeBackendSpec:
    errpreset: str


@dataclass(frozen=True)
class BackendSpec:
    standalone: StandaloneBackendSpec
    ade: AdeBackendSpec


@dataclass(frozen=True)
class SimulationSpec:
    checker: CheckerSpec
    timing: TimingSpec
    interface: InterfaceSpec
    backends: BackendSpec


@dataclass(frozen=True)
class VectorSpec:
    inputs: tuple[int, ...]
    expected: tuple[int, ...]


@dataclass(frozen=True)
class AmsSpec:
    path: Path
    design: DesignSpec
    testbench: str
    simulation: SimulationSpec
    vectors: tuple[VectorSpec, ...]

    @property
    def wrapper_cell(self) -> str:
        return f"{self.design.cell}_ams"


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read TOML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"TOML root must be a table: {path}")
    return value


def _table(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a table")
    return value


def _string(value: Any, field: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    pattern = IDENTIFIER_RE if identifier else TOKEN_RE
    if not pattern.fullmatch(value):
        raise ValueError(f"{field} contains unsupported characters: {value!r}")
    return value


def _bits(value: Any, width: int, field: str) -> tuple[int, ...]:
    if (
        not isinstance(value, list)
        or len(value) != width
        or any(type(bit) is not int or bit not in (0, 1) for bit in value)
    ):
        raise ValueError(f"{field} must contain exactly {width} binary values")
    return tuple(value)


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    return float(value)


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be a boolean")
    return value


def _capacitance(value: Any, field: str) -> str:
    token = _string(value, field)
    match = CAPACITANCE_RE.fullmatch(token)
    if match is None or float(match.group("number")) <= 0:
        raise ValueError(f"{field} must be a positive capacitance value")
    return token


def load_ams_spec(path: Path, *, project_root: Path | None = None) -> AmsSpec:
    spec_path = path.resolve()
    root = (project_root or spec_path.parents[2]).resolve()
    raw = _read_toml(spec_path)
    test = _table(raw.get("test"), "test")
    simulation = _table(raw.get("simulation"), "simulation")
    checker = _table(simulation.get("checker"), "simulation.checker")
    timing = _table(simulation.get("timing"), "simulation.timing")
    interface = _table(simulation.get("interface"), "simulation.interface")
    if "standalone_load_cap" in simulation or "standalone_load_cap" in interface:
        raise ValueError(
            "standalone_load_cap is obsolete; use simulation.interface.load_cap"
        )
    backends = _table(simulation.get("backends"), "simulation.backends")
    standalone = _table(
        backends.get("standalone"),
        "simulation.backends.standalone",
    )
    ade = _table(backends.get("ade"), "simulation.backends.ade")
    design_value = test.get("design")
    if not isinstance(design_value, str) or not design_value:
        raise ValueError("test.design must be a non-empty path")
    design = load_design_spec(spec_path.parent / design_value, project_root=root)
    testbench = _string(test.get("testbench"), "test.testbench", identifier=True)
    if design.inouts:
        raise ValueError(
            "generic AMS truth-table flow does not support bidirectional ports"
        )
    if not design.inputs or not design.outputs:
        raise ValueError(
            "generic AMS truth-table flow requires at least one input and one output"
        )
    source_cells = discover_subckts(design.netlist_snapshot)
    generated_names = {testbench, f"{design.cell}_ams"}
    collisions = sorted(generated_names.intersection(source_cells))
    if collisions:
        raise ValueError(
            "generated AMS cell names collide with canonical source subckts: "
            + ", ".join(collisions)
        )
    if testbench == f"{design.cell}_ams":
        raise ValueError("test.testbench must differ from the generated AMS wrapper name")

    sim = SimulationSpec(
        checker=CheckerSpec(
            settle=_string(checker.get("settle"), "simulation.checker.settle"),
        ),
        timing=TimingSpec(
            stop=_string(timing.get("stop"), "simulation.timing.stop"),
            maxstep=_string(timing.get("maxstep"), "simulation.timing.maxstep"),
        ),
        interface=InterfaceSpec(
            vdd=_number(interface.get("vdd"), "simulation.interface.vdd"),
            load_cap=_capacitance(
                interface.get("load_cap"),
                "simulation.interface.load_cap",
            ),
            rise_time=_string(
                interface.get("rise_time"),
                "simulation.interface.rise_time",
            ),
            vthi=_number(interface.get("vthi"), "simulation.interface.vthi"),
            vtlo=_number(interface.get("vtlo"), "simulation.interface.vtlo"),
            connect_rules=_string(
                interface.get("connect_rules"),
                "simulation.interface.connect_rules",
            ),
        ),
        backends=BackendSpec(
            standalone=StandaloneBackendSpec(
                dump_vcd=_boolean(
                    standalone.get("dump_vcd"),
                    "simulation.backends.standalone.dump_vcd",
                ),
            ),
            ade=AdeBackendSpec(
                errpreset=_string(
                    ade.get("errpreset"),
                    "simulation.backends.ade.errpreset",
                ),
            ),
        ),
    )
    interface_spec = sim.interface
    if (
        interface_spec.vdd <= 0
        or not 0
        <= interface_spec.vtlo
        < interface_spec.vthi
        <= interface_spec.vdd
    ):
        raise ValueError("simulation thresholds must satisfy 0 <= vtlo < vthi <= vdd")

    vectors_raw = raw.get("vectors")
    if not isinstance(vectors_raw, list) or not vectors_raw:
        raise ValueError("vectors must be a non-empty array of tables")
    vectors: list[VectorSpec] = []
    for index, value in enumerate(vectors_raw):
        item = _table(value, f"vectors[{index}]")
        vectors.append(
            VectorSpec(
                inputs=_bits(item.get("inputs"), len(design.inputs), f"vectors[{index}].inputs"),
                expected=_bits(
                    item.get("expected"), len(design.outputs), f"vectors[{index}].expected"
                ),
            )
        )
    return AmsSpec(
        path=spec_path,
        design=design,
        testbench=testbench,
        simulation=sim,
        vectors=tuple(vectors),
    )
