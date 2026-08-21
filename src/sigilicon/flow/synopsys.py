"""Managed Synopsys adapters for standard-ASIC Action seams."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Mapping

from sigilicon.artifacts import atomic_write_json
from sigilicon.external_tools import run_process_group_capture
from sigilicon.flow.model import (
    ActionContext,
    AdapterExecution,
    CollectedActionResult,
    FlowExecutionError,
    ProducedArtifact,
)


_ENVIRONMENT_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_SYNTHESIS_OUTPUT_ROLES = frozenset(
    {"mapped-netlist", "mapped-constraints", "checkpoint"}
)
_DC_RESOURCE_ENVIRONMENT = {
    "rvt": "SIGILICON_STDCELL_RVT_DB",
    "hvt": "SIGILICON_STDCELL_HVT_DB",
    "lvt": "SIGILICON_STDCELL_LVT_DB",
}
_VCS_MODEL_ENVIRONMENT = {
    "rvt": "SIGILICON_STDCELL_RVT_VERILOG",
    "hvt": "SIGILICON_STDCELL_HVT_VERILOG",
    "lvt": "SIGILICON_STDCELL_LVT_VERILOG",
}
_VCS_TARGETS = {
    "asic.rtl-simulation": frozenset({"rtl", "controller", "closed-loop"}),
    "asic.structural-elaboration": frozenset({"structural"}),
    "asic.gate-simulation": frozenset({"gate"}),
}
_FC_ACTIONS = {
    "asic.reference-library-construction": {
        "target": "library",
        "recipe": "reference-library-recipe",
        "capability": "tool.synopsys-library-manager",
    },
    "asic.physical-implementation": {
        "target": "pnr",
        "recipe": "implementation-recipe",
        "capability": "tool.synopsys-fc",
    },
}
_FC_REFERENCE_OUTPUT_ROLES = frozenset(
    {"reference-library", "library-check-report"}
)
_FC_IMPLEMENTATION_OUTPUT_ROLES = frozenset(
    {
        "routed-netlist",
        "routed-constraints",
        "layout-stream",
        "checkpoint",
        "design-check-report",
        "structural-report",
        "qor-report",
        "timing-report",
        "area-report",
        "power-report",
        "drc-report",
    }
)
_FC_DIRECTORY_OUTPUT_ROLES = frozenset({"reference-library", "checkpoint"})
_FC_PHYSICAL_TECHNOLOGY_ENVIRONMENT = {
    "technology-file": "SIGILICON_FC_TECH_FILE",
    "technology-lef": "SIGILICON_FC_TECH_LEF",
    "tluplus": "SIGILICON_FC_TLUPLUS",
    "gds-layer-map": "SIGILICON_FC_GDS_MAP",
}
_FC_STDCELL_PHYSICAL_ENVIRONMENT = {
    "rvt": "SIGILICON_STDCELL_RVT_LEF",
    "hvt": "SIGILICON_STDCELL_HVT_LEF",
    "lvt": "SIGILICON_STDCELL_LVT_LEF",
}
_FC_STDCELL_TIMING_ENVIRONMENT = {
    "rvt": "SIGILICON_STDCELL_RVT_DB",
    "hvt": "SIGILICON_STDCELL_HVT_DB",
    "lvt": "SIGILICON_STDCELL_LVT_DB",
}
_FC_OUTPUT_ENVIRONMENT = {
    "reference-library": "SIGILICON_FC_REFERENCE_NDM",
    "library-check-report": "SIGILICON_FC_LIBRARY_CHECK_REPORT",
    "routed-netlist": "SIGILICON_FC_ROUTED_NETLIST",
    "routed-constraints": "SIGILICON_FC_ROUTED_CONSTRAINTS",
    "layout-stream": "SIGILICON_FC_GDS",
    "checkpoint": "SIGILICON_FC_CHECKPOINT",
    "design-check-report": "SIGILICON_FC_DESIGN_CHECK_REPORT",
    "structural-report": "SIGILICON_FC_STRUCTURAL_REPORT",
    "qor-report": "SIGILICON_FC_QOR_REPORT",
    "timing-report": "SIGILICON_FC_TIMING_REPORT",
    "area-report": "SIGILICON_FC_AREA_REPORT",
    "power-report": "SIGILICON_FC_POWER_REPORT",
    "drc-report": "SIGILICON_FC_DRC_REPORT",
}
_VERILOG_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")


def _sha256(path: Path) -> str:
    hasher = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _text_mapping(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise FlowExecutionError(f"{label} must be a mapping")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise FlowExecutionError(f"{label} contains an invalid key")
        if not isinstance(item, str) or not item:
            raise FlowExecutionError(f"{label} value for {key!r} must be text")
        result[key] = item
    return result


def _environment_mapping(value: object, label: str) -> dict[str, str]:
    result = _text_mapping(value, label)
    for environment_name in result.values():
        if _ENVIRONMENT_NAME.fullmatch(environment_name) is None:
            raise FlowExecutionError(
                f"{label} contains invalid environment name {environment_name!r}"
            )
    return result


def _manifest_members(
    owner_root: Path,
    manifest_path: Path,
    expected_kind: str,
    expected_qualifiers: Mapping[str, Any],
) -> tuple[tuple[str, Path, str], ...]:
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FlowExecutionError(f"cannot read source-set manifest: {exc}") from exc
    if (
        not isinstance(raw, dict)
        or raw.get("schema") != 1
        or raw.get("contract_kind") != "source-set-manifest"
        or raw.get("kind") != expected_kind
        or raw.get("qualifiers") != dict(expected_qualifiers)
    ):
        raise FlowExecutionError("source-set manifest identity does not match input")
    members_raw = raw.get("members")
    if not isinstance(members_raw, list) or not members_raw:
        raise FlowExecutionError("source-set manifest has no members")
    members: list[tuple[str, Path, str]] = []
    for value in members_raw:
        if not isinstance(value, dict):
            raise FlowExecutionError("source-set member must be an object")
        relative_text = value.get("path")
        digest = value.get("digest")
        if not isinstance(relative_text, str) or not isinstance(digest, str):
            raise FlowExecutionError("source-set member identity is invalid")
        relative = Path(relative_text)
        if (
            not relative_text
            or relative.is_absolute()
            or "\\" in relative_text
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise FlowExecutionError(
                f"source-set member must be owner-relative: {relative_text!r}"
            )
        source = (owner_root / relative).resolve()
        if (
            not source.is_relative_to(owner_root)
            or not source.is_file()
            or _sha256(source) != digest
        ):
            raise FlowExecutionError(
                f"source-set member is missing or stale: {relative_text}"
            )
        members.append((relative.as_posix(), source, digest))
    paths = [relative for relative, _source, _digest in members]
    if len(paths) != len(set(paths)):
        raise FlowExecutionError("source-set manifest repeats a member")
    return tuple(members)


def _stage_source_set(
    owner_root: Path,
    context: ActionContext,
    role: str,
    filelist_name: str,
) -> Path:
    artifact = context.input(role)
    members = _manifest_members(
        owner_root,
        artifact.path,
        artifact.kind,
        artifact.qualifiers,
    )
    stage_root = context.work_root / "inputs" / role
    stage_root.mkdir(parents=True)
    staged: list[Path] = []
    for relative, source, digest in members:
        destination = (stage_root / relative).resolve()
        if not destination.is_relative_to(stage_root.resolve()):
            raise FlowExecutionError(
                f"source-set member escaped staging root: {relative}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if _sha256(destination) != digest:
            raise FlowExecutionError(
                f"source-set member changed while staging: {relative}"
            )
        staged.append(destination)
    filelist = context.work_root / filelist_name
    filelist.write_text(
        "".join(f"{path}\n" for path in staged),
        encoding="utf-8",
    )
    return filelist


def _pinned_owner_runner(
    owner_root: Path,
    context: ActionContext,
    recipe_role: str,
    action_label: str,
) -> Path:
    value = context.action_config.get("runner")
    if not isinstance(value, str) or not value:
        raise FlowExecutionError(f"{action_label} Action requires an owner runner")
    relative = Path(value)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise FlowExecutionError(f"{action_label} runner must be owner-relative")
    runner = (owner_root / relative).resolve()
    if not runner.is_relative_to(owner_root) or not runner.is_file():
        raise FlowExecutionError(
            f"{action_label} runner is missing or escaped its owner"
        )
    if not os.access(runner, os.X_OK):
        raise FlowExecutionError(f"{action_label} runner is not executable")
    recipe = context.input(recipe_role)
    members = _manifest_members(
        owner_root,
        recipe.path,
        recipe.kind,
        recipe.qualifiers,
    )
    if value not in {member for member, _path, _digest in members}:
        raise FlowExecutionError(
            f"{action_label} runner is not pinned by {recipe_role}"
        )
    return runner


class SynopsysDCAdapter:
    """Run one owner recipe behind the typed ``asic.synthesis`` interface."""

    version = "1"

    def __init__(self, owner_root: Path) -> None:
        self._owner_root = Path(owner_root).resolve()

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        diagnostics: list[str] = []
        if context.action.kind != "asic.synthesis":
            diagnostics.append("Synopsys DC Adapter requires asic.synthesis")
        try:
            self._pinned_runner(context)
            self._configuration(context)
            self._execution_resources(context)
        except FlowExecutionError as exc:
            diagnostics.append(str(exc))
        for role in ("rtl-sources", "constraints", "synthesis-recipe"):
            try:
                artifact = context.input(role)
            except FlowExecutionError as exc:
                diagnostics.append(str(exc))
                continue
            if not artifact.path.is_file():
                diagnostics.append(f"synthesis input {role!r} is not a regular file")
        if rtl := context.inputs.get("rtl-sources"):
            try:
                _manifest_members(
                    self._owner_root,
                    rtl.path,
                    rtl.kind,
                    rtl.qualifiers,
                )
            except FlowExecutionError as exc:
                diagnostics.append(str(exc))
        rtl = context.inputs.get("rtl-sources")
        constraints = context.inputs.get("constraints")
        if rtl is not None and constraints is not None:
            for dimension in sorted(set(rtl.qualifiers) & set(constraints.qualifiers)):
                if rtl.qualifiers[dimension] != constraints.qualifiers[dimension]:
                    diagnostics.append(
                        f"synthesis input qualifier {dimension!r} does not match"
                    )
        return tuple(diagnostics)

    def prepare(self, context: ActionContext) -> None:
        (context.output_root / "tool").mkdir()

    def execute(self, context: ActionContext) -> AdapterExecution:
        runner = self._pinned_runner(context)
        configuration = self._configuration(context)
        environment = os.environ.copy()
        executable, timing_members = self._execution_resources(context)
        environment["SIGILICON_SYNOPSYS_DC_SHELL"] = str(executable)
        for role, environment_name in _DC_RESOURCE_ENVIRONMENT.items():
            environment[environment_name] = str(timing_members[role])
        tool_root = context.output_root / "tool"
        environment[configuration["output_root_environment"]] = str(tool_root)
        materialized_inputs = {
            "rtl-sources": _stage_source_set(
                self._owner_root,
                context,
                "rtl-sources",
                "rtl-sources.f",
            ),
            "constraints": context.input("constraints").path,
        }
        for role, environment_name in configuration["input_environment"].items():
            environment[environment_name] = str(materialized_inputs[role])
        qualifiers = context.input("rtl-sources").qualifiers
        for dimension, environment_name in configuration[
            "qualifier_environment"
        ].items():
            if dimension not in qualifiers:
                raise FlowExecutionError(
                    f"rtl-sources omitted required qualifier {dimension!r}"
                )
            environment[environment_name] = str(qualifiers[dimension])

        completed = run_process_group_capture(
            [str(runner)],
            cwd=context.work_root,
            env=environment,
            timeout=configuration["timeout_seconds"],
        )
        (context.work_root / "stdout.log").write_text(
            completed.stdout,
            encoding="utf-8",
        )
        (context.work_root / "stderr.log").write_text(
            completed.stderr or "",
            encoding="utf-8",
        )
        status = "succeeded" if completed.returncode == 0 else "failed"
        return AdapterExecution(
            status,
            completed.returncode,
            {"runner": str(context.action_config["runner"])},
        )

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        configuration = self._configuration(context)
        tool_root = context.output_root / "tool"
        outputs = configuration["outputs"]
        produced: list[ProducedArtifact] = []
        qualifiers = context.input("rtl-sources").qualifiers
        for role in sorted(_SYNTHESIS_OUTPUT_ROLES):
            output = self._managed_tool_output(tool_root, outputs[role])
            if not output.is_file():
                raise FlowExecutionError(
                    f"Synopsys DC omitted required output {role!r}: {outputs[role]}"
                )
            produced.append(
                ProducedArtifact(
                    role,
                    context.action.output(role).kind,
                    output,
                    qualifiers=qualifiers,
                )
            )

        report_members: list[dict[str, Any]] = []
        for relative in configuration["reports"]:
            report = self._managed_tool_output(tool_root, relative)
            if not report.is_file():
                raise FlowExecutionError(
                    f"Synopsys DC omitted required report: {relative}"
                )
            report_members.append(
                {
                    "path": report.relative_to(context.output_root).as_posix(),
                    "digest": _sha256(report),
                }
            )
        report_manifest = context.output_path("reports", "reports.json")
        atomic_write_json(
            report_manifest,
            {
                "schema": 1,
                "contract_kind": "artifact-collection",
                "kind": "report.collection",
                "qualifiers": dict(qualifiers),
                "members": report_members,
            },
        )
        produced.append(
            ProducedArtifact(
                "reports",
                context.action.output("reports").kind,
                report_manifest,
                qualifiers=qualifiers,
            )
        )
        return CollectedActionResult(
            artifacts=tuple(produced),
            facts={"passed": True},
            evidence=(
                context.work_root / "stdout.log",
                context.work_root / "stderr.log",
            ),
            details={"report_count": len(report_members)},
        )

    def _execution_resources(
        self,
        context: ActionContext,
    ) -> tuple[Path, dict[str, Path]]:
        capability = context.capabilities.get("tool.synopsys-dc")
        if capability is None or capability.executable is None:
            raise FlowExecutionError(
                "Synopsys DC capability requires a resolved executable"
            )
        executable = capability.executable
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise FlowExecutionError("resolved Synopsys DC executable is unavailable")
        asset = context.platform_assets.get("standard-cell-timing")
        if asset is None or asset.kind != "library.synopsys-db-set":
            raise FlowExecutionError(
                "Synopsys DC requires a resolved standard-cell timing DB set"
            )
        members: dict[str, Path] = {}
        for role in _DC_RESOURCE_ENVIRONMENT:
            member = asset.member(role)
            if member is None:
                raise FlowExecutionError(
                    f"standard-cell timing DB set omitted {role!r}"
                )
            if not member.location.is_file() or _sha256(member.location) != member.digest:
                raise FlowExecutionError(
                    f"resolved standard-cell timing DB {role!r} is stale"
                )
            members[role] = member.location
        return executable, members

    def _pinned_runner(self, context: ActionContext) -> Path:
        return _pinned_owner_runner(
            self._owner_root,
            context,
            "synthesis-recipe",
            "synthesis",
        )

    def _configuration(self, context: ActionContext) -> dict[str, Any]:
        output_root_environment = context.adapter_config.get(
            "output_root_environment"
        )
        if (
            not isinstance(output_root_environment, str)
            or _ENVIRONMENT_NAME.fullmatch(output_root_environment) is None
        ):
            raise FlowExecutionError(
                "DC profile requires a valid output_root_environment"
            )
        timeout_seconds = context.adapter_config.get("timeout_seconds")
        if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            raise FlowExecutionError("DC profile requires a positive timeout_seconds")
        input_environment = _environment_mapping(
            context.adapter_config.get("input_environment"),
            "DC input_environment",
        )
        if set(input_environment) != {"rtl-sources", "constraints"}:
            raise FlowExecutionError(
                "DC input_environment must map rtl-sources and constraints"
            )
        qualifier_environment = _environment_mapping(
            context.adapter_config.get("qualifier_environment", {}),
            "DC qualifier_environment",
        )
        outputs = _text_mapping(
            context.adapter_config.get("outputs"),
            "DC outputs",
        )
        if set(outputs) != _SYNTHESIS_OUTPUT_ROLES:
            raise FlowExecutionError(
                "DC outputs must map mapped-netlist, mapped-constraints and checkpoint"
            )
        raw_reports = context.adapter_config.get("reports")
        if (
            not isinstance(raw_reports, tuple)
            or not raw_reports
            or any(not isinstance(item, str) or not item for item in raw_reports)
        ):
            raise FlowExecutionError("DC reports must be a non-empty list")
        reports = tuple(raw_reports)
        for relative in (*outputs.values(), *reports):
            self._validate_relative_output(relative)
        all_outputs = (*outputs.values(), *reports)
        if len(all_outputs) != len(set(all_outputs)):
            raise FlowExecutionError("DC output and report paths must be unique")
        return {
            "output_root_environment": output_root_environment,
            "timeout_seconds": timeout_seconds,
            "input_environment": input_environment,
            "qualifier_environment": qualifier_environment,
            "outputs": outputs,
            "reports": reports,
        }

    @staticmethod
    def _validate_relative_output(value: str) -> None:
        path = Path(value)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise FlowExecutionError(f"unsafe DC output path: {value!r}")

    @staticmethod
    def _managed_tool_output(tool_root: Path, relative: str) -> Path:
        output = (tool_root / relative).resolve()
        if not output.is_relative_to(tool_root.resolve()):
            raise FlowExecutionError(f"DC output escaped managed root: {relative!r}")
        return output


class SynopsysFCAdapter:
    """Run managed reference-library and place-and-route Action interfaces."""

    version = "1"

    def __init__(self, owner_root: Path) -> None:
        self._owner_root = Path(owner_root).resolve()

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        diagnostics: list[str] = []
        if context.action.kind not in _FC_ACTIONS:
            return ("Synopsys FC Adapter received an unsupported Action",)
        try:
            self._node_configuration(context)
            self._configuration(context)
            self._pinned_runner(context)
            self._execution_resources(context)
            self._qualifiers(context)
        except FlowExecutionError as exc:
            diagnostics.append(str(exc))
        for role, artifact in context.inputs.items():
            if not artifact.path.is_file():
                diagnostics.append(f"FC input {role!r} is not a regular file")
                continue
            if _sha256(artifact.path) != artifact.digest:
                diagnostics.append(f"FC input {role!r} is stale")
        recipe_role = _FC_ACTIONS[context.action.kind]["recipe"]
        recipe = context.inputs.get(recipe_role)
        if recipe is not None:
            try:
                _manifest_members(
                    self._owner_root,
                    recipe.path,
                    recipe.kind,
                    recipe.qualifiers,
                )
            except FlowExecutionError as exc:
                diagnostics.append(str(exc))
        if context.action.kind == "asic.physical-implementation":
            reference = context.inputs.get("reference-library")
            if reference is not None:
                try:
                    self._directory_members(reference)
                except FlowExecutionError as exc:
                    diagnostics.append(str(exc))
        return tuple(diagnostics)

    def prepare(self, context: ActionContext) -> None:
        (context.work_root / "tool").mkdir()
        (context.work_root / "inputs").mkdir()

    def execute(self, context: ActionContext) -> AdapterExecution:
        node = self._node_configuration(context)
        configuration = self._configuration(context)
        qualifiers = self._qualifiers(context)
        runner = self._stage_recipe_runner(context, node["runner"])
        capability_environment, executable, resource_environment = (
            self._execution_resources(context)
        )
        environment = os.environ.copy()
        environment[capability_environment] = str(executable)
        for name, path in resource_environment.items():
            environment[name] = str(path)
        environment["SIGILICON_FC_WORK_ROOT"] = str(context.work_root / "tool")
        environment["SIGILICON_DESIGN_VARIANT"] = str(qualifiers["variant"])
        environment["SIGILICON_DESIGN_CORNER"] = str(qualifiers["corner"])
        environment["SIGILICON_DESIGN_TOP"] = node["top"]

        output_locations = self._output_locations(context, configuration["outputs"])
        for role, path in output_locations.items():
            environment[_FC_OUTPUT_ENVIRONMENT[role]] = str(path)

        if context.action.kind == "asic.physical-implementation":
            environment["SIGILICON_FC_MAPPED_NETLIST"] = str(
                self._stage_regular_input(context, "mapped-netlist", "mapped.v")
            )
            environment["SIGILICON_FC_MAPPED_SDC"] = str(
                self._stage_regular_input(
                    context,
                    "mapped-constraints",
                    "mapped.sdc",
                )
            )
            environment["SIGILICON_FC_REFERENCE_NDM"] = str(
                self._stage_directory_input(context, "reference-library")
            )

        try:
            completed = run_process_group_capture(
                [str(runner), node["target"]],
                cwd=context.work_root,
                env=environment,
                timeout=configuration["timeout_seconds"],
            )
        except Exception as exc:
            if "timed out" in str(exc):
                raise FlowExecutionError(
                    "managed Synopsys FC execution timed out after "
                    f"{configuration['timeout_seconds']} seconds"
                ) from exc
            raise FlowExecutionError(
                "managed Synopsys FC process supervision failed: "
                f"{type(exc).__name__}"
            ) from exc
        (context.work_root / "stdout.log").write_text(
            completed.stdout,
            encoding="utf-8",
        )
        (context.work_root / "stderr.log").write_text(
            completed.stderr or "",
            encoding="utf-8",
        )
        return AdapterExecution(
            "succeeded" if completed.returncode == 0 else "failed",
            completed.returncode,
            {
                "runner": node["runner"],
                "target": node["target"],
            },
        )

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        node = self._node_configuration(context)
        configuration = self._configuration(context)
        qualifiers = self._qualifiers(context)
        locations = self._output_locations(context, configuration["outputs"])
        produced: list[ProducedArtifact] = []
        for role in sorted(locations):
            output = locations[role]
            if role in _FC_DIRECTORY_OUTPUT_ROLES:
                manifest = self._write_directory_manifest(
                    context,
                    role,
                    output,
                    qualifiers,
                )
                produced.append(
                    ProducedArtifact(
                        role,
                        context.action.output(role).kind,
                        manifest,
                        qualifiers=qualifiers,
                    )
                )
                continue
            if not output.is_file():
                raise FlowExecutionError(
                    f"Synopsys FC omitted required output {role!r}: "
                    f"{configuration['outputs'][role]}"
                )
            produced.append(
                ProducedArtifact(
                    role,
                    context.action.output(role).kind,
                    output,
                    qualifiers=qualifiers,
                )
            )

        stdout = context.work_root / "stdout.log"
        stderr = context.work_root / "stderr.log"
        evidence = context.output_path("execution-evidence", "execution.json")
        atomic_write_json(
            evidence,
            {
                "schema": 1,
                "contract_kind": "tool-execution-evidence",
                "kind": "evidence.tool-execution",
                "action": context.action.kind,
                "target": node["target"],
                "exit_code": execution.exit_code,
                "qualifiers": dict(qualifiers),
                "transcripts": {
                    "stdout": _sha256(stdout),
                    "stderr": _sha256(stderr),
                },
            },
        )
        produced.append(
            ProducedArtifact(
                "execution-evidence",
                context.action.output("execution-evidence").kind,
                evidence,
                qualifiers=qualifiers,
            )
        )
        return CollectedActionResult(
            artifacts=tuple(produced),
            facts={"passed": True},
            evidence=(stdout, stderr),
            details={
                "target": node["target"],
                "output_count": len(produced),
            },
        )

    def _node_configuration(self, context: ActionContext) -> dict[str, str]:
        unknown = set(context.action_config) - {"runner", "target", "top"}
        if unknown:
            raise FlowExecutionError(
                f"FC Action contains unknown configuration: {sorted(unknown)}"
            )
        expected_target = _FC_ACTIONS[context.action.kind]["target"]
        target = context.action_config.get("target")
        if target != expected_target:
            raise FlowExecutionError(
                f"FC Action {context.action.kind!r} requires target "
                f"{expected_target!r}"
            )
        runner = context.action_config.get("runner")
        if not isinstance(runner, str) or not runner:
            raise FlowExecutionError("FC Action requires an owner runner")
        top = context.action_config.get("top")
        if not isinstance(top, str) or _VERILOG_IDENTIFIER.fullmatch(top) is None:
            raise FlowExecutionError("FC Action requires a valid top module name")
        return {"runner": runner, "target": target, "top": top}

    def _configuration(self, context: ActionContext) -> dict[str, Any]:
        unknown = set(context.adapter_config) - {"timeout_seconds", "outputs"}
        if unknown:
            raise FlowExecutionError(
                f"FC profile contains unknown configuration: {sorted(unknown)}"
            )
        timeout = context.adapter_config.get("timeout_seconds")
        if not isinstance(timeout, int) or timeout <= 0:
            raise FlowExecutionError("FC profile requires a positive timeout_seconds")
        outputs = _text_mapping(context.adapter_config.get("outputs"), "FC outputs")
        expected = (
            _FC_REFERENCE_OUTPUT_ROLES
            if context.action.kind == "asic.reference-library-construction"
            else _FC_IMPLEMENTATION_OUTPUT_ROLES
        )
        if set(outputs) != expected:
            raise FlowExecutionError(
                f"FC outputs must map {sorted(expected)}"
            )
        for relative in outputs.values():
            self._validate_relative_output(relative)
        if len(outputs.values()) != len(set(outputs.values())):
            raise FlowExecutionError("FC output paths must be unique")
        return {"timeout_seconds": timeout, "outputs": outputs}

    def _pinned_runner(self, context: ActionContext) -> Path:
        node = self._node_configuration(context)
        return _pinned_owner_runner(
            self._owner_root,
            context,
            _FC_ACTIONS[context.action.kind]["recipe"],
            "Fusion Compiler",
        )

    def _stage_recipe_runner(self, context: ActionContext, runner_name: str) -> Path:
        recipe_role = _FC_ACTIONS[context.action.kind]["recipe"]
        recipe = context.input(recipe_role)
        members = _manifest_members(
            self._owner_root,
            recipe.path,
            recipe.kind,
            recipe.qualifiers,
        )
        stage_root = context.work_root / "inputs" / recipe_role
        staged_runner: Path | None = None
        for relative, source, digest in members:
            destination = (stage_root / relative).resolve()
            if not destination.is_relative_to(stage_root.resolve()):
                raise FlowExecutionError(
                    f"FC recipe member escaped staging root: {relative}"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            if _sha256(destination) != digest:
                raise FlowExecutionError(
                    f"FC recipe member changed while staging: {relative}"
                )
            if relative == runner_name:
                staged_runner = destination
        if staged_runner is None:
            raise FlowExecutionError("FC runner is not pinned by its recipe")
        if not os.access(staged_runner, os.X_OK):
            raise FlowExecutionError("staged FC runner is not executable")
        return staged_runner

    def _stage_regular_input(
        self,
        context: ActionContext,
        role: str,
        filename: str,
    ) -> Path:
        artifact = context.input(role)
        if not artifact.path.is_file() or _sha256(artifact.path) != artifact.digest:
            raise FlowExecutionError(f"FC input {role!r} is missing or stale")
        destination = context.work_root / "inputs" / role / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(artifact.path, destination)
        if _sha256(destination) != artifact.digest:
            raise FlowExecutionError(f"FC input {role!r} changed while staging")
        return destination

    def _stage_directory_input(self, context: ActionContext, role: str) -> Path:
        artifact = context.input(role)
        root_name, members = self._directory_members(artifact)
        destination_root = context.work_root / "inputs" / role / root_name
        for relative, source, digest in members:
            destination = (destination_root / relative).resolve()
            if not destination.is_relative_to(destination_root.resolve()):
                raise FlowExecutionError(
                    f"FC directory input escaped staging root: {relative}"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            if _sha256(destination) != digest:
                raise FlowExecutionError(
                    f"FC directory input changed while staging: {relative}"
                )
        return destination_root

    def _directory_members(
        self,
        artifact: Any,
    ) -> tuple[str, tuple[tuple[str, Path, str], ...]]:
        if not artifact.path.is_file() or _sha256(artifact.path) != artifact.digest:
            raise FlowExecutionError(
                f"FC directory artifact {artifact.role!r} is missing or stale"
            )
        try:
            raw = json.loads(artifact.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FlowExecutionError(f"cannot read FC directory manifest: {exc}") from exc
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != 1
            or raw.get("contract_kind") != "artifact-directory-manifest"
            or raw.get("kind") != artifact.kind
            or raw.get("qualifiers") != dict(artifact.qualifiers)
        ):
            raise FlowExecutionError("FC directory manifest identity does not match input")
        root_text = raw.get("root")
        if not isinstance(root_text, str):
            raise FlowExecutionError("FC directory manifest root is invalid")
        self._validate_relative_output(root_text)
        if len(Path(root_text).parts) != 1:
            raise FlowExecutionError("FC directory manifest root must be one name")
        source_root = (artifact.path.parent / root_text).resolve()
        if (
            not source_root.is_relative_to(artifact.path.parent.resolve())
            or not source_root.is_dir()
            or source_root.is_symlink()
        ):
            raise FlowExecutionError("FC directory manifest root is unavailable")
        members_raw = raw.get("members")
        if not isinstance(members_raw, list) or not members_raw:
            raise FlowExecutionError("FC directory manifest has no members")
        members: list[tuple[str, Path, str]] = []
        for value in members_raw:
            if not isinstance(value, dict):
                raise FlowExecutionError("FC directory member must be an object")
            relative_text = value.get("path")
            digest = value.get("digest")
            if not isinstance(relative_text, str) or not isinstance(digest, str):
                raise FlowExecutionError("FC directory member identity is invalid")
            relative = Path(relative_text)
            if (
                not relative_text
                or relative.is_absolute()
                or "\\" in relative_text
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise FlowExecutionError(
                    f"FC directory member path is unsafe: {relative_text!r}"
                )
            source = (source_root / relative).resolve()
            if (
                not source.is_relative_to(source_root)
                or not source.is_file()
                or source.is_symlink()
                or _sha256(source) != digest
            ):
                raise FlowExecutionError(
                    f"FC directory member is missing or stale: {relative_text}"
                )
            members.append((relative.as_posix(), source, digest))
        declared = [relative for relative, _source, _digest in members]
        if len(declared) != len(set(declared)):
            raise FlowExecutionError("FC directory manifest repeats a member")
        actual: list[str] = []
        for path in source_root.rglob("*"):
            if path.is_symlink():
                raise FlowExecutionError("FC directory artifact contains a symlink")
            if path.is_file():
                actual.append(path.relative_to(source_root).as_posix())
        if sorted(actual) != sorted(declared):
            raise FlowExecutionError("FC directory artifact content is stale")
        return root_text, tuple(members)

    def _write_directory_manifest(
        self,
        context: ActionContext,
        role: str,
        directory: Path,
        qualifiers: Mapping[str, Any],
    ) -> Path:
        role_root = (context.output_root / role).resolve()
        resolved = directory.resolve()
        if (
            not resolved.is_relative_to(role_root)
            or not resolved.is_dir()
            or resolved.is_symlink()
        ):
            raise FlowExecutionError(
                f"Synopsys FC omitted required directory output {role!r}"
            )
        root = resolved.relative_to(role_root).as_posix()
        if len(Path(root).parts) != 1:
            raise FlowExecutionError("FC directory output must use one managed name")
        members: list[dict[str, str]] = []
        for path in sorted(resolved.rglob("*")):
            if path.is_symlink():
                raise FlowExecutionError("FC directory output contains a symlink")
            if path.is_file():
                members.append(
                    {
                        "path": path.relative_to(resolved).as_posix(),
                        "digest": _sha256(path),
                    }
                )
        if not members:
            raise FlowExecutionError(f"Synopsys FC produced empty output {role!r}")
        manifest = context.output_path(role, f"{role}.json")
        atomic_write_json(
            manifest,
            {
                "schema": 1,
                "contract_kind": "artifact-directory-manifest",
                "kind": context.action.output(role).kind,
                "qualifiers": dict(qualifiers),
                "root": root,
                "members": members,
            },
        )
        return manifest

    def _qualifiers(self, context: ActionContext) -> Mapping[str, Any]:
        artifacts = list(context.inputs.values())
        if not artifacts:
            raise FlowExecutionError("FC Action has no semantic input")
        qualifiers = dict(artifacts[0].qualifiers)
        if "variant" not in qualifiers or "corner" not in qualifiers:
            raise FlowExecutionError("FC input omitted variant or corner qualifier")
        if any(dict(artifact.qualifiers) != qualifiers for artifact in artifacts[1:]):
            raise FlowExecutionError("FC input qualifiers do not match")
        return qualifiers

    def _execution_resources(
        self,
        context: ActionContext,
    ) -> tuple[str, Path, dict[str, Path]]:
        action = _FC_ACTIONS[context.action.kind]
        capability = context.capabilities.get(action["capability"])
        if capability is None or capability.executable is None:
            raise FlowExecutionError(
                f"Synopsys FC capability {action['capability']!r} requires "
                "a resolved executable"
            )
        executable = capability.executable
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise FlowExecutionError("resolved Synopsys FC executable is unavailable")
        resources: dict[str, Path] = {}
        if context.action.kind == "asic.reference-library-construction":
            resources.update(
                self._asset_environment(
                    context,
                    "physical-technology",
                    "platform.physical-view-set",
                    {
                        role: environment
                        for role, environment in _FC_PHYSICAL_TECHNOLOGY_ENVIRONMENT.items()
                        if role in {"technology-file", "technology-lef"}
                    },
                )
            )
            resources.update(
                self._asset_environment(
                    context,
                    "standard-cell-physical",
                    "library.lef-set",
                    _FC_STDCELL_PHYSICAL_ENVIRONMENT,
                )
            )
            resources.update(
                self._asset_environment(
                    context,
                    "standard-cell-timing",
                    "library.synopsys-db-set",
                    _FC_STDCELL_TIMING_ENVIRONMENT,
                )
            )
            return "SIGILICON_SYNOPSYS_LM_SHELL", executable, resources
        resources.update(
            self._asset_environment(
                context,
                "physical-technology",
                "platform.physical-view-set",
                {
                    role: environment
                    for role, environment in _FC_PHYSICAL_TECHNOLOGY_ENVIRONMENT.items()
                    if role in {"tluplus", "gds-layer-map"}
                },
            )
        )
        return "SIGILICON_SYNOPSYS_FC_SHELL", executable, resources

    @staticmethod
    def _asset_environment(
        context: ActionContext,
        asset_role: str,
        expected_kind: str,
        member_environment: Mapping[str, str],
    ) -> dict[str, Path]:
        asset = context.platform_assets.get(asset_role)
        if asset is None or asset.kind != expected_kind:
            raise FlowExecutionError(
                f"Synopsys FC requires {asset_role!r} kind {expected_kind!r}"
            )
        result: dict[str, Path] = {}
        for role, environment_name in member_environment.items():
            member = asset.member(role)
            if member is None:
                raise FlowExecutionError(
                    f"Synopsys FC asset {asset_role!r} omitted {role!r}"
                )
            if not member.location.is_file() or _sha256(member.location) != member.digest:
                raise FlowExecutionError(
                    f"resolved Synopsys FC asset member {asset_role}.{role} is stale"
                )
            result[environment_name] = member.location
        return result

    @staticmethod
    def _output_locations(
        context: ActionContext,
        outputs: Mapping[str, str],
    ) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for role, relative in outputs.items():
            role_root = (context.output_root / role).resolve()
            role_root.mkdir(parents=True, exist_ok=True)
            output = (role_root / relative).resolve()
            if not output.is_relative_to(role_root):
                raise FlowExecutionError(f"FC output escaped managed root: {relative!r}")
            output.parent.mkdir(parents=True, exist_ok=True)
            result[role] = output
        return result

    @staticmethod
    def _validate_relative_output(value: str) -> None:
        path = Path(value)
        if (
            path.is_absolute()
            or not path.parts
            or "\\" in value
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise FlowExecutionError(f"unsafe FC output path: {value!r}")


class SynopsysVCSAdapter:
    """Run owner VCS recipes behind typed simulation Action interfaces."""

    version = "1"

    def __init__(self, owner_root: Path) -> None:
        self._owner_root = Path(owner_root).resolve()

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        diagnostics: list[str] = []
        if context.action.kind not in _VCS_TARGETS:
            diagnostics.append("Synopsys VCS Adapter received an unsupported Action")
            return tuple(diagnostics)
        try:
            self._target(context)
            self._configuration(context)
            self._pinned_runner(context)
            self._execution_resources(context)
            self._qualifiers(context)
        except FlowExecutionError as exc:
            diagnostics.append(str(exc))
        for role in self._source_set_roles(context):
            artifact = context.inputs.get(role)
            if artifact is None:
                diagnostics.append(f"VCS input {role!r} is missing")
                continue
            try:
                _manifest_members(
                    self._owner_root,
                    artifact.path,
                    artifact.kind,
                    artifact.qualifiers,
                )
            except FlowExecutionError as exc:
                diagnostics.append(str(exc))
        if context.action.kind == "asic.gate-simulation":
            mapped = context.inputs.get("mapped-netlist")
            if mapped is None or not mapped.path.is_file():
                diagnostics.append("VCS mapped-netlist input is unavailable")
        return tuple(diagnostics)

    def prepare(self, context: ActionContext) -> None:
        (context.output_root / "tool").mkdir()

    def execute(self, context: ActionContext) -> AdapterExecution:
        target = self._target(context)
        runner = self._pinned_runner(context)
        configuration = self._configuration(context)
        executable, models = self._execution_resources(context)
        qualifiers = self._qualifiers(context)
        environment = os.environ.copy()
        environment["SIGILICON_SYNOPSYS_VCS"] = str(executable)
        environment["SIGILICON_VCS_OUTPUT_ROOT"] = str(
            context.output_root / "tool"
        )
        environment["SIGILICON_DESIGN_VARIANT"] = str(qualifiers["variant"])
        for role in self._source_set_roles(context):
            environment_name = {
                "rtl-sources": "SIGILICON_VCS_RTL_FILELIST",
                "testbench": "SIGILICON_VCS_TESTBENCH_FILELIST",
            }.get(role)
            if environment_name is not None:
                environment[environment_name] = str(
                    _stage_source_set(
                        self._owner_root,
                        context,
                        role,
                        f"{role}.f",
                    )
                )
        if context.action.kind == "asic.gate-simulation":
            environment["SIGILICON_VCS_MAPPED_NETLIST"] = str(
                context.input("mapped-netlist").path
            )
        for role, environment_name in _VCS_MODEL_ENVIRONMENT.items():
            if role in models:
                environment[environment_name] = str(models[role])

        completed = run_process_group_capture(
            [str(runner), target],
            cwd=context.work_root,
            env=environment,
            timeout=configuration["timeout_seconds"],
        )
        (context.work_root / "stdout.log").write_text(
            completed.stdout,
            encoding="utf-8",
        )
        (context.work_root / "stderr.log").write_text(
            completed.stderr or "",
            encoding="utf-8",
        )
        return AdapterExecution(
            "succeeded" if completed.returncode == 0 else "failed",
            completed.returncode,
            {
                "runner": str(context.action_config["runner"]),
                "target": target,
            },
        )

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        qualifiers = self._qualifiers(context)
        stdout = context.work_root / "stdout.log"
        stderr = context.work_root / "stderr.log"
        evidence = context.output_path("evidence", "simulation.json")
        atomic_write_json(
            evidence,
            {
                "schema": 1,
                "contract_kind": "simulation-evidence",
                "kind": "evidence.simulation",
                "action": context.action.kind,
                "target": self._target(context),
                "qualifiers": dict(qualifiers),
                "transcripts": {
                    "stdout": _sha256(stdout),
                    "stderr": _sha256(stderr),
                },
            },
        )
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "evidence",
                    context.action.output("evidence").kind,
                    evidence,
                    qualifiers=qualifiers,
                ),
            ),
            facts={"passed": True},
            evidence=(stdout, stderr),
            details={"target": self._target(context)},
        )

    def _target(self, context: ActionContext) -> str:
        value = context.action_config.get("target")
        if not isinstance(value, str) or value not in _VCS_TARGETS[context.action.kind]:
            raise FlowExecutionError(
                f"unsupported VCS target {value!r} for {context.action.kind}"
            )
        return value

    def _configuration(self, context: ActionContext) -> dict[str, int]:
        unknown = set(context.adapter_config) - {"timeout_seconds"}
        if unknown:
            raise FlowExecutionError(
                f"VCS profile contains unknown configuration: {sorted(unknown)}"
            )
        timeout = context.adapter_config.get("timeout_seconds")
        if not isinstance(timeout, int) or timeout <= 0:
            raise FlowExecutionError("VCS profile requires a positive timeout_seconds")
        return {"timeout_seconds": timeout}

    def _pinned_runner(self, context: ActionContext) -> Path:
        return _pinned_owner_runner(
            self._owner_root,
            context,
            "simulation-recipe",
            "simulation",
        )

    def _source_set_roles(self, context: ActionContext) -> tuple[str, ...]:
        if context.action.kind == "asic.rtl-simulation":
            return ("rtl-sources", "testbench")
        if context.action.kind == "asic.structural-elaboration":
            return ("rtl-sources",)
        return ("testbench",)

    def _qualifiers(self, context: ActionContext) -> Mapping[str, Any]:
        semantic_inputs = [
            artifact
            for role, artifact in context.inputs.items()
            if role != "simulation-recipe"
        ]
        if not semantic_inputs:
            raise FlowExecutionError("VCS Action has no semantic design input")
        qualifiers = dict(semantic_inputs[0].qualifiers)
        if "variant" not in qualifiers:
            raise FlowExecutionError("VCS design input omitted the variant qualifier")
        if any(dict(artifact.qualifiers) != qualifiers for artifact in semantic_inputs[1:]):
            raise FlowExecutionError("VCS design input qualifiers do not match")
        recipe = context.input("simulation-recipe")
        if dict(recipe.qualifiers) != qualifiers:
            raise FlowExecutionError("VCS simulation recipe qualifiers do not match")
        return qualifiers

    def _execution_resources(
        self,
        context: ActionContext,
    ) -> tuple[Path, dict[str, Path]]:
        capability = context.capabilities.get("tool.synopsys-vcs")
        if capability is None or capability.executable is None:
            raise FlowExecutionError(
                "Synopsys VCS capability requires a resolved executable"
            )
        executable = capability.executable
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise FlowExecutionError("resolved Synopsys VCS executable is unavailable")
        if context.action.kind == "asic.rtl-simulation":
            return executable, {}
        asset = context.platform_assets.get("standard-cell-models")
        if asset is None or asset.kind != "library.verilog-model-set":
            raise FlowExecutionError(
                "Synopsys VCS requires a resolved standard-cell Verilog model set"
            )
        models: dict[str, Path] = {}
        for role in _VCS_MODEL_ENVIRONMENT:
            member = asset.member(role)
            if member is None:
                raise FlowExecutionError(
                    f"standard-cell Verilog model set omitted {role!r}"
                )
            if not member.location.is_file() or _sha256(member.location) != member.digest:
                raise FlowExecutionError(
                    f"resolved standard-cell Verilog model {role!r} is stale"
                )
            models[role] = member.location
        return executable, models

__all__ = ["SynopsysDCAdapter", "SynopsysFCAdapter", "SynopsysVCSAdapter"]
