from __future__ import annotations

import ast
import inspect
from pathlib import Path

from sigilicon.virtuoso import (
    ade,
    importer,
    library,
    maestro_batch,
    oa,
    schematic,
    text_view,
)


LAYERS = {
    "root",
    "project",
    "domain",
    "execution",
    "adapters",
    "layout",
    "virtuoso",
    "workflows",
    "cli",
}
ALLOWED_DEPENDENCIES = {
    "root": {"project", "root"},
    "project": {"adapters", "execution", "project", "root"},
    "domain": {"domain", "project", "root"},
    "execution": {"execution", "root"},
    "adapters": {
        "adapters",
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
        "adapters",
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
FLOW_ROOT = Path(__file__).parents[1] / "src" / "sigilicon"


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
    assert FLOW_ROOT.is_dir()
    violations: list[str] = []
    for path in FLOW_ROOT.rglob("*.py"):
        relative = path.relative_to(FLOW_ROOT)
        source_layer = relative.parts[0] if len(relative.parts) > 1 else "root"
        allowed = ALLOWED_DEPENDENCIES[source_layer]
        for imported in _imports(path):
            target = _dependency_layer(imported)
            if target is not None and target not in allowed:
                violations.append(
                    f"{relative} -> {imported} ({source_layer}->{target})"
                )
    assert not violations, "invalid layer dependencies: " + ", ".join(violations)


def test_external_integration_apis_live_in_their_owned_adapter_layers() -> None:
    assert FLOW_ROOT.is_dir()
    bridge_users: set[str] = set()
    skill_users: set[str] = set()
    process_users: set[str] = set()
    for path in FLOW_ROOT.rglob("*.py"):
        relative = path.relative_to(FLOW_ROOT)
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
    platform_loader = FLOW_ROOT / "domain/platform.py"
    violations: list[str] = []
    for path in FLOW_ROOT.rglob("*.py"):
        if path == platform_loader:
            continue
        source = path.read_text(encoding="utf-8")
        if "platform-definition" in source or "platform-simulation" in source:
            violations.append(str(path.relative_to(FLOW_ROOT)))
        if ".pdk.path" in source or "platform_config(" in source:
            violations.append(str(path.relative_to(FLOW_ROOT)))

    assert violations == []


def test_layout_owner_code_is_loaded_only_inside_the_worker_process() -> None:
    layout_root = FLOW_ROOT / "layout"
    dynamic_loaders: set[str] = set()
    path_mutators: set[str] = set()
    for path in layout_root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        relative = path.relative_to(FLOW_ROOT).as_posix()
        if "spec_from_file_location" in source:
            dynamic_loaders.add(relative)
        if "sys.path.insert" in source:
            path_mutators.add(relative)

    assert dynamic_loaders == {"layout/_generator_worker.py"}
    assert path_mutators == dynamic_loaders
    assert "tempfile" not in _imports(layout_root / "generator.py")


def test_deleted_parallel_framework_surfaces_do_not_return() -> None:
    removed = {
        "PreparedPlan",
        "PreparedStep",
        "StepContext",
        "StepWorkspace",
        "PdkConfig",
        "PlatformContract",
        "build_ip_release",
        "sigilicon.backends",
    }
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in FLOW_ROOT.rglob("*.py")
    )

    assert not (FLOW_ROOT / "backends").exists()
    assert all(name not in sources for name in removed)


def test_stateful_virtuoso_adapters_expose_an_operation_context() -> None:
    stateful = (
        ade.create_oa_native_config_view,
        ade.create_oa_native_maestro_view,
        importer.generate_symbol,
        importer.import_schematic,
        library.create_project_library,
        library.ensure_project_library,
        maestro_batch.run_isolated_maestro,
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
