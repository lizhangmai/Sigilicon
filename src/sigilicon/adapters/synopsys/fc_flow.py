"""Flat, non-DFT FC RTL-to-GDS operation compiled from FC-RM V-2023.12.

The owner supplies design intent, not RM variable names or stage wiring.
The stage graph and checkpoint/export contracts belong to this module.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from collections.abc import Mapping
import re
import tomllib

from sigilicon.domain.hdl import HdlCompilation
from sigilicon.execution._plan import Step
from sigilicon.execution._values import ContractError
from sigilicon.execution.artifact_reference import ArtifactProduct, ArtifactReference, StepContract
from sigilicon.adapters.synopsys._common import _strict_config, _text, _positive_integer
from sigilicon.adapters.synopsys.planning import source

STAGES = ("reference-library", "init", "compile", "cts", "clock-opt",
          "route", "route-opt", "chip-finish", "export")
RM_SCRIPTS = dict(zip(STAGES, ("create_fusion_reference_library", "init_design",
    "compile", "clock_opt_cts", "clock_opt_opto", "route_auto", "route_opt",
    "chip_finish", "write_data")))
LABELS = {stage: RM_SCRIPTS[stage] for stage in STAGES}
METHOD = "fc-rm-v2023.12-flat-rtl-to-gds"
FC_TOOL = "SIGILICON_SYNOPSYS_FC_SHELL"
LC_TOOL = "SIGILICON_SYNOPSYS_LC_SHELL"
FILE_SLOTS = ("SIGILICON_FC_TECH_FILE", "SIGILICON_FC_TECH_LEF",
              "SIGILICON_FC_CELL_LEF", "SIGILICON_FC_CELL_DB",
              "SIGILICON_FC_TECHNOLOGY_SETUP", "SIGILICON_FC_RC_EARLY",
              "SIGILICON_FC_RC_LATE", "SIGILICON_FC_GDS_MAP")
_FIELDS = frozenset({"flow", "design", "design_table", "parameter_bindings",
    "hdl", "constraints", "floorplan", "timeout_seconds", "corner",
    "voltage", "temperature", "process", "cores", "metric", "macros", "pg_connections"})


@dataclass(frozen=True)
class MacroLibrary:
    cell: str
    library: str
    lef: str
    liberty: str
    instances: int

    @classmethod
    def compile(cls, step, config):
        if not isinstance(config, Mapping) or set(config) != {"cell", "library", "lef", "liberty", "instances"}:
            raise ContractError("FC macro requires cell, library, lef, liberty and instances")
        for key in ("cell", "library"):
            if not isinstance(config[key], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", config[key]):
                raise ContractError("invalid FC macro library/cell name")
        selected = replace(step, config=config)
        return cls(config["cell"], config["library"], source(selected, "lef"),
                   source(selected, "liberty"), _positive_integer(config, "instances"))


@dataclass(frozen=True)
class FcAction:
    stage: str
    previous: str | None
    hdl: HdlCompilation
    parameters: tuple[tuple[str, int], ...]
    constraints: str
    floorplan: str
    corner: str
    voltage: float
    temperature: float
    process: float
    cores: int
    metric: str
    timeout_seconds: int
    macros: tuple[MacroLibrary, ...] = ()
    pg_connections: str | None = None

    @property
    def record(self):
        return {"kind": "synopsys.fc", "methodology": METHOD, **asdict(self)}

    @classmethod
    def compile(cls, step: Step):
        import math
        config = _strict_config(step, _FIELDS | {"stage", "previous"})
        if config.get("flow") != "rtl-to-gds":
            raise ContractError("FC supports the flat non-DFT rtl-to-gds flow")
        stage = _text(config, "stage")
        if stage not in STAGES:
            raise ContractError(f"unsupported FC stage {stage!r}")
        previous = None if stage == STAGES[0] else STAGES[STAGES.index(stage) - 1]
        if config.get("previous") != previous:
            raise ContractError("FC stage predecessor disagrees with the RM graph")
        expected_needs = () if previous is None else tuple(dict.fromkeys(("reference-library", previous)))
        if step.needs != expected_needs:
            raise ContractError("FC stage dependencies disagree with the RM graph")
        for slot in ("SIGILICON_RUNNER_SHELL", FC_TOOL, LC_TOOL):
            if slot not in step.runtime.tools:
                raise ContractError(f"FC runtime must bind tool {slot}")
        for slot in FILE_SLOTS:
            if slot not in step.runtime.files:
                raise ContractError(f"FC runtime must bind file {slot}")
        path = source(step, "design")
        document = tomllib.loads(next(s for s in step.source_closure if s.path == path).read_text())
        for key in _text(config, "design_table").split("."):
            if not isinstance(document, dict) or key not in document:
                raise ContractError("FC design_table does not select an owner design table")
            document = document[key]
        bindings = config.get("parameter_bindings")
        if not isinstance(bindings, Mapping) or not isinstance(document, dict):
            raise ContractError("FC parameter_bindings must map RTL parameters to design fields")
        parameters = []
        for name, field in bindings.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ContractError("invalid RTL parameter name")
            if not isinstance(field, str):
                raise ContractError("FC parameter binding must name a design field")
            value = document.get(field)
            if type(value) is not int:
                raise ContractError(f"FC parameter {name} requires an integer design field")
            parameters.append((name, value))
        hdl = HdlCompilation.resolve(config.get("hdl"), {s.reference: s.path for s in step.source_closure})
        if document.get("top_module") != hdl.top:
            raise ContractError("FC design top disagrees with HDL top")
        numbers = []
        for key in ("voltage", "temperature", "process"):
            value = config.get(key)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ContractError(f"FC {key} must be a finite number")
            if key != "temperature" and value <= 0:
                raise ContractError(f"FC {key} must be positive")
            numbers.append(float(value))
        metric = _text(config, "metric")
        if metric not in {"timing", "area", "total_power", "leakage_power"}:
            raise ContractError("unsupported FC QoR metric")
        macro_config = config.get("macros", ())
        if not isinstance(macro_config, (list, tuple)):
            raise ContractError("FC macros must be a list")
        macros = tuple(MacroLibrary.compile(step, item) for item in macro_config)
        if len({m.cell for m in macros}) != len(macros) or len({m.library for m in macros}) != len(macros):
            raise ContractError("FC macro cells and libraries must be unique")
        return cls(stage, previous, hdl, tuple(sorted(parameters)),
            source(step, "constraints"), source(step, "floorplan"),
            _text(config, "corner"), *numbers, _positive_integer(config, "cores"),
            metric, _positive_integer(config, "timeout_seconds"), macros,
            source(step, "pg_connections") if "pg_connections" in config else None)

    @property
    def contract(self):
        logs = ArtifactProduct("log", "log.synopsys", "many")
        facts = ArtifactProduct("execution-verdict", "evidence.tool-verdict", path="verdict.json")
        if self.stage == "reference-library":
            return StepContract(produces=(logs, facts,
                ArtifactProduct("reference-library", "library.synopsys-ndm", "many")))
        consumes = [ArtifactReference("reference-library", "reference-library", "library.synopsys-ndm", "many")]
        if self.previous != "reference-library":
            consumes.extend((
                ArtifactReference(self.previous, "checkpoint", "checkpoint.synopsys-dlib-tar"),
                ArtifactReference(self.previous, "checkpoint-metadata", "evidence.fc-checkpoint"),
            ))
        outputs = [logs, facts,
            ArtifactProduct("checkpoint", "checkpoint.synopsys-dlib-tar", path="design.dlib.tar"),
            ArtifactProduct("checkpoint-metadata", "evidence.fc-checkpoint", path="checkpoint.json"),
            ArtifactProduct("report", "report.synopsys", "many")]
        if self.stage == "export":
            outputs.append(ArtifactProduct("implementation", "implementation.fc-export", "many"))
            outputs.extend(ArtifactProduct(role, kind, path=path) for role, kind, path in (
                ("routed-netlist", "netlist.verilog", "design.v"),
                ("timing-netlist", "netlist.verilog", "design.pt.v"),
                ("lvs-netlist", "netlist.verilog", "design.lvs.v"),
                ("layout-stream", "layout.gds", "design.gds"),
                ("routed-def", "layout.def", "design.def"),
            ))
            outputs.append(ArtifactProduct("parasitics", "parasitics.spef", "many"))
        return StepContract(tuple(consumes), tuple(outputs))


def compile_steps(step: Step) -> tuple[Step, ...]:
    _strict_config(step, _FIELDS)
    result = []
    for i, stage in enumerate(STAGES):
        previous = STAGES[i - 1] if i else None
        needs = () if previous is None else tuple(dict.fromkeys(("reference-library", previous)))
        selected = replace(step, id=stage, needs=needs,
            config={**step.config, "stage": stage,
                    **({"previous": previous} if previous else {})})
        FcAction.compile(selected)
        result.append(selected)
    return tuple(result)
