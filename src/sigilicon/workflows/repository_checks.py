"""Manifest-driven repository source and integration checks."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import json
import os
from pathlib import Path
import tomllib
from typing import Any

from sigilicon.domain.config_contracts import validate_configuration_inventory, require_config_header
from sigilicon.external_tools import run_process_group_capture
from sigilicon.workflows.design_catalog import inspect_design_catalog
from sigilicon.workflows.design_targets import load_design_target_catalog
from sigilicon.workflows.oa_library import plan_oa_library_rebuild
from sigilicon.workflows.soc import plan_soc


def _relative_file(root: Path, value: object, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty relative path")
    relative = Path(value)
    resolved = (root / relative).resolve()
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not resolved.is_relative_to(root)
        or not resolved.is_file()
    ):
        raise ValueError(f"{field} must name a project-owned file")
    return resolved


def _rows(raw: Mapping[str, Any], name: str) -> tuple[Mapping[str, Any], ...]:
    value = raw.get(name, [])
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ValueError(f"repository check {name} must be an array of tables")
    return tuple(value)


def _name(row: Mapping[str, Any], field: str) -> str:
    value = row.get("name")
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field}.name must be a non-empty string")
    return value


def _run_json_command(
    command: Sequence[str],
    *,
    project_root: Path,
    runner: Callable[..., Any],
) -> dict[str, Any]:
    completed = runner(
        command,
        cwd=project_root,
        env=os.environ.copy(),
        timeout=900,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"design check failed: {detail}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("design check did not return JSON") from exc
    if not isinstance(payload, dict) or payload.get("passed") is not True:
        raise RuntimeError("design check did not report a passing result")
    return payload


def inspect_repository_designs(
    manifest_path: Path,
    *,
    project_root: Path,
    artifact_root: Path,
    runner: Callable[..., Any] = run_process_group_capture,
) -> dict[str, Any]:
    """Run all checks declared by a project-owned repository manifest."""

    root = project_root.resolve()
    manifest = (
        manifest_path.resolve()
        if manifest_path.is_absolute()
        else (root / manifest_path).resolve()
    )
    if not manifest.is_relative_to(root) or not manifest.is_file():
        raise ValueError("repository check manifest must be a project-owned file")
    try:
        with manifest.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read repository check manifest: {exc}") from exc
    require_config_header(
        raw,
        manifest,
        contract_kind="flow-repository-checks",
        path_scope="repository",
        owner="repository",
    )
    inventory_value = raw.get("configuration_inventory")
    if not isinstance(inventory_value, str) or not inventory_value:
        raise ValueError("repository check configuration_inventory must be a path")
    inventory = (root / inventory_value).resolve()
    inventory_report = validate_configuration_inventory(
        inventory, project_root=root
    )
    catalogs: dict[str, Any] = {}
    for index, row in enumerate(_rows(raw, "catalogs")):
        field = f"catalogs[{index}]"
        name = _name(row, field)
        if name in catalogs:
            raise ValueError(f"duplicate repository check name: {name}")
        path = _relative_file(root, row.get("path"), f"{field}.path")
        _, catalogs[name] = inspect_design_catalog(path, project_root=root)

    target_catalog = load_design_target_catalog(root)
    designs: dict[str, Any] = {}
    for index, row in enumerate(_rows(raw, "designs")):
        field = f"designs[{index}]"
        name = _name(row, field)
        target_name = row.get("target")
        mode = row.get("mode")
        if not isinstance(target_name, str) or not isinstance(mode, str):
            raise ValueError(f"{field} target and mode must be strings")
        if name in designs:
            raise ValueError(f"duplicate repository check name: {name}")
        target = target_catalog.get(target_name)
        designs[name] = _run_json_command(
            target.command(mode), project_root=root, runner=runner
        )

    oa_assemblies: dict[str, Any] = {}
    for index, row in enumerate(_rows(raw, "oa_assemblies")):
        field = f"oa_assemblies[{index}]"
        name = _name(row, field)
        if name in oa_assemblies:
            raise ValueError(f"duplicate repository check name: {name}")
        assembly = _relative_file(root, row.get("manifest"), f"{field}.manifest")
        oa_assemblies[name] = plan_oa_library_rebuild(
            assembly, project_root=root
        ).as_dict()

    socs: dict[str, Any] = {}
    for index, row in enumerate(_rows(raw, "socs")):
        field = f"socs[{index}]"
        name = _name(row, field)
        if name in socs:
            raise ValueError(f"duplicate repository check name: {name}")
        contract = _relative_file(root, row.get("contract"), f"{field}.contract")
        socs[name] = plan_soc(
            contract, project_root=root, artifact_root=artifact_root
        )

    if not catalogs and not designs and not oa_assemblies and not socs:
        raise ValueError("repository check manifest declares no checks")
    return {
        "passed": True,
        "manifest": manifest.relative_to(root).as_posix(),
        "catalogs": catalogs,
        "designs": designs,
        "oa_assemblies": oa_assemblies,
        "socs": socs,
        "configuration_inventory": inventory_report,
    }
