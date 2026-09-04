from __future__ import annotations

import ast
from pathlib import Path


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
    "project": {"adapters", "domain", "execution", "project", "root"},
    "domain": {"domain", "root"},
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
