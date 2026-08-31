from __future__ import annotations

import importlib
from pathlib import Path
import sys

from sigilicon.project_modules import project_import_path


def _package(root: Path, value: str) -> None:
    package = root / "ownedpkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        f"VALUE = {value!r}\n",
        encoding="utf-8",
    )


def test_project_import_path_prioritizes_isolates_and_restores_modules(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    selected = tmp_path / "selected"
    _package(first, "first")
    _package(selected, "selected")
    previous_path = tuple(sys.path)
    previous_module = sys.modules.pop("ownedpkg", None)
    try:
        sys.path.insert(0, str(first))
        original = importlib.import_module("ownedpkg")
        sys.path.append(str(selected))
        outer_path = tuple(sys.path)

        with project_import_path(selected, module_names=("ownedpkg",)):
            imported = importlib.import_module("ownedpkg")
            assert sys.path[0] == str(selected.resolve())
            assert imported.VALUE == "selected"
            assert imported is not original

        assert tuple(sys.path) == outer_path
        assert sys.modules["ownedpkg"] is original
    finally:
        sys.path[:] = previous_path
        sys.modules.pop("ownedpkg", None)
        if previous_module is not None:
            sys.modules["ownedpkg"] = previous_module
