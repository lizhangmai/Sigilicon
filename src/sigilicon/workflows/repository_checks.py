"""Canonical repository source and integration checks."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sigilicon.domain.component import load_component_graph
from sigilicon.domain.config_contracts import (
    inspect_project_configurations,
)
from sigilicon.domain.ip_release import load_ip_contract
from sigilicon.domain.platform import load_platform, load_platform_catalog
from sigilicon.domain.repository import Project
from sigilicon.workflows.design_targets import load_design_target_catalog
from sigilicon.workflows.layout_targets import load_layout_target_catalog
from sigilicon.workflows.oa_library import plan_oa_library_rebuild
from sigilicon.workflows.ip_integration import plan_ip_integration


def _contract_entries(
    context: Project,
    catalog: str,
    rows: Mapping[str, Any],
    *,
    owner_roots: bool = False,
) -> dict[str, Path]:
    root = context.project_root
    result: dict[str, Path] = {}
    for name, row in rows.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"{catalog} catalog names must be non-empty strings")
        expected = {"contract", "root"} if owner_roots else {"contract"}
        if not isinstance(row, Mapping) or set(row) != expected:
            raise ValueError(
                f"{catalog} catalog entry {name!r} must contain {sorted(expected)}"
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


def _component_owner_root(context: Project, path: Path) -> Path:
    return context.require_owner(path).root


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


def check_project_designs(
    project_contract: Path,
) -> dict[str, Any]:
    """Inspect one explicit project while parsing its manifest exactly once."""

    project = Project.from_file(project_contract)
    return inspect_repository_designs(project)


def inspect_repository_designs(
    project: Project | Path,
) -> dict[str, Any]:
    """Validate every canonical source selected by one project context."""

    context = (
        project
        if isinstance(project, Project)
        else Project.from_project_root(project)
    )
    root = context.project_root
    ip_catalog = context.ip_catalog_snapshot()
    ip_catalog_path = ip_catalog.path
    release_rows = ip_catalog.document.get("targets", {})
    component_rows = ip_catalog.document.get("components", {})
    if not isinstance(release_rows, Mapping):
        raise ValueError("ip catalog targets must be a table")
    if not isinstance(component_rows, Mapping):
        raise ValueError("ip catalog components must be a table")
    release_paths = _contract_entries(context, "ip.targets", release_rows)
    component_paths = _contract_entries(
        context,
        "ip.components",
        component_rows,
        owner_roots=True,
    )

    owner_roots: dict[str, Path] = {}
    components: dict[str, Any] = {}
    for name, path in component_paths.items():
        owner = context.require_owner(path)
        graph = load_component_graph(
            path,
            project_root=root,
            root_contract=(owner.component if owner.component.path == path else None),
        )
        component = graph.get(name)
        if component is None or component.path != path:
            raise ValueError(f"IP component catalog identity mismatch: {name}")
        _register_owner_root(
            owner_roots,
            owner=component.owner,
            root=_component_owner_root(context, path),
        )
        component_result: dict[str, Any] = {
            "contract": path.relative_to(root).as_posix(),
            "kind": component.kind,
            "graph": sorted(graph),
        }
        if "variants" in component.document:
            integration = plan_ip_integration(
                path,
                project=context,
            )
            if integration.get("ip") != name:
                raise ValueError(f"IP integration catalog identity mismatch: {name}")
            component_result["integration"] = integration
        components[name] = component_result

    ip_releases: dict[str, Any] = {}
    oa_assemblies: dict[str, Any] = {}
    for name, path in release_paths.items():
        contract = load_ip_contract(path, project=context)
        if contract.name != name:
            raise ValueError(f"IP release catalog identity mismatch: {name}")
        _register_owner_root(
            owner_roots,
            owner=contract.owner,
            root=_component_owner_root(context, path),
        )
        assembly = (root / contract.oa_assembly).resolve()
        ip_releases[name] = {
            "contract": path.relative_to(root).as_posix(),
            "default_maturity": contract.default_maturity,
            "exports": [item.name for item in contract.exports],
            "oa_assembly": contract.oa_assembly.as_posix(),
        }
        oa_assemblies[name] = plan_oa_library_rebuild(
            assembly,
            project=context,
        ).as_dict()

    platform_catalog = load_platform_catalog(context)
    platform_catalog_path = platform_catalog.path
    platforms: dict[str, Any] = {}
    for name in platform_catalog.manifests:
        platform = load_platform(context, name, catalog=platform_catalog)
        _register_owner_root(
            owner_roots,
            owner=platform.owner,
            root=platform.path.parent,
        )
        platforms[name] = {
            "manifest": platform.path.relative_to(root).as_posix(),
            "owner": platform.owner,
        }

    flow_catalog_inventory = context.flow_catalog_inventory()
    configuration = inspect_project_configurations(
        context,
        owner_roots=owner_roots,
        catalog_inventory=flow_catalog_inventory,
        platform_catalog=platform_catalog,
    )

    design_catalog = load_design_target_catalog(
        project=context,
        catalog_inventory=flow_catalog_inventory,
    )
    designs = {
        target.name: {
            "owner": target.owner,
            "entrypoint": target.entrypoint,
            "modes": [mode.name for mode in target.modes],
        }
        for target in design_catalog.targets
    }

    layout_catalog = load_layout_target_catalog(
        project=context,
        catalog_inventory=flow_catalog_inventory,
    )
    layout_targets = {
        target.name: {
            "owner": target.owner,
            "spec": target.spec_relative.as_posix(),
            "actions": list(target.actions),
        }
        for target in layout_catalog.targets
    }

    catalogs = {
        "ip": ip_catalog_path.relative_to(root).as_posix(),
        "platform": platform_catalog_path.relative_to(root).as_posix(),
        "design_targets": [
            path.relative_to(root).as_posix()
            for path in design_catalog.paths
        ],
        "layout_targets": [
            path.relative_to(root).as_posix()
            for path in layout_catalog.paths
        ],
    }
    return {
        "passed": True,
        "project": "sigilicon.toml",
        "configuration": configuration,
        "catalogs": catalogs,
        "components": components,
        "ip_releases": ip_releases,
        "designs": designs,
        "layout_targets": layout_targets,
        "oa_assemblies": oa_assemblies,
        "platforms": platforms,
    }
