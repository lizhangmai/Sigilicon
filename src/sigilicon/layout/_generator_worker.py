"""Private entry point for one isolated layout-generator invocation."""

from __future__ import annotations

from contextlib import redirect_stdout
import json
from pathlib import Path
import sys

from sigilicon.layout.generator import LayoutGeneratorInput, execute_layout_generator


def main() -> None:
    if len(sys.argv) != 2:
        raise ValueError("layout generator worker requires one request file")
    with Path(sys.argv[1]).open("r", encoding="utf-8") as stream:
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
        plan = execute_layout_generator(
            spec,
            project_root=root,
            generator_source=root / relative,
        )
    sys.stdout.write(plan.canonical_json())


if __name__ == "__main__":
    main()
