"""Design Compiler plugin with design-owned Tcl and framework-owned evidence.

Tcl controls analysis, linking, mapping and reporting. Configuration binds the
sealed design parameters, library artifacts and acceptance; no IP Python or
shell runner participates in the execution/result protocol.
"""
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import re
import tomllib
from collections.abc import Mapping

from sigilicon.adapters.synopsys._common import (
    _hdl_environment, _logs, _mapping, _positive_integer, _runtime_environment,
    _safe_relative, _strict_config, _strings, _text,
)
from sigilicon.adapters.synopsys.dc_reports import TimingCoverage, area_evidence, timing_evidence
from sigilicon.adapters.synopsys.planning import source
from sigilicon.adapters.synopsys.tcl import completion, run_tcl, verdict
from sigilicon.domain.hdl import HdlCompilation
from sigilicon.execution.adapter import AdapterPreparation
from sigilicon.execution.artifact_reference import ArtifactProduct, ArtifactReference, StepContract
from sigilicon.execution._result import Artifact, StepResult
from sigilicon.execution._values import ContractError
from sigilicon.execution.runtime import preflight_environment
from sigilicon.external_tools import owned_scratch_directory, process_group_cleanup_uncertainty

TOOL = "SIGILICON_SYNOPSYS_DC_SHELL"
TARGET = "SIGILICON_DC_TARGET_DB"


def _table(document, selector):
    for key in selector.split("."):
        if not isinstance(document, Mapping) or key not in document:
            raise ContractError(f"DC design contract omits table {selector!r}")
        document = document[key]
    if not isinstance(document, Mapping):
        raise ContractError(f"DC {selector!r} must select a table")
    return document


@dataclass(frozen=True)
class DcAction:
    script: str
    variant: str
    corner: str
    constraints: str
    hdl: HdlCompilation
    parameters: tuple[tuple[str, int], ...]
    libraries: tuple[ArtifactReference, ...]
    inputs: tuple[tuple[str, ArtifactReference], ...]
    reports: tuple[str, ...]
    timing: TimingCoverage
    acceptance: tuple[str, ...]
    expected_macros: int
    success_marker: str
    timeout_seconds: int

    @property
    def record(self):
        return {"kind": "synopsys.dc", **asdict(self)}

    @property
    def contract(self):
        return StepContract(consumes=(*self.libraries, *(ref for _, ref in self.inputs)), produces=(
            ArtifactProduct("log", "log.synopsys", "many"),
            ArtifactProduct("mapped-netlist", "netlist.verilog", path="mapped.v"),
            ArtifactProduct("mapped-constraints", "constraints.sdc", path="mapped.sdc"),
            ArtifactProduct("checkpoint", "checkpoint.synopsys-ddc", path="mapped.ddc"),
            *(ArtifactProduct("report", "report.synopsys", path=f"reports/{name}") for name in self.reports),
            ArtifactProduct("measurements", "evidence.synthesis", path="measurements.json"),
            ArtifactProduct("execution-verdict", "evidence.tool-verdict", path="verdict.json")))

    @classmethod
    def compile(cls, step):
        config = _strict_config(step, frozenset({
            "script", "variant", "corner", "constraints", "hdl", "reports",
            "design", "design_table", "parameter_bindings", "libraries", "timing",
            "acceptance_table", "macro_count_field", "success_marker", "timeout_seconds", "inputs",
        }))
        if TOOL not in step.runtime.tools or TARGET not in step.runtime.files:
            raise ContractError(f"DC runtime must bind tool {TOOL} and file {TARGET}")
        design_path = source(step, "design")
        document = tomllib.loads(next(s for s in step.source_closure if s.path == design_path).read_text())
        design = _table(document, _text(config, "design_table"))
        hdl = HdlCompilation.resolve(config.get("hdl"), {s.reference: s.path for s in step.source_closure})
        if design.get("top_module") != hdl.top:
            raise ContractError("DC design top disagrees with HDL top")
        parameters = []
        for name, field in _mapping(config, "parameter_bindings").items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or not isinstance(field, str):
                raise ContractError("DC parameter bindings require identifiers and design fields")
            value = design.get(field)
            if type(value) is not int:
                raise ContractError(f"DC parameter {name} requires an integer design field")
            parameters.append((name, value))
        libraries = config.get("libraries", ())
        if not isinstance(libraries, tuple):
            raise ContractError("DC libraries must be an array of artifact references")
        libraries = tuple(ArtifactReference.from_record(raw) for raw in libraries)
        if (len(set(libraries)) != len(libraries) or any(
                item.step not in step.needs or item.kind != "library.synopsys-db"
                for item in libraries)):
            raise ContractError("DC libraries require unique DB artifacts from declared dependencies")
        raw_inputs = config.get("inputs", {})
        if not isinstance(raw_inputs, Mapping):
            raise ContractError("DC inputs must map environment suffixes to artifact references")
        inputs = []
        for name, raw in raw_inputs.items():
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
                raise ContractError("DC input names must be uppercase environment suffixes")
            reference = ArtifactReference.from_record(raw)
            if reference.step not in step.needs:
                raise ContractError("DC inputs must consume declared dependencies")
            inputs.append((name, reference))
        reports = tuple(_safe_relative(name, "DC report") for name in _strings(config, "reports"))
        if (len(set(reports)) != len(reports) or not {"qor.rpt", "accounting.rpt"} <= set(reports)
                or set(reports) & {"mapped.v", "mapped.sdc", "mapped.ddc", "measurements.json", "verdict.json"}):
            raise ContractError("DC reports require unique native qor.rpt and accounting.rpt outputs")
        timing = _mapping(config, "timing")
        if set(timing) != {"clock", "frequency_field", "path_groups"}:
            raise ContractError("DC timing requires clock, frequency_field and path_groups")
        frequency = design.get(_text(timing, "frequency_field"))
        if type(frequency) not in (int, float) or not math.isfinite(frequency) or frequency <= 0:
            raise ContractError("DC clock frequency requires a finite positive design field")
        groups = []
        for pattern, count in _mapping(timing, "path_groups").items():
            if not pattern or any(c in pattern for c in "\r\n\x00"):
                raise ContractError("DC path group patterns must be non-empty single-line text")
            count = design.get(count) if isinstance(count, str) else count
            if type(count) is not int or count <= 0:
                raise ContractError("DC path group count requires a positive integer or design field")
            groups.append((pattern, count))
        if not groups:
            raise ContractError("DC timing coverage cannot be empty")
        acceptance = _table(document, _text(config, "acceptance_table")) if "acceptance_table" in config else {}
        if (set(acceptance) - {"no_setup_violations", "no_hold_violations"}
                or any(type(value) is not bool for value in acceptance.values())):
            raise ContractError("DC acceptance requires explicit boolean setup/hold policies")
        macros = design.get(_text(config, "macro_count_field")) if "macro_count_field" in config else 0
        if type(macros) is not int or macros < 0:
            raise ContractError("DC macro count requires a non-negative integer design field")
        return cls(source(step, "script"), _text(config, "variant"), _text(config, "corner"),
                   source(step, "constraints"), hdl, tuple(parameters), libraries, tuple(inputs), reports,
                   TimingCoverage(_text(timing, "clock"), 1000.0 / frequency, tuple(groups)),
                   tuple(key for key, enabled in acceptance.items() if enabled), macros,
                   _text(config, "success_marker"), _positive_integer(config, "timeout_seconds"))


class DcAdapter:
    name = "synopsys.dc"

    def contract(self, project, step):
        return DcAction.compile(step).contract

    def prepare(self, project, step, resources):
        return AdapterPreparation(action=DcAction.compile(step))

    def preflight(self, step, resources):
        return preflight_environment(step.runtime, resources)

    def run(self, context):
        context.step.validate_action()
        action = context.step.action
        if not isinstance(action, DcAction):
            raise ContractError("DC requires a compiled action")
        runtime = _runtime_environment(context.runtime, context.step)
        hdl = _hdl_environment(context, action.hdl)
        libraries = tuple(artifact.path for reference in action.libraries for artifact in context.artifacts(reference))
        inputs = {}
        for name, reference in action.inputs:
            artifacts = context.artifacts(reference)
            if len(artifacts) != 1:
                raise ContractError("DC named input must select one artifact")
            inputs[f"SIGILICON_DC_INPUT_{name}"] = artifacts[0].path
        library_list = context.workspace("synopsys", {}, tool_work_root=context.work_directory).write_text(
            "work", ("libraries.f",), "".join(f"{path}\n" for path in libraries))
        with owned_scratch_directory(prefix=f"sigilicon-dc-{context.run_id}-",
                retain_on_error=lambda exc: process_group_cleanup_uncertainty(exc) is not None) as scratch:
            completed = run_tcl(context, runtime, tool=TOOL, script=action.script,
                output_root=scratch.path, timeout_seconds=action.timeout_seconds,
                inputs=(*libraries, *inputs.values(), library_list, *(Path(hdl[name]) for name in
                         ("SIGILICON_HDL_FILELIST", "SIGILICON_HDL_CONTRACT"))),
                environment={**hdl, **{key: str(path) for key, path in inputs.items()},
                    "SIGILICON_DESIGN_VARIANT": action.variant, "SIGILICON_DESIGN_CORNER": action.corner,
                    "SIGILICON_DC_PARAMETERS": ",".join(f"{key}={value}" for key, value in action.parameters),
                    "SIGILICON_DC_EXPECTED_MACROS": str(action.expected_macros),
                    "SIGILICON_DC_LINK_LIBRARIES": str(library_list),
                    "SIGILICON_DC_CONSTRAINTS": str(context.source_path(action.constraints))})
            artifacts = list(_logs(context, completed.stdout, completed.stderr or ""))
            checks = completion(completed, action.success_marker)
            checks["no_unresolved_references"] = "Unable to resolve reference" not in completed.stdout + (completed.stderr or "")
            declared = (("mapped-netlist", "netlist.verilog", "mapped.v"),
                        ("mapped-constraints", "constraints.sdc", "mapped.sdc"),
                        ("checkpoint", "checkpoint.synopsys-ddc", "mapped.ddc"),
                        *(("report", "report.synopsys", name) for name in action.reports))
            missing = []
            reports = {}
            for role, kind, name in declared:
                path = scratch.path / name
                if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
                    missing.append(name)
                    continue
                artifact = context.copy_output(role=role, kind=kind, source=path,
                                               filename=f"reports/{name}" if role == "report" else name)
                artifacts.append(artifact)
                if role == "report":
                    reports[name] = artifact.read_text()
            checks["declared_outputs"] = not missing
            area, area_checks = area_evidence(reports.get("accounting.rpt", ""), action.expected_macros)
            timing, timing_checks = timing_evidence(reports.get("qor.rpt", ""), action.timing)
            checks.update(area_checks)
            checks.update({key: timing_checks[key] for key in
                           ("timing_report_complete", "target_clock_period", *action.acceptance)})
            measurements = dict(schema=1, contract_kind="synthesis-measurements", owner=context.owner,
                variant=action.variant, corner=action.corner, area=area, timing=timing,
                checks={**area_checks, **timing_checks}, product_qualification_conclusion=False,
                plan_identity=context.plan_identity, run_id=context.run_id, step_id=context.step.id)
            path = context.write_text("measurements", "measurements.json", json.dumps(measurements, indent=2) + "\n")
            artifacts.append(Artifact("measurements", "evidence.synthesis", path))
            artifacts.append(verdict(context, stage="synthesis", variant=action.variant, corner=action.corner, checks=checks))
            return StepResult("succeeded" if all(checks.values()) else "failed", tuple(artifacts),
                              message="" if all(checks.values()) else "DC execution or declared acceptance failed")
