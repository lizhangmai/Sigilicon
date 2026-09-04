"""Private entry point for one isolated layout-generator invocation."""

from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import json
from pathlib import Path
import sys

from sigilicon.layout.generator import LayoutGeneratorInput
from sigilicon.layout.ir import LayoutPlan


def _execute_generator(
    spec: LayoutGeneratorInput,
    *,
    project_root: Path,
    generator_source: Path,
) -> LayoutPlan:
    root = project_root.absolute()
    source = generator_source.absolute()
    if root != root.resolve() or source != source.resolve():
        raise ValueError("layout generator paths must not traverse symlinks")
    if not source.is_file() or not source.is_relative_to(root):
        raise ValueError("layout generator must be a file inside the sealed project")
    sys.path.insert(0, str(root))
    module_spec = importlib.util.spec_from_file_location(
        "_sigilicon_layout_generator", source
    )
    if module_spec is None or module_spec.loader is None:
        raise ValueError(f"cannot load layout generator source: {source}")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module.__name__] = module
    module_spec.loader.exec_module(module)
    entrypoint = getattr(module, "build_layout_plan", None)
    if not callable(entrypoint):
        raise ValueError(f"layout generator {source} must export build_layout_plan")
    plan = entrypoint(spec)
    if not isinstance(plan, LayoutPlan):
        raise TypeError(
            f"layout generator {source} returned {type(plan).__name__}, "
            "expected LayoutPlan"
        )
    identity = (plan.library, plan.cell, plan.view, plan.generator, plan.stage)
    expected = (spec.library, spec.cell, spec.view, spec.generator, spec.stage)
    if identity != expected:
        raise ValueError(
            f"layout generator changed spec identity: got={identity}, expected={expected}"
        )
    return plan


def main() -> None:
    if len(sys.argv) != 2:
        raise ValueError("layout generator worker requires one request descriptor")
    try:
        descriptor = int(sys.argv[1])
    except ValueError as exc:
        raise ValueError("layout generator request descriptor must be an integer") from exc
    with Path(f"/proc/self/fd/{descriptor}").open("r", encoding="utf-8") as stream:
        request = json.load(stream)
    if not isinstance(request, dict) or set(request) != {
        "schema",
        "project_root",
        "generator_source",
        "spec",
    }:
        raise ValueError("layout generator request has invalid fields")
    if request["schema"] != 1:
        raise ValueError("unsupported layout generator request schema")
    root = Path(request["project_root"])
    relative = Path(request["generator_source"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("generator_source must be a safe relative path")
    spec = LayoutGeneratorInput.from_payload(request["spec"])
    with redirect_stdout(sys.stderr):
        plan = _execute_generator(
            spec,
            project_root=root,
            generator_source=root / relative,
        )
    sys.stdout.write(plan.canonical_json())


if __name__ == "__main__":
    main()
