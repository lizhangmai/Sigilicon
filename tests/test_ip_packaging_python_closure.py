from __future__ import annotations

from pathlib import Path

from sigilicon.workflows.ip_packaging import (
    _python_import_closure,
    _python_module_paths,
    _sigilicon_tool_identity,
)


def test_python_import_closure_tracks_project_owned_root_modules(tmp_path: Path) -> None:
    package = tmp_path / "project_layout"
    package.mkdir()
    initializer = package / "__init__.py"
    initializer.write_text("", encoding="utf-8")
    helper = package / "helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    recipe = package / "recipe.py"
    recipe.write_text(
        "from project_layout.helper import VALUE\n",
        encoding="utf-8",
    )

    paths = _python_module_paths(tmp_path, "project_layout.recipe")
    _python_import_closure(tmp_path, paths)

    assert paths == {initializer.resolve(), helper.resolve(), recipe.resolve()}


def test_python_module_paths_do_not_snapshot_external_packages(tmp_path: Path) -> None:
    assert _python_module_paths(tmp_path, "sigilicon.layout.mos") == set()


def test_sigilicon_tool_identity_has_version_and_source_digest() -> None:
    identity = _sigilicon_tool_identity()

    assert identity["version"]
    assert len(identity["source_sha256"]) == 64
