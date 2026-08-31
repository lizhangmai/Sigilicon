"""Shared implementation details for managed Synopsys adapters."""

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
from sigilicon.flow.evidence import FactSet, FactSource
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


def _fact_set(context: ActionContext, values: Mapping[str, Any]) -> FactSet:
    """Project one collector observation mapping into the Action schema."""

    schema = context.action.fact_schema
    if schema is None:
        raise FlowExecutionError(
            f"Action {context.node_id!r} has no fact schema"
        )
    return FactSet(
        schema,
        values,
        FactSource(context.action.kind, context.node_id),
    )


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

