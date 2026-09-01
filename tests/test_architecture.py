from __future__ import annotations

import ast
import inspect
from pathlib import Path

from sigilicon.virtuoso import (
    ade,
    importer,
    layout,
    library,
    maestro_batch,
    netlisting,
    oa,
    schematic,
    text_view,
)


LAYERS = {
    "root",
    "project",
    "domain",
    "execution",
    "backends",
    "layout",
    "virtuoso",
    "workflows",
    "cli",
}
ALLOWED_DEPENDENCIES = {
    "root": {"project", "root"},
    "project": {"backends", "execution", "project", "root"},
    "domain": {"domain", "project", "root"},
    "execution": {"execution", "root"},
    "backends": {
        "backends",
        "domain",
        "execution",
        "project",
        "virtuoso",
        "workflows",
        "root",
    },
    "layout": {"domain", "layout", "project", "root"},
    "virtuoso": {"domain", "layout", "virtuoso", "root"},
    "workflows": {
        "domain",
        "execution",
        "layout",
        "project",
        "virtuoso",
        "workflows",
        "root",
    },
    "cli": {
        "backends",
        "domain",
        "execution",
        "layout",
        "project",
        "virtuoso",
        "workflows",
        "cli",
        "root",
    },
}
COMPOSITION_ROOTS: dict[Path, str] = {}
def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _dependency_layer(module: str) -> str | None:
    if not module.startswith("sigilicon."):
        return None
    component = module.split(".", 2)[1]
    return component if component in LAYERS else "root"


def test_sigilicon_modules_follow_the_declared_layer_dependency_matrix() -> None:
    flow_root = Path(__file__).parents[1] / "src" / "sigilicon"
    assert flow_root.is_dir()
    violations: list[str] = []
    for path in flow_root.rglob("*.py"):
        relative = path.relative_to(flow_root)
        source_layer = COMPOSITION_ROOTS.get(
            relative,
            relative.parts[0] if len(relative.parts) > 1 else "root",
        )
        allowed = ALLOWED_DEPENDENCIES[source_layer]
        for imported in _imports(path):
            target = _dependency_layer(imported)
            if target is not None and target not in allowed:
                violations.append(
                    f"{relative} -> {imported} ({source_layer}->{target})"
                )
    assert not violations, "invalid layer dependencies: " + ", ".join(violations)


def test_external_integration_apis_live_in_their_owned_adapter_layers() -> None:
    flow_root = Path(__file__).parents[1] / "src" / "sigilicon"
    assert flow_root.is_dir()
    bridge_users: set[str] = set()
    skill_users: set[str] = set()
    process_users: set[str] = set()
    for path in flow_root.rglob("*.py"):
        relative = path.relative_to(flow_root)
        imports = _imports(path)
        if any(name.startswith("virtuoso_bridge") for name in imports):
            bridge_users.add(str(relative))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Attribute) and node.attr == "execute_skill"
            for node in ast.walk(tree)
        ):
            skill_users.add(str(relative))
        if "subprocess" in imports:
            process_users.add(str(relative))

    assert bridge_users <= {"virtuoso/bridge.py"}
    assert all(path.startswith("virtuoso/") for path in skill_users)
    assert process_users <= {"external_tools.py", "process_supervisor.py"}


def test_platform_source_contracts_have_one_loader() -> None:
    flow_root = Path(__file__).parents[1] / "src" / "sigilicon"
    platform_loader = flow_root / "domain/platform.py"
    violations: list[str] = []
    for path in flow_root.rglob("*.py"):
        if path == platform_loader:
            continue
        source = path.read_text(encoding="utf-8")
        if "platform-definition" in source or "platform-simulation" in source:
            violations.append(str(path.relative_to(flow_root)))
        if ".pdk.path" in source or "platform_config(" in source:
            violations.append(str(path.relative_to(flow_root)))

    assert violations == []


def test_stateful_virtuoso_adapters_expose_an_operation_context() -> None:
    stateful = (
        ade.create_oa_native_config_view,
        ade.create_oa_native_maestro_view,
        importer.generate_symbol,
        importer.import_schematic,
        layout.read_layout_geometry,
        library.create_project_library,
        library.ensure_project_library,
        maestro_batch.run_isolated_maestro,
            netlisting.export_netlist,
            oa.close_visible_cell_windows,
            oa.set_cell_port_directions,
            oa.validate_cell_port_directions,
        schematic.read_instance_parameters,
        schematic.read_schematic,
        schematic.set_instance_parameters,
        text_view.import_oa_text_view,
    )
    parameters = {
        f"{function.__module__}.{function.__name__}": inspect.signature(function).parameters
        for function in stateful
    }
    assert all("operation" in names for names in parameters.values())
