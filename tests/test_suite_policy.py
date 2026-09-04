"""Keep regression tests coupled to behavior instead of Python object shape."""

from __future__ import annotations

import ast
from pathlib import Path


TEST_ROOT = Path(__file__).parent
PROHIBITED_ATTRIBUTES = {"__dict__", "__dataclass_fields__"}
PROHIBITED_BUILTINS = {"dir", "hasattr", "vars"}
PROHIBITED_INSPECTION = {"getmembers", "getsource", "signature"}


def _call_name(node: ast.expr) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        return (*_call_name(node.value), node.attr)
    return ()


def test_regressions_do_not_introspect_production_object_shape() -> None:
    violations: list[str] = []
    for path in sorted(TEST_ROOT.glob("test_*.py")):
        if path == Path(__file__):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                alias.name == "inspect" for alias in node.names
            ):
                violations.append(f"{path.name}:{node.lineno}: import inspect")
            elif isinstance(node, ast.ImportFrom) and node.module == "inspect":
                violations.append(f"{path.name}:{node.lineno}: from inspect")
            elif isinstance(node, ast.Attribute) and node.attr in PROHIBITED_ATTRIBUTES:
                violations.append(f"{path.name}:{node.lineno}: .{node.attr}")
            elif isinstance(node, ast.Call):
                name = _call_name(node.func)
                if name and (
                    name[-1] in PROHIBITED_BUILTINS
                    or name[-1] in PROHIBITED_INSPECTION
                ):
                    violations.append(
                        f"{path.name}:{node.lineno}: {'/'.join(name)}()"
                    )

    assert violations == []
