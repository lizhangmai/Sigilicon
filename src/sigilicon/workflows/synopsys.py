"""Managed Synopsys adapters for standard-ASIC Action seams."""

from __future__ import annotations

import csv
from hashlib import sha256
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tomllib
from typing import Any, Mapping

from sigilicon.artifacts import atomic_write_json
from sigilicon.domain.ip_release import RELEASE_MATURITY_LEVELS
from sigilicon.external_tools import run_process_group_capture, run_readonly_capture
from sigilicon.flow.adapter_result import complete_staged_run
from sigilicon.flow.model import (
    ActionContext,
    AdapterExecution,
    AdapterResult,
    CollectedActionResult,
    FlowExecutionError,
    ProducedArtifact,
)
from sigilicon.workflows.ip_packaging import audit_ip_release_manifest
from sigilicon.workflows.synopsys_reports import parse_synopsys_fc_report_facts


_ENVIRONMENT_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_ENVIRONMENT_PREFIX = re.compile(r"[A-Z][A-Z0-9_]*_\Z")
_RESERVED_PROCESS_ENVIRONMENT = frozenset(
    {
        "BASH_ENV",
        "CDPATH",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "ENV",
        "HOME",
        "IFS",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "PATH",
        "PYTHONHOME",
        "PYTHONPATH",
        "SHELL",
    }
)
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
_HSPICE_MODEL_ENVIRONMENT = {
    "nominal-model": "SIGILICON_HSPICE_NOMINAL_MODEL",
    "mismatch-model": "SIGILICON_HSPICE_MISMATCH_MODEL",
    "rvt": "SIGILICON_STDCELL_RVT_SPICE",
    "hvt": "SIGILICON_STDCELL_HVT_SPICE",
    "lvt": "SIGILICON_STDCELL_LVT_SPICE",
    "stdcell-12t-rvt": "SIGILICON_STDCELL_12T_RVT_SPICE",
}
_HSPICE_MEASUREMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_#]*\Z")
_HSPICE_TARGET = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_HSPICE_DIAGNOSTIC_ACTIONS = frozenset(
    {
        "asic.electrical-diagnostic",
        "asic.electrical-model-variant-diagnostic",
    }
)
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
        "physical-completion-report",
        "tie-off-check-report",
    }
)
_FC_DIRECTORY_OUTPUT_ROLES = frozenset({"reference-library", "checkpoint"})
_FC_PHYSICAL_TECHNOLOGY_ENVIRONMENT = {
    "technology-file": "SIGILICON_FC_TECH_FILE",
    "technology-lef": "SIGILICON_FC_TECH_LEF",
    "tluplus": "SIGILICON_FC_TLUPLUS",
    "gds-layer-map": "SIGILICON_FC_GDS_MAP",
    "antenna-rules": "SIGILICON_FC_ANTENNA_RULES",
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
    "physical-completion-report": "SIGILICON_FC_PHYSICAL_COMPLETION_REPORT",
    "tie-off-check-report": "SIGILICON_FC_TIE_OFF_CHECK_REPORT",
}
_VERILOG_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")


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
    manifest_path: Path,
    expected_kind: str,
    expected_qualifiers: Mapping[str, Any],
) -> tuple[tuple[str, Path], ...]:
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
    members: list[tuple[str, Path]] = []
    for value in members_raw:
        if not isinstance(value, dict):
            raise FlowExecutionError("source-set member must be an object")
        relative_text = value.get("path")
        file_text = value.get("file")
        if not isinstance(relative_text, str) or not isinstance(file_text, str):
            raise FlowExecutionError("source-set member identity is invalid")
        relative = Path(relative_text)
        relative_file = Path(file_text)
        if (
            not relative_text
            or relative.is_absolute()
            or "\\" in relative_text
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise FlowExecutionError(
                f"source-set member must be owner-relative: {relative_text!r}"
            )
        if (
            not file_text
            or relative_file.is_absolute()
            or "\\" in file_text
            or any(part in {"", ".", ".."} for part in relative_file.parts)
        ):
            raise FlowExecutionError("source-set snapshot path is invalid")
        manifest_root = manifest_path.parent.resolve()
        source = (manifest_root / relative_file).resolve()
        if (
            not source.is_relative_to(manifest_root)
            or not source.is_file()
        ):
            raise FlowExecutionError(f"source-set snapshot is missing: {relative_text}")
        members.append((relative.as_posix(), source))
    paths = [relative for relative, _source in members]
    if len(paths) != len(set(paths)):
        raise FlowExecutionError("source-set manifest repeats a member")
    return tuple(members)


def _stage_source_set(
    context: ActionContext,
    role: str,
    filelist_name: str,
) -> Path:
    artifact = context.input(role)
    members = _manifest_members(
        artifact.path,
        artifact.kind,
        artifact.qualifiers,
    )
    stage_root = context.work_root / "inputs" / role
    stage_root.mkdir(parents=True)
    staged: list[Path] = []
    for relative, source in members:
        destination = (stage_root / relative).resolve()
        if not destination.is_relative_to(stage_root.resolve()):
            raise FlowExecutionError(
                f"source-set member escaped staging root: {relative}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        staged.append(destination)
    filelist = context.work_root / filelist_name
    filelist.write_text(
        "".join(f"{path}\n" for path in staged),
        encoding="utf-8",
    )
    return filelist


def _pinned_owner_runner(
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
    recipe = context.input(recipe_role)
    members = _manifest_members(
        recipe.path,
        recipe.kind,
        recipe.qualifiers,
    )
    member_paths = {member: path for member, path in members}
    if value not in member_paths:
        raise FlowExecutionError(
            f"{action_label} runner is not pinned by {recipe_role}"
        )
    runner = member_paths[value]
    if not os.access(runner, os.X_OK):
        raise FlowExecutionError(f"{action_label} runner is not executable")
    return runner


class SynopsysDCAdapter:
    """Run one owner recipe behind the typed ``asic.synthesis`` interface."""

    def run(self, context: ActionContext) -> AdapterResult:
        return complete_staged_run(
            context,
            validate_inputs=self._validate_inputs,
            prepare=self._prepare,
            execute=self._execute,
            collect_result=self._collect_result,
        )

    def _validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
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

    def _prepare(self, context: ActionContext) -> None:
        (context.output_root / "tool").mkdir()

    def _execute(self, context: ActionContext) -> AdapterExecution:
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
        (context.log_root / "stdout.log").write_text(
            completed.stdout,
            encoding="utf-8",
        )
        (context.log_root / "stderr.log").write_text(
            completed.stderr or "",
            encoding="utf-8",
        )
        status = "succeeded" if completed.returncode == 0 else "failed"
        return AdapterExecution(
            status,
            completed.returncode,
            {"runner": str(context.action_config["runner"])},
        )

    def _collect_result(
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
                context.log_root / "stdout.log",
                context.log_root / "stderr.log",
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
            if not member.location.is_file():
                raise FlowExecutionError(
                    f"resolved standard-cell timing DB {role!r} is unavailable"
                )
            members[role] = member.location
        return executable, members

    def _pinned_runner(self, context: ActionContext) -> Path:
        return _pinned_owner_runner(
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


class SynopsysStructuralLinkAdapter:
    """Link composite-IP RTL against one immutable structural macro release."""

    _ACTION = "asic.structural-link"
    _CAPABILITIES = (
        "tool.synopsys-library-compiler",
        "tool.synopsys-dc",
    )

    def run(self, context: ActionContext) -> AdapterResult:
        return complete_staged_run(
            context,
            validate_inputs=self._validate_inputs,
            prepare=self._prepare,
            execute=self._execute,
            collect_result=self._collect_result,
        )

    def _validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        diagnostics: list[str] = []
        if context.action.kind != self._ACTION:
            diagnostics.append("structural-link Adapter requires asic.structural-link")
        for role in ("rtl-sources", "structural-link-recipe"):
            artifact = context.inputs.get(role)
            if artifact is None:
                diagnostics.append(f"structural-link input {role!r} is missing")
            elif not artifact.path.is_file():
                diagnostics.append(
                    f"structural-link input {role!r} is not a regular file"
                )
        try:
            recipe, recipe_members = self._recipe(context)
            integration = self._integration(context, recipe, recipe_members)
            self._source_members(context, integration)
            self._recipe_member(recipe_members, recipe["liberty_compile_script"])
            self._recipe_member(recipe_members, recipe["link_script"])
            self._variant_top(context, recipe, recipe_members)
            self._executables(context)
            self._timeout(context)
        except FlowExecutionError as exc:
            diagnostics.append(str(exc))
        return tuple(diagnostics)

    @staticmethod
    def _prepare(context: ActionContext) -> None:
        (context.output_root / "tool").mkdir()

    def _execute(self, context: ActionContext) -> AdapterExecution:
        recipe, recipe_members = self._recipe(context)
        integration = self._integration(context, recipe, recipe_members)
        self._source_members(context, integration)
        source_file = _stage_source_set(
            context,
            "rtl-sources",
            "structural-link-sources.f",
        )
        library_compiler, design_compiler = self._executables(context)
        compile_script = self._recipe_member(
            recipe_members,
            recipe["liberty_compile_script"],
        )
        link_script = self._recipe_member(
            recipe_members,
            recipe["link_script"],
        )
        top = self._variant_top(context, recipe, recipe_members)
        released, macro_liberty = self._released_macro(context, recipe, integration)
        staged_release_root = context.work_root / "inputs" / "release"
        staged_release_root.mkdir(parents=True)
        staged_macro_liberty = staged_release_root / "structural-macro.lib"
        shutil.copyfile(macro_liberty, staged_macro_liberty)
        macro_liberty_sha256 = sha256(staged_macro_liberty.read_bytes()).hexdigest()
        pinned_source_sha256 = self._pinned_release_source_sha256(
            context,
            recipe,
            released,
            macro_liberty,
        )
        if macro_liberty_sha256 != pinned_source_sha256:
            raise FlowExecutionError(
                "structural-link Liberty differs from its pinned source commit"
            )
        tool_root = context.output_root / "tool"
        macro_db = tool_root / "structural-macro.db"
        report = tool_root / "structural-link.rpt"
        checkpoint = tool_root / f"{top}.ddc"
        parameters = ",".join(
            f"{name}={value}"
            for name, value in recipe["parameter_overrides"].items()
        )
        environment = os.environ.copy()
        environment.pop("LD_PRELOAD", None)
        environment.update(
            {
                "SIGILICON_STRUCTURAL_LIBERTY": str(staged_macro_liberty),
                "SIGILICON_STRUCTURAL_DB": str(macro_db),
                "SIGILICON_STRUCTURAL_LIBRARY": recipe["library_name"],
                "SIGILICON_STRUCTURAL_MACRO_CELL": recipe["macro_cell"],
                "SIGILICON_STRUCTURAL_TOP": top,
                "SIGILICON_STRUCTURAL_SOURCES": str(source_file),
                "SIGILICON_STRUCTURAL_PARAMETERS": parameters,
                "SIGILICON_STRUCTURAL_REPORT": str(report),
                "SIGILICON_STRUCTURAL_CHECKPOINT": str(checkpoint),
            }
        )

        lc = run_process_group_capture(
            [str(library_compiler), "-f", str(compile_script)],
            cwd=context.work_root,
            env=environment,
            timeout=self._timeout(context),
        )
        (context.log_root / "library-compiler.stdout.log").write_text(
            lc.stdout,
            encoding="utf-8",
        )
        (context.log_root / "library-compiler.stderr.log").write_text(
            lc.stderr or "",
            encoding="utf-8",
        )
        lc_marker = (
            "SIGILICON_STRUCTURAL_DB_PASS "
            f"library={recipe['library_name']}"
        )
        lc_errors = tuple(
            line
            for line in (lc.stdout + "\n" + (lc.stderr or "")).splitlines()
            if line.startswith(("Error:", "Fatal:"))
        )
        if (
            lc.returncode != 0
            or lc_marker not in lc.stdout
            or lc_errors
            or not macro_db.is_file()
        ):
            return AdapterExecution(
                "failed",
                lc.returncode,
                {
                    "stage": "library-compilation",
                    "tool-error-count": len(lc_errors),
                },
            )

        dc = run_process_group_capture(
            [str(design_compiler), "-f", str(link_script)],
            cwd=context.work_root,
            env=environment,
            timeout=self._timeout(context),
        )
        (context.log_root / "design-compiler.stdout.log").write_text(
            dc.stdout,
            encoding="utf-8",
        )
        (context.log_root / "design-compiler.stderr.log").write_text(
            dc.stderr or "",
            encoding="utf-8",
        )
        expected = recipe["expected"]
        marker = (
            f"SIGILICON_STRUCTURAL_LINK_PASS top={top} "
            f"macro_instances={expected['macro_instances']} "
            f"unresolved={expected['unresolved_references']}"
        )
        tool_errors = tuple(
            line
            for line in (dc.stdout + "\n" + (dc.stderr or "")).splitlines()
            if line.startswith(("Error:", "Fatal:"))
        )
        if (
            dc.returncode != 0
            or marker not in dc.stdout
            or tool_errors
            or not report.is_file()
            or not checkpoint.is_file()
        ):
            return AdapterExecution(
                "failed",
                dc.returncode,
                {"stage": "structural-link", "tool-error-count": len(tool_errors)},
            )
        return AdapterExecution.succeeded(
            details={
                "stage": "complete",
                "variant": self._variant(context),
                "top": top,
                "release": {
                    "release-id": released["release_id"],
                    "source-commit": released["source_commit"],
                    "manifest": released["manifest"],
                    "manifest-sha256": released["manifest_sha256"],
                    "macro-liberty": released["roles"][recipe["liberty_role"]],
                    "macro-liberty-sha256": macro_liberty_sha256,
                    "pinned-source-sha256": pinned_source_sha256,
                },
            }
        )

    def _collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        recipe, recipe_members = self._recipe(context)
        top = self._variant_top(context, recipe, recipe_members)
        if execution.details.get("top") != top:
            raise FlowExecutionError("structural-link execution top identity drifted")
        release = execution.details.get("release")
        if not isinstance(release, Mapping):
            raise FlowExecutionError("structural-link execution omitted release identity")
        tool_root = context.output_root / "tool"
        macro_db = tool_root / "structural-macro.db"
        report = tool_root / "structural-link.rpt"
        checkpoint = tool_root / f"{top}.ddc"
        for label, path in (
            ("compiled macro library", macro_db),
            ("structural report", report),
            ("checkpoint", checkpoint),
        ):
            if not path.is_file():
                raise FlowExecutionError(f"structural-link omitted {label}")

        expected = recipe["expected"]
        claims = recipe["claims"]
        qualifiers = dict(context.input("rtl-sources").qualifiers)
        evidence_path = context.output_path("evidence", "structural-link.json")
        evidence = {
            "schema": 1,
            "contract_kind": "structural-link-evidence",
            "owner": context.require_project_scope().owner,
            "variant": self._variant(context),
            "top": top,
            "scope": "structural-link-only",
            "parameter_overrides": dict(recipe["parameter_overrides"]),
            "macro_cell": recipe["macro_cell"],
            "macro_instances": expected["macro_instances"],
            "unresolved_references": expected["unresolved_references"],
            "release_id": release["release-id"],
            "release_source_commit": release["source-commit"],
            "manifest": release["manifest"],
            "manifest_sha256": release["manifest-sha256"],
            "macro_liberty": release["macro-liberty"],
            "macro_liberty_sha256": release["macro-liberty-sha256"],
            "pinned_source_sha256": release["pinned-source-sha256"],
            "compiled_macro_db_sha256": sha256(macro_db.read_bytes()).hexdigest(),
            "timing_characterized": claims["timing_characterized"],
            "power_characterized": claims["power_characterized"],
            "area_characterized": claims["area_characterized"],
        }
        atomic_write_json(evidence_path, evidence)
        facts = {
            "passed": True,
            "evidence-role": "regression",
            "evidence-level": "l4",
            "evidence-scope": "native-macro-structural-link",
            "product-qualification-conclusion": False,
            "macro-instance-count": expected["macro_instances"],
            "unresolved-reference-count": expected["unresolved_references"],
            "timing-characterized": claims["timing_characterized"],
            "power-characterized": claims["power_characterized"],
            "area-characterized": claims["area_characterized"],
        }
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "compiled-macro-library",
                    context.action.output("compiled-macro-library").kind,
                    macro_db,
                    qualifiers=qualifiers,
                ),
                ProducedArtifact(
                    "checkpoint",
                    context.action.output("checkpoint").kind,
                    checkpoint,
                    qualifiers=qualifiers,
                ),
                ProducedArtifact(
                    "structural-report",
                    context.action.output("structural-report").kind,
                    report,
                    qualifiers=qualifiers,
                ),
                ProducedArtifact(
                    "evidence",
                    context.action.output("evidence").kind,
                    evidence_path,
                    qualifiers=qualifiers,
                ),
            ),
            facts=facts,
            evidence=(
                context.log_root / "library-compiler.stdout.log",
                context.log_root / "library-compiler.stderr.log",
                context.log_root / "design-compiler.stdout.log",
                context.log_root / "design-compiler.stderr.log",
                evidence_path,
            ),
            details={"release-id": release["release-id"]},
        )

    def _recipe(
        self,
        context: ActionContext,
    ) -> tuple[dict[str, Any], dict[str, Path]]:
        artifact = context.input("structural-link-recipe")
        members = dict(
            _manifest_members(
                artifact.path,
                artifact.kind,
                artifact.qualifiers,
            )
        )
        recipe_path = context.action_config.get("recipe")
        if not isinstance(recipe_path, str) or recipe_path not in members:
            raise FlowExecutionError(
                "structural-link recipe must be pinned by its typed input"
            )
        try:
            with members[recipe_path].open("rb") as stream:
                raw = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise FlowExecutionError("cannot read structural-link recipe") from exc
        scope = context.require_project_scope()
        if (
            raw.get("schema") != 1
            or raw.get("contract_kind") != "ip-structural-link-recipe"
            or raw.get("path_scope") != "owner"
            or raw.get("owner") != scope.owner
        ):
            raise FlowExecutionError("structural-link recipe header is invalid")
        required_text = (
            "component_contract",
            "dependency_lock",
            "provider_owner",
            "fileset",
            "liberty_role",
            "liberty_compile_script",
            "link_script",
            "library_name",
            "macro_cell",
        )
        for field in required_text:
            if not isinstance(raw.get(field), str) or not raw[field]:
                raise FlowExecutionError(
                    f"structural-link recipe {field!r} must be text"
                )
        for field in ("library_name", "macro_cell"):
            if _VERILOG_IDENTIFIER.fullmatch(raw[field]) is None:
                raise FlowExecutionError(
                    f"structural-link recipe {field!r} must be an identifier"
                )
        parameters = raw.get("parameter_overrides")
        if (
            not isinstance(parameters, dict)
            or not parameters
            or any(
                not isinstance(name, str)
                or _VERILOG_IDENTIFIER.fullmatch(name) is None
                or not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                for name, value in parameters.items()
            )
        ):
            raise FlowExecutionError(
                "structural-link parameter_overrides must be positive integers"
            )
        variants = raw.get("variants")
        if not isinstance(variants, dict) or any(
            not isinstance(name, str)
            or not isinstance(path, str)
            or not path
            for name, path in variants.items()
        ):
            raise FlowExecutionError("structural-link variants are invalid")
        expected = raw.get("expected")
        if not isinstance(expected, dict) or set(expected) != {
            "macro_instances",
            "unresolved_references",
        }:
            raise FlowExecutionError("structural-link expected results are invalid")
        for name, value in expected.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise FlowExecutionError(
                    f"structural-link expected {name!r} must be non-negative"
                )
        claims = raw.get("claims")
        if not isinstance(claims, dict) or claims != {
            "timing_characterized": False,
            "power_characterized": False,
            "area_characterized": False,
        }:
            raise FlowExecutionError(
                "structural-link recipe must preserve uncharacterized claims"
            )
        return raw, members

    def _integration(
        self,
        context: ActionContext,
        recipe: Mapping[str, Any],
        recipe_members: Mapping[str, Path],
    ) -> dict[str, Any]:
        scope = context.require_project_scope()
        variant_name = self._variant(context)
        component = self._toml_member(
            recipe_members,
            recipe["component_contract"],
            "component contract",
        )
        dependency_lock = self._toml_member(
            recipe_members,
            recipe["dependency_lock"],
            "dependency lock",
        )
        variant_path = recipe["variants"].get(variant_name)
        variant = self._toml_member(
            recipe_members,
            variant_path,
            "variant contract",
        )
        if (
            component.get("schema") != 1
            or component.get("contract_kind") != "ip-component"
            or component.get("path_scope") != "owner"
            or component.get("owner") != scope.owner
            or component.get("name") != scope.owner
        ):
            raise FlowExecutionError(
                "structural-link component snapshot identity is invalid"
            )
        if (
            dependency_lock.get("schema") != 1
            or dependency_lock.get("contract_kind") != "ip-dependency-lock"
            or dependency_lock.get("path_scope") != "owner"
            or dependency_lock.get("owner") != scope.owner
            or dependency_lock.get("ip") != scope.owner
        ):
            raise FlowExecutionError(
                "structural-link dependency-lock snapshot identity is invalid"
            )
        if (
            variant.get("schema") != 1
            or variant.get("contract_kind") != "ip-operating-variant"
            or variant.get("path_scope") != "variant"
            or variant.get("owner") != scope.owner
        ):
            raise FlowExecutionError(
                "structural-link variant snapshot identity is invalid"
            )

        component_project_path = self._project_member_path(
            context,
            recipe["component_contract"],
        )
        lock_project_path = self._project_member_path(
            context,
            recipe["dependency_lock"],
        )
        variant_project_path = self._project_member_path(context, variant_path)
        variants = component.get("variants")
        variant_integration = variant.get("integration")
        if (
            component.get("dependency_lock") != lock_project_path
            or not isinstance(variants, Mapping)
            or variants.get(variant_name) != variant_project_path
            or not isinstance(variant_integration, Mapping)
            or variant_integration.get("component_contract")
            != component_project_path
            or variant_integration.get("variant") != variant_name
        ):
            raise FlowExecutionError(
                "structural-link component, lock and variant snapshots disagree"
            )

        filesets = variant.get("filesets")
        fileset = (
            filesets.get(recipe["fileset"])
            if isinstance(filesets, Mapping)
            else None
        )
        if (
            not isinstance(fileset, Mapping)
            or fileset.get("required_capability") != "synthesis"
        ):
            raise FlowExecutionError(
                "structural-link variant omitted its synthesis fileset"
            )
        dependency_roles = fileset.get("dependency_roles")
        if not isinstance(dependency_roles, Mapping) or len(dependency_roles) != 1:
            raise FlowExecutionError(
                "structural-link fileset must select one macro dependency"
            )
        dependency_name, roles = next(iter(dependency_roles.items()))
        if roles != [recipe["liberty_role"]]:
            raise FlowExecutionError(
                "structural-link fileset did not select only its Liberty role"
            )

        dependencies = component.get("component")
        if not isinstance(dependencies, list):
            raise FlowExecutionError(
                "structural-link component dependencies are invalid"
            )
        selected = [
            item
            for item in dependencies
            if isinstance(item, Mapping) and item.get("name") == dependency_name
        ]
        if len(selected) != 1 or not isinstance(selected[0].get("release"), Mapping):
            raise FlowExecutionError(
                "structural-link macro dependency is not uniquely released"
            )
        provider_contract = selected[0].get("contract")
        release_contract = selected[0]["release"]
        release_roles = release_contract.get("roles")
        if (
            not isinstance(provider_contract, str)
            or not isinstance(release_roles, list)
            or any(not isinstance(role, str) or not role for role in release_roles)
            or len(release_roles) != len(set(release_roles))
            or recipe["liberty_role"] not in release_roles
            or not isinstance(release_contract.get("export"), str)
            or release_contract.get("required_maturity")
            not in RELEASE_MATURITY_LEVELS
            or not isinstance(release_contract.get("interface"), Mapping)
        ):
            raise FlowExecutionError(
                "structural-link macro release contract is invalid"
            )
        self._safe_relative(provider_contract, "provider component contract")

        locked_dependencies = dependency_lock.get("dependency")
        if not isinstance(locked_dependencies, list):
            raise FlowExecutionError(
                "structural-link dependency lock has no dependencies"
            )
        locked = [
            item
            for item in locked_dependencies
            if isinstance(item, Mapping) and item.get("name") == dependency_name
        ]
        if len(locked) != 1:
            raise FlowExecutionError(
                "structural-link dependency lock did not pin the macro release"
            )
        released = self._locked_release(
            context,
            recipe,
            dependency_name,
            provider_contract,
            release_contract,
            locked[0],
        )

        filelist_value = fileset.get("filelist")
        filelist_member = self._owner_member_from_project_path(
            context,
            recipe_members,
            filelist_value,
            "synthesis filelist",
        )
        try:
            lines = filelist_member.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise FlowExecutionError(
                "structural-link synthesis filelist is unreadable"
            ) from exc
        source_files = [line.strip() for line in lines if line.strip()]
        if not source_files or any(line.startswith("#") for line in source_files):
            raise FlowExecutionError(
                "structural-link synthesis filelist must contain only source paths"
            )
        return {
            "passed": True,
            "owner": scope.owner,
            "variant": variant_name,
            "fileset": recipe["fileset"],
            "source_files": source_files,
            "dependency_releases": [released],
        }

    def _locked_release(
        self,
        context: ActionContext,
        recipe: Mapping[str, Any],
        dependency_name: object,
        provider_contract: str,
        release_contract: Mapping[str, Any],
        locked: Mapping[str, Any],
    ) -> dict[str, Any]:
        manifest_value = locked.get("manifest")
        manifest_sha256 = locked.get("manifest_sha256")
        source_commit = locked.get("source_commit")
        release_id = locked.get("release_id")
        if (
            not isinstance(dependency_name, str)
            or not isinstance(manifest_value, str)
            or not isinstance(manifest_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None
            or not isinstance(source_commit, str)
            or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
            or not isinstance(release_id, str)
            or not release_id
        ):
            raise FlowExecutionError(
                "structural-link locked release identity is invalid"
            )
        artifact_root = context.require_project_scope().project.artifact_root.resolve()
        manifest_relative = self._safe_relative(
            manifest_value,
            "release manifest",
        )
        manifest = self._artifact_member(
            artifact_root,
            manifest_relative,
            "release manifest",
        )
        if not manifest.is_file():
            raise FlowExecutionError(
                "structural-link release manifest is unavailable"
            )
        try:
            manifest_bytes = manifest.read_bytes()
            raw = json.loads(manifest_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FlowExecutionError(
                "structural-link release manifest is unreadable"
            ) from exc
        try:
            audited = audit_ip_release_manifest(manifest)
        except (OSError, ValueError, RuntimeError) as exc:
            raise FlowExecutionError(
                "structural-link release package audit failed"
            ) from exc
        if audited != raw:
            raise FlowExecutionError(
                "structural-link release manifest changed during package audit"
            )
        maturity = raw.get("maturity") if isinstance(raw, Mapping) else None
        actual_maturity = maturity.get("level") if isinstance(maturity, Mapping) else None
        required_maturity = release_contract["required_maturity"]
        maturity_checks = maturity.get("checks") if isinstance(maturity, Mapping) else None
        if (
            sha256(manifest_bytes).hexdigest() != manifest_sha256
            or raw.get("schema") != 1
            or raw.get("contract_kind") != "ip-release-manifest"
            or raw.get("release_kind") != "source-package"
            or raw.get("owner") != dependency_name
            or raw.get("ip_name") != dependency_name
            or raw.get("release_id") != release_id
            or raw.get("source_commit") != source_commit
            or actual_maturity != locked.get("maturity")
            or actual_maturity not in RELEASE_MATURITY_LEVELS
            or RELEASE_MATURITY_LEVELS.index(actual_maturity)
            < RELEASE_MATURITY_LEVELS.index(required_maturity)
            or not isinstance(maturity_checks, list)
            or not maturity_checks
            or any(
                not isinstance(check, Mapping) or check.get("passed") is not True
                for check in maturity_checks
            )
        ):
            raise FlowExecutionError(
                "structural-link release manifest disagrees with its snapshot lock"
            )

        provider_relative = self._safe_relative(
            provider_contract,
            "provider component contract",
        )
        component_identity = raw.get("component")
        provenance = raw.get("provenance")
        source_files = raw.get("source_files")
        if (
            not isinstance(component_identity, Mapping)
            or component_identity.get("name") != dependency_name
            or component_identity.get("contract") != provider_relative.as_posix()
            or not isinstance(component_identity.get("kind"), str)
            or not isinstance(provenance, Mapping)
            or provenance.get("working_tree_dirty") is not False
            or provenance.get("producer") != recipe["provider_owner"]
            or not isinstance(provenance.get("contract"), str)
            or not isinstance(source_files, list)
            or any(not isinstance(path, str) for path in source_files)
            or len(source_files) != len(set(source_files))
        ):
            raise FlowExecutionError(
                "structural-link release provider provenance is invalid"
            )
        producer_relative = self._safe_relative(
            recipe["provider_owner"],
            "release producer",
        )
        release_contract_relative = self._safe_relative(
            provenance["contract"],
            "provider release contract",
        )
        try:
            provider_relative.relative_to(producer_relative)
            release_contract_relative.relative_to(producer_relative)
        except ValueError as exc:
            raise FlowExecutionError(
                "structural-link release provenance escaped its provider owner"
            ) from exc
        for source in source_files:
            self._safe_relative(source, "release source")
        if (
            provider_relative.as_posix() not in source_files
            or release_contract_relative.as_posix() not in source_files
        ):
            raise FlowExecutionError(
                "structural-link release omitted its provider contracts"
            )

        export_name = release_contract["export"]
        exported = self._release_export(raw, export_name)
        if (
            not self._release_interface_matches(
                exported,
                release_contract["interface"],
                producer_relative,
                source_files,
            )
            or not isinstance(exported.get("availability"), Mapping)
            or exported["availability"].get("synthesis") is not True
        ):
            raise FlowExecutionError(
                "structural-link release export disagrees with integration intent"
            )
        role_exports = release_contract.get("role_exports", {})
        role_modules = release_contract.get("role_modules", {})
        if (
            not isinstance(role_exports, Mapping)
            or not isinstance(role_modules, Mapping)
            or any(
                role not in release_contract["roles"]
                or not isinstance(value, str)
                or not value
                for role, value in role_exports.items()
            )
            or any(
                role not in release_contract["roles"]
                or not isinstance(value, str)
                or not value
                for role, value in role_modules.items()
            )
        ):
            raise FlowExecutionError(
                "structural-link release role mapping is invalid"
            )
        role_export = role_exports.get(recipe["liberty_role"], export_name)
        role_exported = self._release_export(raw, role_export)
        availability = role_exported.get("availability")
        if (
            not isinstance(availability, Mapping)
            or availability.get("synthesis") is not True
        ):
            raise FlowExecutionError(
                "structural-link Liberty role is unavailable for synthesis"
            )
        views = raw.get("views")
        selected_views = [
            item
            for item in views
            if isinstance(item, Mapping)
            and item.get("export") == role_export
            and item.get("role") == recipe["liberty_role"]
            and isinstance(item.get("capabilities"), list)
            and "synthesis" in item["capabilities"]
        ] if isinstance(views, list) else []
        if len(selected_views) != 1 or not isinstance(
            selected_views[0].get("path"), str
        ) or (
            recipe["liberty_role"] in role_modules
            and selected_views[0].get("module")
            != role_modules[recipe["liberty_role"]]
        ):
            raise FlowExecutionError(
                "structural-link release has no unique Liberty view"
            )
        view_relative = self._safe_relative(
            selected_views[0]["path"],
            "release view",
        )
        macro_liberty = self._artifact_member(
            artifact_root,
            manifest_relative.parent / view_relative,
            "release Liberty view",
        )
        if (
            not macro_liberty.is_file()
            or selected_views[0].get("size") != macro_liberty.stat().st_size
        ):
            raise FlowExecutionError(
                "structural-link release Liberty view is unavailable"
            )
        return {
            "name": dependency_name,
            "export": export_name,
            "provider_contract": provider_relative.as_posix(),
            "provider_release_contract": release_contract_relative.as_posix(),
            "release_id": release_id,
            "source_commit": source_commit,
            "manifest": manifest_relative.as_posix(),
            "manifest_sha256": manifest_sha256,
            "maturity": actual_maturity,
            "roles": {
                recipe["liberty_role"]: macro_liberty.relative_to(
                    artifact_root
                ).as_posix(),
            },
        }

    @staticmethod
    def _release_export(
        manifest: Mapping[str, Any],
        name: object,
    ) -> Mapping[str, Any]:
        exports = manifest.get("exports")
        selected = [
            item
            for item in exports
            if isinstance(item, Mapping) and item.get("name") == name
        ] if isinstance(exports, list) and isinstance(name, str) else []
        if len(selected) != 1:
            raise FlowExecutionError(
                "structural-link release export identity is invalid"
            )
        return selected[0]

    @classmethod
    def _release_interface_matches(
        cls,
        exported: Mapping[str, Any],
        expected: Mapping[str, Any],
        producer: PurePosixPath,
        source_files: list[str],
    ) -> bool:
        interface = exported.get("interface")
        if not isinstance(interface, Mapping):
            return False
        kind = expected.get("kind")
        if kind == "oa-native":
            contract = interface.get("contract")
            if (
                set(expected)
                != {"kind", "library", "cell", "schematic_view", "layout_view"}
                or set(interface) != {"kind", "contract"}
                or interface.get("kind") != kind
                or not isinstance(contract, str)
            ):
                return False
            try:
                contract_path = cls._safe_relative(
                    contract,
                    "release interface contract",
                )
                contract_path.relative_to(producer)
            except (FlowExecutionError, ValueError):
                return False
            oa = exported.get("oa")
            return (
                contract_path.as_posix() in source_files
                and isinstance(oa, Mapping)
                and all(
                    oa.get(field) == expected[field]
                    for field in ("library", "cell", "schematic_view", "layout_view")
                )
            )
        if kind == "oa-mixed-signal":
            return set(expected) == {"kind", "logical", "physical"} and all(
                interface.get(field) == expected[field]
                for field in ("logical", "physical")
            )
        if kind == "rtl":
            contract = interface.get("contract")
            if set(expected) != {"kind", "module"} or not isinstance(contract, str):
                return False
            try:
                contract_path = cls._safe_relative(
                    contract,
                    "release interface contract",
                )
                contract_path.relative_to(producer)
            except (FlowExecutionError, ValueError):
                return False
            return (
                contract_path.as_posix() in source_files
                and interface.get("kind") == kind
                and interface.get("module") == expected["module"]
            )
        return False

    @staticmethod
    def _toml_member(
        members: Mapping[str, Path],
        value: object,
        label: str,
    ) -> dict[str, Any]:
        path = SynopsysStructuralLinkAdapter._recipe_member(members, value)
        try:
            with path.open("rb") as stream:
                return tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise FlowExecutionError(
                f"structural-link {label} snapshot is unreadable"
            ) from exc

    @staticmethod
    def _safe_relative(value: str, label: str) -> PurePosixPath:
        relative = PurePosixPath(value)
        if (
            not value
            or not relative.parts
            or relative.is_absolute()
            or "\\" in value
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise FlowExecutionError(f"structural-link {label} path is unsafe")
        return relative

    @staticmethod
    def _artifact_member(
        artifact_root: Path,
        relative: PurePosixPath,
        label: str,
    ) -> Path:
        candidate = artifact_root
        for component in relative.parts:
            candidate /= component
            if candidate.is_symlink():
                raise FlowExecutionError(
                    f"structural-link {label} path contains a symlink"
                )
        if not candidate.resolve().is_relative_to(artifact_root):
            raise FlowExecutionError(
                f"structural-link {label} escaped artifact root"
            )
        return candidate

    @staticmethod
    def _project_member_path(context: ActionContext, value: object) -> str:
        if not isinstance(value, str):
            raise FlowExecutionError(
                "structural-link owner member path must be text"
            )
        owner_relative = SynopsysStructuralLinkAdapter._safe_relative(
            value,
            "owner member",
        )
        scope = context.require_project_scope()
        owner_prefix = scope.owner_root.relative_to(
            scope.project.project_root
        ).as_posix()
        return f"{owner_prefix}/{owner_relative.as_posix()}"

    @staticmethod
    def _owner_relative_project_path(
        context: ActionContext,
        value: object,
        label: str,
    ) -> str:
        if not isinstance(value, str):
            raise FlowExecutionError(f"structural-link {label} path must be text")
        project_relative = SynopsysStructuralLinkAdapter._safe_relative(value, label)
        scope = context.require_project_scope()
        owner_prefix = PurePosixPath(
            scope.owner_root.relative_to(scope.project.project_root).as_posix()
        )
        try:
            return project_relative.relative_to(owner_prefix).as_posix()
        except ValueError as exc:
            raise FlowExecutionError(
                f"structural-link {label} escaped owner root"
            ) from exc

    @staticmethod
    def _owner_member_from_project_path(
        context: ActionContext,
        members: Mapping[str, Path],
        value: object,
        label: str,
    ) -> Path:
        relative = SynopsysStructuralLinkAdapter._owner_relative_project_path(
            context,
            value,
            label,
        )
        return SynopsysStructuralLinkAdapter._recipe_member(members, relative)

    def _source_members(
        self,
        context: ActionContext,
        integration: Mapping[str, Any],
    ) -> tuple[tuple[str, Path], ...]:
        artifact = context.input("rtl-sources")
        members = _manifest_members(
            artifact.path,
            artifact.kind,
            artifact.qualifiers,
        )
        expected: list[str] = []
        for value in integration.get("source_files", ()):
            if not isinstance(value, str):
                raise FlowExecutionError(
                    "structural-link integration source identity is invalid"
                )
            expected.append(
                self._owner_relative_project_path(context, value, "RTL source")
            )
        if tuple(relative for relative, _path in members) != tuple(expected):
            raise FlowExecutionError(
                "structural-link RTL source snapshot drifted from IP integration"
            )
        return members

    def _released_macro(
        self,
        context: ActionContext,
        recipe: Mapping[str, Any],
        integration: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], Path]:
        releases = integration.get("dependency_releases")
        if not isinstance(releases, list) or len(releases) != 1:
            raise FlowExecutionError(
                "structural-link requires one immutable macro release"
            )
        released = releases[0]
        roles = released.get("roles") if isinstance(released, Mapping) else None
        role = recipe["liberty_role"]
        if not isinstance(roles, Mapping) or set(roles) != {role}:
            raise FlowExecutionError(
                "structural-link dependency did not resolve the declared Liberty role"
            )
        relative = roles[role]
        if not isinstance(relative, str):
            raise FlowExecutionError("structural-link Liberty identity is invalid")
        artifact_root = context.require_project_scope().project.artifact_root.resolve()
        macro_liberty = self._artifact_member(
            artifact_root,
            self._safe_relative(relative, "released Liberty"),
            "released Liberty",
        )
        if not macro_liberty.is_file():
            raise FlowExecutionError(
                "structural-link released Liberty artifact is unavailable"
            )
        return released, macro_liberty

    def _pinned_release_source_sha256(
        self,
        context: ActionContext,
        recipe: Mapping[str, Any],
        released: Mapping[str, Any],
        macro_liberty: Path,
    ) -> str:
        artifact_root = context.require_project_scope().project.artifact_root.resolve()
        try:
            macro_relative = PurePosixPath(
                macro_liberty.relative_to(artifact_root).as_posix()
            )
        except ValueError as exc:
            raise FlowExecutionError(
                "structural-link released Liberty escaped artifact root"
            ) from exc
        macro_liberty = self._artifact_member(
            artifact_root,
            macro_relative,
            "released Liberty",
        )
        manifest_value = released.get("manifest")
        source_commit = released.get("source_commit")
        if (
            not isinstance(manifest_value, str)
            or not isinstance(source_commit, str)
            or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
        ):
            raise FlowExecutionError(
                "structural-link release provenance is incomplete"
            )
        manifest_relative = self._safe_relative(
            manifest_value,
            "release manifest",
        )
        manifest = self._artifact_member(
            artifact_root,
            manifest_relative,
            "release manifest",
        )
        if not manifest.is_file():
            raise FlowExecutionError(
                "structural-link release manifest is unavailable"
            )
        try:
            manifest_bytes = manifest.read_bytes()
            raw = json.loads(manifest_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FlowExecutionError(
                "structural-link release manifest is unreadable"
            ) from exc
        if sha256(manifest_bytes).hexdigest() != released.get("manifest_sha256"):
            raise FlowExecutionError(
                "structural-link release manifest drifted from its dependency lock"
            )
        views = raw.get("views") if isinstance(raw, Mapping) else None
        if not isinstance(views, list):
            raise FlowExecutionError("structural-link release views are invalid")
        matching_views: list[Mapping[str, Any]] = []
        for value in views:
            if not isinstance(value, Mapping) or value.get("role") != recipe["liberty_role"]:
                continue
            view_path = value.get("path")
            if not isinstance(view_path, str):
                continue
            relative = PurePosixPath(view_path)
            if (
                relative.is_absolute()
                or "\\" in view_path
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                continue
            candidate = self._artifact_member(
                artifact_root,
                manifest_relative.parent / relative,
                "release source view",
            )
            if candidate.resolve() == macro_liberty.resolve():
                matching_views.append(value)
        if len(matching_views) != 1:
            raise FlowExecutionError(
                "structural-link release has no unique Liberty source view"
            )
        source_value = matching_views[0].get("source")
        if not isinstance(source_value, str):
            raise FlowExecutionError(
                "structural-link release Liberty omitted its source identity"
            )
        source = PurePosixPath(source_value)
        if (
            source.is_absolute()
            or "\\" in source_value
            or any(part in {"", ".", ".."} for part in source.parts)
        ):
            raise FlowExecutionError(
                "structural-link release Liberty source path is unsafe"
            )
        try:
            content = run_readonly_capture(
                ("git", "show", f"{source_commit}:{source.as_posix()}"),
                cwd=context.require_project_scope().project.project_root,
            )
        except Exception as exc:
            raise FlowExecutionError(
                "structural-link cannot read the pinned Liberty source blob"
            ) from exc
        return sha256(content).hexdigest()

    def _variant_top(
        self,
        context: ActionContext,
        recipe: Mapping[str, Any],
        members: Mapping[str, Path],
    ) -> str:
        variant = self._variant(context)
        if variant not in recipe["variants"]:
            raise FlowExecutionError(
                f"structural-link recipe does not support variant {variant!r}"
            )
        path = self._recipe_member(members, recipe["variants"][variant])
        try:
            with path.open("rb") as stream:
                raw = tomllib.load(stream)
            top = raw["filesets"][recipe["fileset"]]["top_module"]
        except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
            raise FlowExecutionError(
                "structural-link variant omitted its synthesis top"
            ) from exc
        if not isinstance(top, str) or _VERILOG_IDENTIFIER.fullmatch(top) is None:
            raise FlowExecutionError("structural-link top must be an identifier")
        return top

    @staticmethod
    def _variant(context: ActionContext) -> str:
        variant = context.action_config.get("variant")
        if not isinstance(variant, str) or not variant:
            raise FlowExecutionError("structural-link Action requires a variant")
        qualifiers = context.input("rtl-sources").qualifiers
        if qualifiers.get("variant") != variant:
            raise FlowExecutionError(
                "structural-link variant does not match RTL source qualifiers"
            )
        return variant

    @staticmethod
    def _recipe_member(members: Mapping[str, Path], value: object) -> Path:
        if not isinstance(value, str) or value not in members:
            raise FlowExecutionError(
                "structural-link recipe member is not pinned by its typed input"
            )
        return members[value]

    def _executables(self, context: ActionContext) -> tuple[Path, Path]:
        paths: list[Path] = []
        for name in self._CAPABILITIES:
            capability = context.capabilities.get(name)
            executable = None if capability is None else capability.executable
            if (
                executable is None
                or not executable.is_file()
                or not os.access(executable, os.X_OK)
            ):
                raise FlowExecutionError(
                    f"structural-link capability {name!r} is unavailable"
                )
            paths.append(executable)
        return paths[0], paths[1]

    @staticmethod
    def _timeout(context: ActionContext) -> int:
        timeout = context.adapter_config.get("timeout_seconds")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise FlowExecutionError(
                "structural-link profile requires a positive timeout_seconds"
            )
        return timeout


class SynopsysFCAdapter:
    """Run managed reference-library and place-and-route Action interfaces."""

    def run(self, context: ActionContext) -> AdapterResult:
        return complete_staged_run(
            context,
            validate_inputs=self._validate_inputs,
            prepare=self._prepare,
            execute=self._execute,
            collect_result=self._collect_result,
        )

    def _validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
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
        recipe_role = _FC_ACTIONS[context.action.kind]["recipe"]
        recipe = context.inputs.get(recipe_role)
        if recipe is not None:
            try:
                _manifest_members(
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

    def _prepare(self, context: ActionContext) -> None:
        (context.work_root / "tool").mkdir()
        (context.work_root / "inputs").mkdir()

    def _execute(self, context: ActionContext) -> AdapterExecution:
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
        (context.log_root / "stdout.log").write_text(
            completed.stdout,
            encoding="utf-8",
        )
        (context.log_root / "stderr.log").write_text(
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

    def _collect_result(
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

        stdout = context.log_root / "stdout.log"
        stderr = context.log_root / "stderr.log"
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
        facts = parse_synopsys_fc_report_facts(
            context.action.kind,
            {
                role: path
                for role, path in locations.items()
                if role.endswith("-report")
            },
        )
        return CollectedActionResult(
            artifacts=tuple(produced),
            facts=facts,
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
            context,
            _FC_ACTIONS[context.action.kind]["recipe"],
            "Fusion Compiler",
        )

    def _stage_recipe_runner(self, context: ActionContext, runner_name: str) -> Path:
        recipe_role = _FC_ACTIONS[context.action.kind]["recipe"]
        recipe = context.input(recipe_role)
        members = _manifest_members(
            recipe.path,
            recipe.kind,
            recipe.qualifiers,
        )
        stage_root = context.work_root / "inputs" / recipe_role
        staged_runner: Path | None = None
        for relative, source in members:
            destination = (stage_root / relative).resolve()
            if not destination.is_relative_to(stage_root.resolve()):
                raise FlowExecutionError(
                    f"FC recipe member escaped staging root: {relative}"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
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
        if not artifact.path.is_file():
            raise FlowExecutionError(f"FC input {role!r} is missing")
        destination = context.work_root / "inputs" / role / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(artifact.path, destination)
        return destination

    def _stage_directory_input(self, context: ActionContext, role: str) -> Path:
        artifact = context.input(role)
        root_name, members = self._directory_members(artifact)
        destination_root = context.work_root / "inputs" / role / root_name
        for relative, source in members:
            destination = (destination_root / relative).resolve()
            if not destination.is_relative_to(destination_root.resolve()):
                raise FlowExecutionError(
                    f"FC directory input escaped staging root: {relative}"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        return destination_root

    def _directory_members(
        self,
        artifact: Any,
    ) -> tuple[str, tuple[tuple[str, Path], ...]]:
        if not artifact.path.is_file():
            raise FlowExecutionError(f"FC directory artifact {artifact.role!r} is missing")
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
        members: list[tuple[str, Path]] = []
        for value in members_raw:
            if not isinstance(value, dict):
                raise FlowExecutionError("FC directory member must be an object")
            relative_text = value.get("path")
            if not isinstance(relative_text, str):
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
            ):
                raise FlowExecutionError(
                    f"FC directory member is missing or stale: {relative_text}"
                )
            members.append((relative.as_posix(), source))
        declared = [relative for relative, _source in members]
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
                members.append({"path": path.relative_to(resolved).as_posix()})
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
                    if role in {"tluplus", "gds-layer-map", "antenna-rules"}
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
            if not member.location.is_file():
                raise FlowExecutionError(
                    f"resolved Synopsys FC asset member {asset_role}.{role} is unavailable"
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

    def run(self, context: ActionContext) -> AdapterResult:
        return complete_staged_run(
            context,
            validate_inputs=self._validate_inputs,
            prepare=self._prepare,
            execute=self._execute,
            collect_result=self._collect_result,
        )

    def _validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
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

    def _prepare(self, context: ActionContext) -> None:
        (context.output_root / "tool").mkdir()

    def _execute(self, context: ActionContext) -> AdapterExecution:
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
        (context.log_root / "stdout.log").write_text(
            completed.stdout,
            encoding="utf-8",
        )
        (context.log_root / "stderr.log").write_text(
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

    def _collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        qualifiers = self._qualifiers(context)
        stdout = context.log_root / "stdout.log"
        stderr = context.log_root / "stderr.log"
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
            if not member.location.is_file():
                raise FlowExecutionError(
                    f"resolved standard-cell Verilog model {role!r} is unavailable"
                )
            models[role] = member.location
        return executable, models

class SynopsysHSpiceAdapter:
    """Run an owner-selected HSPICE regression or characterization campaign."""

    def run(self, context: ActionContext) -> AdapterResult:
        return complete_staged_run(
            context,
            validate_inputs=self._validate_inputs,
            prepare=self._prepare,
            execute=self._execute,
            collect_result=self._collect_result,
        )

    def _validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        diagnostics: list[str] = []
        if context.action.kind not in {
            "asic.electrical-functional",
            *_HSPICE_DIAGNOSTIC_ACTIONS,
            "asic.electrical-campaign",
        }:
            return (
                "Synopsys HSPICE Adapter requires an HSPICE electrical Action",
            )
        try:
            self._configuration(context)
            self._pinned_runner(context)
            self._execution_resources(context)
            self._qualifiers(context)
        except FlowExecutionError as exc:
            diagnostics.append(str(exc))
        for role in ("electrical-sources", "decks", "electrical-recipe"):
            artifact = context.inputs.get(role)
            if artifact is None:
                diagnostics.append(f"HSPICE input {role!r} is missing")
                continue
            try:
                _manifest_members(
                    artifact.path,
                    artifact.kind,
                    artifact.qualifiers,
                )
            except FlowExecutionError as exc:
                diagnostics.append(str(exc))
        return tuple(diagnostics)

    def _prepare(self, context: ActionContext) -> None:
        (context.output_root / "tool").mkdir()

    def _execute(self, context: ActionContext) -> AdapterExecution:
        configuration = self._configuration(context)
        runner = self._pinned_runner(context)
        executable, models = self._execution_resources(context)
        qualifiers = self._qualifiers(context)
        _stage_source_set(context, "electrical-sources", "electrical-sources.f")
        _stage_source_set(context, "decks", "decks.f")

        environment = os.environ.copy()
        environment["SIGILICON_SYNOPSYS_HSPICE"] = str(executable)
        environment["SIGILICON_HSPICE_OUTPUT_ROOT"] = str(
            context.output_root / "tool"
        )
        environment["SIGILICON_HSPICE_SOURCE_ROOT"] = str(
            context.work_root / "inputs" / "electrical-sources"
        )
        environment["SIGILICON_HSPICE_DECK_ROOT"] = str(
            context.work_root / "inputs" / "decks"
        )
        environment["SIGILICON_HSPICE_MODEL_SECTION"] = configuration[
            "model_section"
        ]
        environment["SIGILICON_DESIGN_VARIANT"] = str(qualifiers["variant"])
        environment["SIGILICON_DESIGN_CORNER"] = str(qualifiers["corner"])
        environment["SIGILICON_PYTHON"] = sys.executable
        environment.update(configuration.get("runner_environment", {}))
        for role, model in models.items():
            environment[_HSPICE_MODEL_ENVIRONMENT[role]] = str(model)

        completed = run_process_group_capture(
            [str(runner), configuration["target"]],
            cwd=context.work_root,
            env=environment,
            timeout=configuration["timeout_seconds"],
        )
        (context.log_root / "stdout.log").write_text(
            completed.stdout,
            encoding="utf-8",
        )
        (context.log_root / "stderr.log").write_text(
            completed.stderr or "",
            encoding="utf-8",
        )
        return AdapterExecution(
            "succeeded" if completed.returncode == 0 else "failed",
            completed.returncode,
            {
                "runner": str(context.action_config["runner"]),
                "target": configuration["target"],
            },
        )

    def _collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        configuration = self._configuration(context)
        qualifiers = self._qualifiers(context)
        if context.action.kind in _HSPICE_DIAGNOSTIC_ACTIONS:
            return self._collect_diagnostic(
                context,
                configuration,
                qualifiers,
            )
        if context.action.kind == "asic.electrical-campaign":
            return self._collect_campaign(
                context,
                execution,
                configuration,
                qualifiers,
            )
        raw_measurement = self._managed_tool_output(
            context.output_root / "tool",
            configuration["measurement_file"],
        )
        if not raw_measurement.is_file():
            raise FlowExecutionError(
                "Synopsys HSPICE omitted required measurement file: "
                f"{configuration['measurement_file']}"
            )
        rows, failure_count = self._parse_measurements(
            raw_measurement,
            configuration["required_measurements"],
        )
        check_failure_count = sum(
            1
            for row in rows
            for measurement in configuration["positive_measurements"]
            if not isinstance(row[measurement], float) or row[measurement] <= 0.0
        )
        measurements = context.output_path("measurements", "measurements.json")
        atomic_write_json(
            measurements,
            {
                "schema": 1,
                "contract_kind": "measurement-collection",
                "kind": "measurement.collection",
                "target": configuration["target"],
                "qualifiers": dict(qualifiers),
                "measurement_file": configuration["measurement_file"],
                "required_measurements": list(configuration["required_measurements"]),
                "rows": rows,
            },
        )
        stdout = context.log_root / "stdout.log"
        stderr = context.log_root / "stderr.log"
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "measurements",
                    context.action.output("measurements").kind,
                    measurements,
                    qualifiers=qualifiers,
                ),
            ),
            facts={
                "tool-execution-completed": True,
                "measurement-file-count": 1,
                "measurement-row-count": len(rows),
                "measurement-failure-count": failure_count,
                "measurement-check-failure-count": check_failure_count,
            },
            evidence=(stdout, stderr, raw_measurement),
            details={
                "target": configuration["target"],
                "measurement_file": configuration["measurement_file"],
            },
        )

    def _collect_diagnostic(
        self,
        context: ActionContext,
        configuration: Mapping[str, Any],
        qualifiers: Mapping[str, Any],
    ) -> CollectedActionResult:
        evidence = context.output_path("evidence", "diagnostic.json")
        atomic_write_json(
            evidence,
            {
                "schema": 1,
                "contract_kind": "electrical-diagnostic-evidence",
                "kind": "evidence.electrical-diagnostic",
                "target": configuration["target"],
                "qualifiers": dict(qualifiers),
                "tool_execution_completed": True,
                "evidence_role": "diagnostic",
                "product_qualification_conclusion": False,
            },
        )
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "evidence",
                    "evidence.electrical-diagnostic",
                    evidence,
                    qualifiers=qualifiers,
                ),
            ),
            facts={
                "tool-execution-completed": True,
                "evidence-role": "diagnostic",
                "product-qualification-conclusion": False,
            },
            evidence=(
                context.log_root / "stdout.log",
                context.log_root / "stderr.log",
            ),
            details={
                "target": configuration["target"],
                "product_qualification_conclusion": False,
            },
        )

    def _collect_campaign(
        self,
        context: ActionContext,
        execution: AdapterExecution,
        configuration: Mapping[str, Any],
        qualifiers: Mapping[str, Any],
    ) -> CollectedActionResult:
        raw_summary = self._managed_tool_output(
            context.output_root / "tool",
            configuration["summary_file"],
        )
        try:
            summary = json.loads(raw_summary.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FlowExecutionError(
                "Synopsys HSPICE omitted or malformed the campaign summary"
            ) from exc
        if not isinstance(summary, dict) or summary.get("schema") != 1:
            raise FlowExecutionError("HSPICE campaign summary schema must be 1")
        if summary.get("contract_kind") != configuration["summary_contract_kind"]:
            raise FlowExecutionError(
                "HSPICE campaign summary contract_kind does not match the Action"
            )
        records = summary.get(configuration["summary_records_field"])
        if not isinstance(records, list) or not records:
            raise FlowExecutionError("HSPICE campaign summary has no records")
        summary["kind"] = "report.electrical-campaign"
        summary["qualifiers"] = dict(qualifiers)
        output = context.output_path("campaign-summary", "campaign-summary.json")
        atomic_write_json(output, summary)
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "campaign-summary",
                    "report.electrical-campaign",
                    output,
                    qualifiers=qualifiers,
                ),
            ),
            facts={
                "tool-execution-completed": True,
                "campaign-record-count": len(records),
            },
            evidence=(
                context.log_root / "stdout.log",
                context.log_root / "stderr.log",
                raw_summary,
            ),
            details={
                "target": configuration["target"],
                "summary_file": configuration["summary_file"],
            },
        )

    def _configuration(self, context: ActionContext) -> dict[str, Any]:
        if context.action.kind in _HSPICE_DIAGNOSTIC_ACTIONS:
            unknown_action = set(context.action_config) - {
                "runner",
                "target",
                "model_section",
            }
            if unknown_action:
                raise FlowExecutionError(
                    "HSPICE diagnostic Action contains unknown configuration: "
                    f"{sorted(unknown_action)}"
                )
            target = context.action_config.get("target")
            if not isinstance(target, str) or _HSPICE_TARGET.fullmatch(target) is None:
                raise FlowExecutionError(
                    "HSPICE diagnostic Action requires a safe target"
                )
            if (
                context.action.kind == "asic.electrical-model-variant-diagnostic"
                and target != "core-variant-offset"
            ):
                raise FlowExecutionError(
                    "HSPICE model-variant diagnostic requires "
                    "target 'core-variant-offset'"
                )
            model_section = context.action_config.get("model_section")
            if not isinstance(model_section, str) or not model_section:
                raise FlowExecutionError(
                    "HSPICE diagnostic Action requires a model_section"
                )
            unknown_adapter = set(context.adapter_config) - {
                "timeout_seconds",
                "runner_environment_prefix",
                "runner_environment",
            }
            if unknown_adapter:
                raise FlowExecutionError(
                    "HSPICE diagnostic profile contains unknown configuration: "
                    f"{sorted(unknown_adapter)}"
                )
            prefix = context.adapter_config.get("runner_environment_prefix")
            if (
                not isinstance(prefix, str)
                or _ENVIRONMENT_PREFIX.fullmatch(prefix) is None
            ):
                raise FlowExecutionError(
                    "HSPICE diagnostic Action requires a safe "
                    "runner_environment_prefix"
                )
            return {
                "target": target,
                "model_section": model_section,
                "runner_environment": self._runner_environment(
                    context.adapter_config.get("runner_environment", {}),
                    prefix=prefix,
                ),
                "timeout_seconds": self._timeout(
                    context,
                    allowed={
                        "timeout_seconds",
                        "runner_environment_prefix",
                        "runner_environment",
                    },
                ),
            }
        if context.action.kind == "asic.electrical-campaign":
            unknown_action = set(context.action_config) - {
                "runner",
                "target",
                "model_section",
                "summary_file",
                "summary_contract_kind",
                "summary_records_field",
                "runner_environment_prefix",
                "runner_environment",
            }
            if unknown_action:
                raise FlowExecutionError(
                    "HSPICE campaign Action contains unknown configuration: "
                    f"{sorted(unknown_action)}"
                )
            target = context.action_config.get("target")
            if (
                not isinstance(target, str)
                or _HSPICE_TARGET.fullmatch(target) is None
            ):
                raise FlowExecutionError("HSPICE campaign Action requires a safe target")
            model_section = context.action_config.get("model_section")
            if not isinstance(model_section, str) or not model_section:
                raise FlowExecutionError(
                    "HSPICE campaign Action requires a model_section"
                )
            summary_file = context.action_config.get("summary_file")
            if not isinstance(summary_file, str):
                raise FlowExecutionError(
                    "HSPICE campaign Action requires a summary_file"
                )
            self._managed_tool_output(Path("."), summary_file)
            summary_contract_kind = context.action_config.get(
                "summary_contract_kind"
            )
            if (
                not isinstance(summary_contract_kind, str)
                or not summary_contract_kind
            ):
                raise FlowExecutionError(
                    "HSPICE campaign requires a summary_contract_kind"
                )
            summary_records_field = context.action_config.get(
                "summary_records_field"
            )
            if (
                not isinstance(summary_records_field, str)
                or _HSPICE_MEASUREMENT_NAME.fullmatch(summary_records_field) is None
            ):
                raise FlowExecutionError(
                    "HSPICE campaign requires a safe summary_records_field"
                )
            runner_environment_prefix = context.action_config.get(
                "runner_environment_prefix"
            )
            if (
                not isinstance(runner_environment_prefix, str)
                or _ENVIRONMENT_PREFIX.fullmatch(runner_environment_prefix) is None
            ):
                raise FlowExecutionError(
                    "HSPICE campaign requires a safe runner_environment_prefix"
                )
            runner_environment = self._runner_environment(
                context.action_config.get("runner_environment", {}),
                prefix=runner_environment_prefix,
            )
            timeout = self._timeout(context)
            return {
                "target": target,
                "model_section": model_section,
                "summary_file": summary_file,
                "summary_contract_kind": summary_contract_kind,
                "summary_records_field": summary_records_field,
                "runner_environment_prefix": runner_environment_prefix,
                "runner_environment": runner_environment,
                "timeout_seconds": timeout,
            }
        unknown_action = set(context.action_config) - {
            "runner",
            "target",
            "model_section",
            "measurement_file",
            "required_measurements",
            "positive_measurements",
        }
        if unknown_action:
            raise FlowExecutionError(
                "HSPICE Action contains unknown configuration: "
                f"{sorted(unknown_action)}"
            )
        target = context.action_config.get("target")
        if not isinstance(target, str) or _HSPICE_TARGET.fullmatch(target) is None:
            raise FlowExecutionError("HSPICE Action requires a safe target")
        model_section = context.action_config.get("model_section")
        if not isinstance(model_section, str) or not model_section:
            raise FlowExecutionError("HSPICE Action requires a model_section")
        measurement_file = context.action_config.get("measurement_file")
        if not isinstance(measurement_file, str):
            raise FlowExecutionError("HSPICE Action requires a measurement_file")
        self._managed_tool_output(Path("."), measurement_file)
        required = self._measurement_names(
            context.action_config.get("required_measurements"),
            "required_measurements",
        )
        positive = self._measurement_names(
            context.action_config.get("positive_measurements", ()),
            "positive_measurements",
            allow_empty=True,
        )
        if not set(positive) <= set(required):
            raise FlowExecutionError(
                "HSPICE positive_measurements must be required measurements"
            )
        timeout = self._timeout(context)
        return {
            "target": target,
            "model_section": model_section,
            "measurement_file": measurement_file,
            "required_measurements": required,
            "positive_measurements": positive,
            "timeout_seconds": timeout,
        }

    @staticmethod
    def _runner_environment(value: object, *, prefix: str) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise FlowExecutionError(
                "HSPICE runner_environment must be a mapping"
            )
        result: dict[str, str] = {}
        for name, raw_value in value.items():
            if not isinstance(name, str) or _ENVIRONMENT_NAME.fullmatch(name) is None:
                raise FlowExecutionError(
                    f"HSPICE runner_environment contains invalid name {name!r}"
                )
            if name.startswith("SIGILICON_"):
                raise FlowExecutionError(
                    "HSPICE runner_environment cannot override SIGILICON_* variables"
                )
            if name in _RESERVED_PROCESS_ENVIRONMENT:
                raise FlowExecutionError(
                    f"HSPICE runner_environment cannot override {name}"
                )
            if not name.startswith(prefix):
                raise FlowExecutionError(
                    "HSPICE runner_environment name must use owner prefix "
                    f"{prefix!r}: {name!r}"
                )
            if isinstance(raw_value, bool) or not isinstance(
                raw_value, (str, int, float)
            ):
                raise FlowExecutionError(
                    f"HSPICE runner_environment value for {name!r} must be scalar"
                )
            if isinstance(raw_value, float) and not math.isfinite(raw_value):
                raise FlowExecutionError(
                    f"HSPICE runner_environment value for {name!r} must be finite"
                )
            result[name] = str(raw_value)
        return result

    @staticmethod
    def _timeout(
        context: ActionContext,
        *,
        allowed: set[str] | None = None,
    ) -> int:
        allowed_keys = {"timeout_seconds"} if allowed is None else allowed
        unknown_adapter = set(context.adapter_config) - allowed_keys
        if unknown_adapter:
            raise FlowExecutionError(
                "HSPICE profile contains unknown configuration: "
                f"{sorted(unknown_adapter)}"
            )
        timeout = context.adapter_config.get("timeout_seconds")
        if not isinstance(timeout, int) or timeout <= 0:
            raise FlowExecutionError(
                "HSPICE profile requires a positive timeout_seconds"
            )
        return timeout

    @staticmethod
    def _measurement_names(
        value: object,
        label: str,
        *,
        allow_empty: bool = False,
    ) -> tuple[str, ...]:
        if (
            not isinstance(value, tuple)
            or (not value and not allow_empty)
            or any(
                not isinstance(item, str)
                or _HSPICE_MEASUREMENT_NAME.fullmatch(item) is None
                for item in value
            )
        ):
            requirement = "a list" if allow_empty else "a non-empty list"
            raise FlowExecutionError(f"HSPICE {label} must be {requirement}")
        if len(value) != len(set(value)):
            raise FlowExecutionError(f"HSPICE {label} repeats a measurement")
        return value

    def _pinned_runner(self, context: ActionContext) -> Path:
        return _pinned_owner_runner(
            context,
            "electrical-recipe",
            "electrical simulation",
        )

    def _qualifiers(self, context: ActionContext) -> Mapping[str, Any]:
        semantic = tuple(
            context.input(role)
            for role in ("electrical-sources", "decks", "electrical-recipe")
        )
        qualifiers = dict(semantic[0].qualifiers)
        if "variant" not in qualifiers or "corner" not in qualifiers:
            raise FlowExecutionError(
                "HSPICE inputs require variant and corner qualifiers"
            )
        if any(dict(artifact.qualifiers) != qualifiers for artifact in semantic[1:]):
            raise FlowExecutionError("HSPICE input qualifiers do not match")
        return qualifiers

    def _execution_resources(
        self,
        context: ActionContext,
    ) -> tuple[Path, dict[str, Path]]:
        capability = context.capabilities.get("tool.synopsys-hspice")
        if capability is None or capability.executable is None:
            raise FlowExecutionError(
                "Synopsys HSPICE capability requires a resolved executable"
            )
        executable = capability.executable
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise FlowExecutionError(
                "resolved Synopsys HSPICE executable is unavailable"
            )
        asset = context.platform_assets.get("hspice-models")
        if asset is None or asset.kind != "model.hspice-set":
            raise FlowExecutionError(
                "Synopsys HSPICE requires a resolved HSPICE model set"
            )
        models: dict[str, Path] = {}
        if context.action.kind == "asic.electrical-diagnostic":
            required_models = tuple(
                role
                for role in _HSPICE_MODEL_ENVIRONMENT
                if role != "stdcell-12t-rvt"
            )
        elif context.action.kind == "asic.electrical-model-variant-diagnostic":
            required_models = tuple(_HSPICE_MODEL_ENVIRONMENT)
        elif context.action.kind == "asic.electrical-campaign":
            required_models = tuple(
                role
                for role in _HSPICE_MODEL_ENVIRONMENT
                if role != "stdcell-12t-rvt"
            )
        else:
            required_models = tuple(
                role
                for role in _HSPICE_MODEL_ENVIRONMENT
                if role not in {"mismatch-model", "stdcell-12t-rvt"}
            )
        for role in required_models:
            member = asset.member(role)
            if member is None:
                raise FlowExecutionError(f"HSPICE model set omitted {role!r}")
            if not member.location.is_file():
                raise FlowExecutionError(
                    f"resolved HSPICE model {role!r} is unavailable"
                )
            models[role] = member.location
        return executable, models

    @staticmethod
    def _managed_tool_output(tool_root: Path, relative: str) -> Path:
        path = Path(relative)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise FlowExecutionError(
                f"unsafe HSPICE output path: {relative!r}"
            )
        output = (tool_root / path).resolve()
        if not output.is_relative_to(tool_root.resolve()):
            raise FlowExecutionError(
                f"HSPICE output escaped managed root: {relative!r}"
            )
        return output

    @staticmethod
    def _parse_measurements(
        path: Path,
        required: tuple[str, ...],
    ) -> tuple[list[dict[str, float | str]], int]:
        lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith(("$", "."))
        ]
        if len(lines) < 2:
            raise FlowExecutionError("HSPICE measurement CSV has no data rows")
        reader = csv.DictReader(lines, skipinitialspace=True)
        if reader.fieldnames is None or len(reader.fieldnames) != len(
            set(reader.fieldnames)
        ):
            raise FlowExecutionError("HSPICE measurement CSV has an invalid header")
        missing = set(required) - set(reader.fieldnames)
        if missing:
            raise FlowExecutionError(
                f"HSPICE measurement CSV omitted columns {sorted(missing)}"
            )
        rows: list[dict[str, float | str]] = []
        failures = 0
        for raw_row in reader:
            row: dict[str, float | str] = {}
            for name, raw_value in raw_row.items():
                if name is None or raw_value is None:
                    raise FlowExecutionError(
                        "HSPICE measurement CSV has an irregular row"
                    )
                value = raw_value.strip()
                if value.lower() == "failed":
                    row[name] = "failed"
                    failures += 1
                    continue
                try:
                    numeric = float(value)
                except ValueError as exc:
                    raise FlowExecutionError(
                        f"HSPICE measurement {name!r} is not numeric"
                    ) from exc
                if not math.isfinite(numeric):
                    raise FlowExecutionError(
                        f"HSPICE measurement {name!r} is not finite"
                    )
                row[name] = numeric
            rows.append(row)
        if not rows:
            raise FlowExecutionError("HSPICE measurement CSV has no data rows")
        return rows, failures


__all__ = [
    "SynopsysDCAdapter",
    "SynopsysFCAdapter",
    "SynopsysHSpiceAdapter",
    "SynopsysStructuralLinkAdapter",
    "SynopsysVCSAdapter",
]
