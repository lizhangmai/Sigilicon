from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.virtuoso.library import create_project_library
from sigilicon.virtuoso.workspace import require_project_library_path as _real_library_path


class _Library:
    def __init__(self, path: Path, technology: str | None = None) -> None:
        self.info = SimpleNamespace(
            path=str(path),
            technology_library=technology,
        )

    def list(self, **_kwargs):
        return ["lib"]

    def get(self, _library, **_kwargs):
        return self.info


class _Client:
    def __init__(self, path: Path, technology: str | None = None) -> None:
        self.library = _Library(path, technology)


def test_if_missing_repairs_cds_lib_for_existing_project_library(
    tmp_path, workspace_factory
) -> None:
    client = _Client(tmp_path / "placeholder")

    with workspace_factory(client) as operation:
        path = operation.root / "lib"
        path.mkdir(parents=True)
        client.library.info.path = str(path)
        cds_lib = operation.root / "cds.lib"
        cds_lib.write_text("# project libraries\n", encoding="utf-8")
        with operation.mutation_scope(
            "lib",
            cells=None,
            phase="test library reconciliation",
            expected_library_path=path,
            require_view_lease=False,
        ):
            result = create_project_library(
                client,
                library="lib",
                path=path,
                technology_library=None,
                cds_lib=cds_lib,
                if_missing=True,
                operation=operation,
            )

    assert result.action == "exists"
    assert result.cds_entry == "added: DEFINE lib ./lib"
    assert "DEFINE lib ./lib" in cds_lib.read_text(encoding="utf-8")


def test_if_missing_coalesces_equivalent_cds_lib_definitions(
    tmp_path, workspace_factory
) -> None:
    client = _Client(tmp_path / "placeholder")

    with workspace_factory(client) as operation:
        path = operation.root / "lib"
        path.mkdir(parents=True)
        client.library.info.path = str(path)
        cds_lib = operation.root / "cds.lib"
        cds_lib.write_text(
            f"DEFINE lib ./lib\nDEFINE lib {path}\n",
            encoding="utf-8",
        )
        with operation.mutation_scope(
            "lib",
            cells=None,
            phase="test equivalent library reconciliation",
            expected_library_path=path,
            require_view_lease=False,
        ):
            result = create_project_library(
                client,
                library="lib",
                path=path,
                technology_library=None,
                cds_lib=cds_lib,
                if_missing=True,
                operation=operation,
            )

    assert result.cds_entry == "normalized: DEFINE lib ./lib"
    assert cds_lib.read_text(encoding="utf-8").count("DEFINE lib ") == 1


def test_if_missing_rejects_conflicting_cds_lib_definitions(
    tmp_path, workspace_factory
) -> None:
    client = _Client(tmp_path / "placeholder")

    with pytest.raises(RuntimeError, match="duplicate DEFINE entries"):
        with workspace_factory(client) as operation:
            path = operation.root / "lib"
            path.mkdir(parents=True)
            client.library.info.path = str(path)
            cds_lib = operation.root / "cds.lib"
            cds_lib.write_text(
                "DEFINE lib ./lib\nDEFINE lib ./other\n",
                encoding="utf-8",
            )
            with operation.mutation_scope(
                "lib",
                cells=None,
                phase="test conflicting library reconciliation",
                expected_library_path=path,
                require_view_lease=False,
            ):
                create_project_library(
                    client,
                    library="lib",
                    path=path,
                    technology_library=None,
                    cds_lib=cds_lib,
                    if_missing=True,
                    operation=operation,
                )


def test_if_missing_rejects_existing_library_outside_requested_path(
    monkeypatch, tmp_path, workspace_factory
) -> None:
    client = _Client(tmp_path / "external-lib")
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.require_project_library_path",
        _real_library_path,
    )

    with workspace_factory(client) as operation:
        path = operation.root / "lib"
        path.mkdir(parents=True)
        cds_lib = operation.root / "cds.lib"
        cds_lib.write_text("# project libraries\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="leaves the project workspace|resolves to"):
            with operation.mutation_scope(
                "lib",
                cells=None,
                phase="test external library rejection",
                expected_library_path=path,
                require_view_lease=False,
            ):
                create_project_library(
                    client,
                    library="lib",
                    path=path,
                    technology_library=None,
                    cds_lib=cds_lib,
                    if_missing=True,
                    operation=operation,
                )


def test_cds_lib_symlink_is_rejected_without_touching_target(
    tmp_path,
    workspace_factory,
) -> None:
    client = _Client(tmp_path / "placeholder")
    outside = tmp_path / "outside-cds.lib"
    outside.write_text("do-not-touch\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="symbolic-link project cds.lib"):
        with workspace_factory(client) as operation:
            path = operation.root / "lib"
            path.mkdir(parents=True)
            client.library.info.path = str(path)
            cds_lib = operation.root / "cds.lib"
            cds_lib.symlink_to(outside)
            with operation.mutation_scope(
                "lib",
                cells=None,
                phase="cds.lib symlink proof",
                expected_library_path=path,
                require_view_lease=False,
            ):
                create_project_library(
                    client,
                    library="lib",
                    path=path,
                    technology_library=None,
                    cds_lib=cds_lib,
                    if_missing=True,
                    operation=operation,
                )

    assert outside.read_text(encoding="utf-8") == "do-not-touch\n"
