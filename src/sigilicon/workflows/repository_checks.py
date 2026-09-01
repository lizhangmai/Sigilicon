"""Canonical repository source and integration checks."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from sigilicon.domain.component import load_component_graph
from sigilicon.domain.config_contracts import (
    RepositorySourceInventory,
    freeze_toml_document,
    inspect_project_configuration_sources,
    read_toml,
    require_config_header,
)
from sigilicon.execution.operations import compile_operation
from sigilicon.domain.ip_integration import load_ip_integration_contract
from sigilicon.domain.ip_release import load_ip_contract
from sigilicon.domain.oa_library import load_oa_library_source
from sigilicon.domain.platform import load_platform_inventory
from sigilicon.domain.repository import Project
from sigilicon.layout.spec import resolve_layout_spec
from sigilicon.workflows.oa_library import plan_oa_library_rebuild
from sigilicon.workflows.ip_integration import plan_ip_integration_contract


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


def _architecture_source_documents(
    context: Project,
) -> Mapping[Path, Mapping[str, Any]]:
    """Read owner-selected architecture TOML once for this repository check."""

    root = context.project_root
    documents: dict[Path, Mapping[str, Any]] = {}
    declared_by: dict[Path, str] = {}
    for component in context.component_inventory.values():
        owner = context.require_owner(component.path)
        if component.owner != owner.name:
            raise ValueError("architecture component owner identity drift")
        for relative in component.filesets.get("architecture", ()):
            path = (root / relative).resolve()
            if path.suffix != ".toml":
                continue
            if not path.is_relative_to(owner.root) or not path.is_file():
                raise ValueError(f"architecture fileset source is missing: {path}")
            previous_owner = declared_by.get(path)
            if previous_owner is not None:
                if previous_owner != owner.name:
                    raise ValueError(
                        f"architecture source has multiple owners: {path}"
                    )
                continue
            declared_by[path] = owner.name
            document = freeze_toml_document(read_toml(path))
            envelope_fields = {"contract_kind", "path_scope", "owner"}
            present = envelope_fields & document.keys()
            if present:
                if present != envelope_fields:
                    raise ValueError(
                        f"architecture source has an incomplete header: {path}"
                    )
                contract_kind = document.get("contract_kind")
                if not isinstance(contract_kind, str) or not contract_kind:
                    raise ValueError(
                        f"architecture source contract kind is invalid: {path}"
                    )
                require_config_header(
                    document,
                    path,
                    contract_kind=contract_kind,
                    path_scope=("owner", "cell", "verification", "variant"),
                    owner=owner.name,
                )
            documents[path] = document
    return MappingProxyType(documents)


def _integration_variant_inventory(
    context: Project,
    component_path: Path,
    architecture_source_documents: Mapping[Path, Mapping[str, Any]],
) -> Mapping[Path, Mapping[str, Any]] | None:
    """Select a complete preloaded variant set, or preserve standalone loading."""

    component = context.require_owner(component_path).component
    variants = component.document.get("variants")
    if not isinstance(variants, Mapping) or not variants:
        return None
    paths: list[Path] = []
    for value in variants.values():
        if not isinstance(value, str):
            return None
        paths.append((context.project_root / Path(value)).resolve())
    if any(path not in architecture_source_documents for path in paths):
        return None
    return MappingProxyType(
        {path: architecture_source_documents[path] for path in paths}
    )


def inspect_repository_designs(
    project: Project,
) -> dict[str, Any]:
    """Validate every canonical source selected by one project context."""

    context = project
    root = context.project_root
    source_inventory = RepositorySourceInventory.for_project(context)
    operation_catalog_inventory = {
        owner.name: context.project_root.joinpath(
            *owner.component.target_catalog.parts
        ).resolve()
        for owner in context.owners
        if owner.component.target_catalog is not None
    }
    source_inventory.verify(
        "owner operation catalog snapshot",
        {
            path: source_inventory.resolve(path)
            for path in operation_catalog_inventory.values()
        },
    )
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

    platform_inventory = load_platform_inventory(context)
    platform_catalog = platform_inventory.catalog
    platform_catalog_path = platform_catalog.path

    architecture_source_documents = _architecture_source_documents(context)

    release_inventory = {}
    for name, path in release_paths.items():
        contract = load_ip_contract(path, project=context)
        if contract.name != name:
            raise ValueError(f"IP release catalog identity mismatch: {name}")
        release_inventory[name] = contract

    oa_source_inventory = {}
    for contract in release_inventory.values():
        if contract.oa_assembly is None:
            continue
        assembly = (root / contract.oa_assembly).resolve()
        if assembly not in oa_source_inventory:
            oa_source_inventory[assembly] = load_oa_library_source(
                assembly,
                project=context,
            )

    oa_plan_inventory = {
        assembly: plan_oa_library_rebuild(
            assembly,
            project=context,
            platform_inventory=platform_inventory,
            oa_source_inventory=oa_source_inventory,
            architecture_source_documents=architecture_source_documents,
        )
        for assembly in oa_source_inventory
    }
    oa_simulation_inventory = {}
    oa_design_inventory = {}
    oa_layout_inventory = {}
    layout_source_documents = {}
    for plan in oa_plan_inventory.values():
        for step in plan.designs:
            path = step.inspection.spec.path.resolve()
            if path in oa_design_inventory:
                raise ValueError(f"design path belongs to multiple OA plans: {path}")
            oa_design_inventory[path] = step.inspection.spec
        for step in plan.layouts:
            path = step.spec.path.resolve()
            if path in oa_layout_inventory:
                raise ValueError(f"layout path belongs to multiple OA plans: {path}")
            oa_layout_inventory[path] = step.spec
        for step in plan.testbenches:
            path = step.simulation.path.resolve()
            if path in oa_simulation_inventory:
                raise ValueError(
                    f"OA simulation path belongs to multiple plans: {path}"
                )
            oa_simulation_inventory[path] = step.simulation
    for path, snapshot in oa_layout_inventory.items():
        layout = resolve_layout_spec(
            path,
            project=context,
            snapshot=snapshot,
            platform=platform_inventory,
        )
        for source_path, document in layout.source_documents.items():
            previous = layout_source_documents.get(source_path)
            if previous is not None and previous != document:
                raise ValueError(
                    f"layout snapshots disagree for source: {source_path}"
                )
            layout_source_documents[source_path] = document

    components: dict[str, Any] = {}
    component_source_documents = {}
    integration_inventory = {}
    for name, path in component_paths.items():
        owner = context.require_owner(path)
        graph = load_component_graph(
            path,
            project_root=root,
            root_contract=(owner.component if owner.component.path == path else None),
            contract_inventory=context.component_inventory,
        )
        component = graph.get(name)
        if component is None or component.path != path:
            raise ValueError(f"IP component catalog identity mismatch: {name}")
        for graph_component in graph.values():
            previous = component_source_documents.get(graph_component.path)
            if previous is not None and previous != graph_component.document:
                raise ValueError(
                    "component graph snapshots disagree for source: "
                    f"{graph_component.path}"
                )
            component_source_documents[graph_component.path] = (
                graph_component.document
            )
        component_result: dict[str, Any] = {
            "contract": path.relative_to(root).as_posix(),
            "kind": component.kind,
            "graph": sorted(graph),
        }
        if "variants" in component.document:
            integration_contract = load_ip_integration_contract(
                path,
                project=context,
                variant_source_documents=_integration_variant_inventory(
                    context,
                    path,
                    architecture_source_documents,
                ),
            )
            integration = plan_ip_integration_contract(
                integration_contract,
                platform_inventory=platform_inventory,
                release_inventory=release_inventory,
                oa_source_inventory=oa_source_inventory,
                oa_plan_inventory=oa_plan_inventory,
            )
            if integration.get("ip") != name:
                raise ValueError(f"IP integration catalog identity mismatch: {name}")
            component_result["integration"] = integration
            integration_inventory[name] = integration_contract
        components[name] = component_result

    ip_releases: dict[str, Any] = {}
    oa_assemblies: dict[str, Any] = {}
    for name, contract in release_inventory.items():
        path = contract.path
        release_row = {
            "contract": path.relative_to(root).as_posix(),
            "default_maturity": contract.default_maturity,
            "exports": [item.name for item in contract.exports],
            "interface_kinds": sorted(
                {item.interface.kind for item in contract.exports}
            ),
        }
        if contract.oa_assembly is not None:
            assembly = (root / contract.oa_assembly).resolve()
            release_row["oa_assembly"] = contract.oa_assembly.as_posix()
            oa_assemblies[name] = oa_plan_inventory[assembly].as_dict()
        ip_releases[name] = release_row

    platforms: dict[str, Any] = {}
    for name, platform in platform_inventory.items():
        platforms[name] = {
            "manifest": platform.path.relative_to(root).as_posix(),
            "owner": platform.owner,
        }

    source_inventory.verify(
        "component graph snapshot",
        component_source_documents,
    )
    source_inventory.verify(
        "platform catalog snapshot",
        {platform_catalog.path: platform_catalog.document},
    )
    for platform in platform_inventory.values():
        source_inventory.verify(
            "platform source snapshot",
            platform.source_documents,
        )
    for contract in release_inventory.values():
        if contract.document:
            source_inventory.verify(
                "IP release contract snapshot",
                {contract.path: contract.document},
            )
        source_inventory.verify(
            "IP release interface snapshot",
            contract.interface_documents,
        )
    for contract in integration_inventory.values():
        source_inventory.verify(
            "IP integration source snapshot",
            contract.source_documents,
        )
    for source in oa_source_inventory.values():
        source_inventory.verify(
            "OA source snapshot",
            source.source_documents,
        )
    for simulation in oa_simulation_inventory.values():
        source_inventory.verify(
            "OA simulation snapshot",
            simulation.source_documents,
        )
    for design in oa_design_inventory.values():
        source_inventory.verify(
            "design snapshot",
            design.source_documents,
        )
    source_inventory.verify(
        "layout snapshot",
        layout_source_documents,
    )
    source_inventory.verify(
        "architecture source snapshot",
        architecture_source_documents,
    )
    configuration = inspect_project_configuration_sources(
        context,
        operation_catalog_inventory=operation_catalog_inventory,
        sources=source_inventory,
    )

    targets: dict[str, Any] = {}
    for owner in context.owners:
        if owner.component.target_catalog is None:
            continue
        catalog_path = operation_catalog_inventory[owner.name]
        document = source_inventory.resolve(catalog_path)
        target_rows = document.get("targets")
        if not isinstance(target_rows, Mapping):
            raise ValueError(f"{catalog_path}: targets must be a table")
        owner_targets: dict[str, Any] = {}
        for target_name, target_row in target_rows.items():
            if not isinstance(target_name, str) or not isinstance(target_row, Mapping):
                raise ValueError(f"{catalog_path}: target declarations are invalid")
            operations = target_row.get("operations")
            if not isinstance(operations, Mapping):
                raise ValueError(f"{catalog_path}: target {target_name!r} has no operations")
            compiled = {
                operation_name: compile_operation(
                    catalog_path,
                    owner=owner.name,
                    owner_root=owner.root,
                    target=target_name,
                    operation=operation_name,
                )
                for operation_name in operations
            }
            owner_targets[target_name] = {
                "description": target_row.get("description"),
                "operations": {
                    operation_name: {
                        "steps": [
                            {"id": step.id, "uses": step.uses}
                            for step in plan.steps
                        ]
                    }
                    for operation_name, plan in compiled.items()
                },
            }
        targets[owner.name] = owner_targets

    catalogs = {
        "ip": ip_catalog_path.relative_to(root).as_posix(),
        "platform": platform_catalog_path.relative_to(root).as_posix(),
        "target_catalogs": {
            owner: path.relative_to(root).as_posix()
            for owner, path in sorted(operation_catalog_inventory.items())
        },
    }
    return {
        "passed": True,
        "project": "sigilicon.toml",
        "configuration": configuration,
        "catalogs": catalogs,
        "components": components,
        "ip_releases": ip_releases,
        "targets": targets,
        "oa_assemblies": oa_assemblies,
        "platforms": platforms,
    }
