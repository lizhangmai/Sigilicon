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

__all__ = ["SynopsysDCAdapter", "SynopsysVCSAdapter"]
