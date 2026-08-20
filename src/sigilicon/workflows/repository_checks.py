"""Canonical repository source and integration checks."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import json
import os
from pathlib import Path
from typing import Any

from sigilicon.domain.component import load_component_graph
from sigilicon.domain.config_contracts import (
    inspect_project_configurations,
    read_toml,
    require_config_header,
)
from sigilicon.domain.ip_release import load_ip_contract
from sigilicon.domain.platform import load_platform
from sigilicon.external_tools import run_process_group_capture
from sigilicon.paths import ProjectContext
from sigilicon.workflows.design_targets import load_design_target_catalog
from sigilicon.workflows.layout_targets import load_layout_target_catalog
from sigilicon.workflows.oa_library import plan_oa_library_rebuild
from sigilicon.workflows.soc import plan_soc


_HEADER_FIELDS = frozenset({"schema", "contract_kind", "path_scope", "owner"})


def _catalog(
    context: ProjectContext,
    name: str,
    contract_kind: str,
    sections: tuple[str, ...],
) -> tuple[Path, dict[str, Mapping[str, Any]]]:
    path = context.catalog(name)
    root = context.project_root
    if not path.is_file() or not path.is_relative_to(root):
        raise ValueError(f"{name} catalog must be a project-owned file")
    raw = read_toml(path)
    require_config_header(
        raw,
        path,
        contract_kind=contract_kind,
        path_scope="repository",
    )
    unknown = set(raw) - _HEADER_FIELDS - set(sections)
    if unknown:
        raise ValueError(f"{name} catalog contains unknown fields: {sorted(unknown)}")
    parsed: dict[str, Mapping[str, Any]] = {}
    for section in sections:
        value = raw.get(section, {})
        if not isinstance(value, Mapping):
            raise ValueError(f"{name} catalog {section} must be a table")
        parsed[section] = value
    return path, parsed


def _contract_entries(
    context: ProjectContext,
    catalog: str,
    rows: Mapping[str, Any],
) -> dict[str, Path]:
    root = context.project_root
    result: dict[str, Path] = {}
    for name, row in rows.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"{catalog} catalog names must be non-empty strings")
        if not isinstance(row, Mapping) or set(row) != {"contract"}:
            raise ValueError(
                f"{catalog} catalog entry {name!r} must contain only contract"
            )
        value = row.get("contract")
        if not isinstance(value, str) or not value:
            raise ValueError(f"{catalog} catalog entry {name!r} needs a contract")
        relative = Path(value)
        path = (root / relative).resolve()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not path.is_relative_to(root)
            or not path.is_file()
        ):
            raise ValueError(
                f"{catalog} catalog entry {name!r} must name a project-owned file"
            )
        result[name] = path
    return result


def _document_owner(path: Path) -> str:
    owner = read_toml(path).get("owner")
    if not isinstance(owner, str) or not owner:
        raise ValueError(f"{path}: owner must be a non-empty string")
    return owner


def _managed_ip_root(context: ProjectContext, path: Path) -> Path:
    matches = tuple(
        root for root in context.managed_ip_roots if path.is_relative_to(root)
    )
    if len(matches) != 1:
        raise ValueError(f"IP contract must belong to exactly one managed root: {path}")
    return matches[0]


def _register_owner_root(
    owner_roots: dict[str, Path],
    *,
    owner: str,
    root: Path,
) -> None:
    previous = owner_roots.get(owner)
    if previous is not None and previous != root:
        raise ValueError(f"configuration owner {owner!r} has multiple roots")
    for previous_owner, previous_root in owner_roots.items():
        if previous_root == root and previous_owner != owner:
            raise ValueError(
                f"configuration root {root} has multiple owners: "
                f"{previous_owner!r}, {owner!r}"
            )
    owner_roots[owner] = root


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
    context: ProjectContext,
    *,
    runner: Callable[..., Any] = run_process_group_capture,
) -> dict[str, Any]:
    """Validate every canonical source selected by one project context."""

    root = context.project_root
    ip_catalog_path, ip_catalog = _catalog(
        context,
        "ip",
        "ip-catalog",
        ("targets", "components"),
    )
    release_paths = _contract_entries(context, "ip.targets", ip_catalog["targets"])
    component_paths = _contract_entries(
        context,
        "ip.components",
        ip_catalog["components"],
    )

    owner_roots: dict[str, Path] = {}
    components: dict[str, Any] = {}
    for name, path in component_paths.items():
        graph = load_component_graph(path, project_root=root)
        component = graph.get(name)
        if component is None or component.path != path:
            raise ValueError(f"IP component catalog identity mismatch: {name}")
        _register_owner_root(
            owner_roots,
            owner=_document_owner(path),
            root=_managed_ip_root(context, path),
        )
        components[name] = {
            "contract": path.relative_to(root).as_posix(),
            "kind": component.kind,
            "graph": sorted(graph),
        }

    ip_releases: dict[str, Any] = {}
    oa_assemblies: dict[str, Any] = {}
    for name, path in release_paths.items():
        contract = load_ip_contract(path, project_root=root)
        if contract.name != name:
            raise ValueError(f"IP release catalog identity mismatch: {name}")
        _register_owner_root(
            owner_roots,
            owner=_document_owner(path),
            root=_managed_ip_root(context, path),
        )
        assembly = (root / contract.oa_assembly).resolve()
        ip_releases[name] = {
            "contract": path.relative_to(root).as_posix(),
            "default_qualification": contract.default_qualification,
            "exports": [item.name for item in contract.exports],
            "oa_assembly": contract.oa_assembly.as_posix(),
        }
        oa_assemblies[name] = plan_oa_library_rebuild(
            assembly,
            project_root=root,
        ).as_dict()

    soc_catalog_path, soc_catalog = _catalog(
        context,
        "soc",
        "soc-catalog",
        ("targets",),
    )
    soc_paths = _contract_entries(context, "soc.targets", soc_catalog["targets"])
    socs: dict[str, Any] = {}
    for name, path in soc_paths.items():
        plan = plan_soc(path, project_root=root, artifact_root=context.artifact_root)
        if plan.get("soc") != name:
            raise ValueError(f"SoC catalog identity mismatch: {name}")
        _register_owner_root(
            owner_roots,
            owner=_document_owner(path),
            root=path.parent,
        )
        socs[name] = plan

    platform_catalog_path, platform_catalog = _catalog(
        context,
        "platform",
        "platform-catalog",
        ("platforms",),
    )
    platforms: dict[str, Any] = {}
    for name, manifest in platform_catalog["platforms"].items():
        if not isinstance(name, str) or not isinstance(manifest, str):
            raise ValueError("platform catalog must map names to manifest paths")
        platform = load_platform(context, name)
        _register_owner_root(
            owner_roots,
            owner=platform.owner,
            root=platform.path.parent,
        )
        platforms[name] = {
            "manifest": platform.path.relative_to(root).as_posix(),
            "owner": platform.owner,
            "source_sha256": platform.source_sha256,
        }

    for flow in context.flows:
        owner_root = owner_roots.get(flow.owner)
        if owner_root is None:
            raise ValueError(f"flow owner is absent from canonical catalogs: {flow.owner}")
        for kind, path in flow.catalog_paths:
            if not path.is_relative_to(owner_root):
                raise ValueError(
                    f"flow {flow.name!r} {kind} is outside owner {flow.owner!r}"
                )

    configuration = inspect_project_configurations(
        context,
        owner_roots=owner_roots,
    )

    designs: dict[str, Any] = {}
    design_catalog_paths = context.flow_catalogs("design_targets")
    if design_catalog_paths:
        design_catalog = load_design_target_catalog(root)
        for target in design_catalog.targets:
            if "topology" not in {mode.name for mode in target.modes}:
                continue
            designs[target.name] = _run_json_command(
                target.command("topology"),
                project_root=root,
                runner=runner,
            )

    layout_targets: dict[str, Any] = {}
    layout_catalog_paths = context.flow_catalogs("layout_targets")
    if layout_catalog_paths:
        layout_catalog = load_layout_target_catalog(root)
        layout_targets = {
            target.name: {
                "owner": target.owner,
                "spec": target.spec_relative.as_posix(),
                "actions": list(target.actions),
            }
            for target in layout_catalog.targets
        }

    return {
        "passed": True,
        "project": "sigilicon.toml",
        "configuration": configuration,
        "catalogs": {
            "ip": ip_catalog_path.relative_to(root).as_posix(),
            "soc": soc_catalog_path.relative_to(root).as_posix(),
            "platform": platform_catalog_path.relative_to(root).as_posix(),
            "design_targets": [
                path.relative_to(root).as_posix()
                for _, path in design_catalog_paths
            ],
            "layout_targets": [
                path.relative_to(root).as_posix()
                for _, path in layout_catalog_paths
            ],
        },
        "components": components,
        "ip_releases": ip_releases,
        "designs": designs,
        "layout_targets": layout_targets,
        "oa_assemblies": oa_assemblies,
        "platforms": platforms,
        "socs": socs,
    }
