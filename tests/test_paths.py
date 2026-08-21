from __future__ import annotations

from pathlib import Path

import pytest

from sigilicon.paths import (
    ProjectContext,
    discover_project_context,
    validate_artifact_component,
)
from sigilicon.domain.repository import RepositoryContext

from conftest import write_component_owner


RUN = "1" * 32
ATTEMPT = "2" * 32


def test_cli_discovery_uses_the_project_contract_not_pixi_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PIXI_PROJECT_ROOT", "/not/the/project")

    context = discover_project_context()

    assert context.project_root == tmp_path.resolve()
    assert context.workspace_root == (tmp_path / "virtuoso").resolve()


def test_all_artifact_paths_match_the_single_layout(tmp_path: Path) -> None:
    paths = ProjectContext.from_project_root(tmp_path).artifacts

    assert paths.design_sync_attempt("lib", "inv", ATTEMPT).root == (
        tmp_path / "artifacts/designs/lib/inv/sync/attempts" / ATTEMPT
    )
    text_view = paths.oa_text_view_attempt("lib", "inv", "veriloga", ATTEMPT)
    assert text_view.root == (
        tmp_path
        / "artifacts/designs/lib/inv/views/veriloga/sync/attempts"
        / ATTEMPT
    )
    assert text_view.artifact_kind == "oa_text_view"
    assert paths.netlist_export_run(
        "lib", "inv", "schematic", "spectre", RUN
    ).root == (
        tmp_path
        / "artifacts/designs/lib/inv/exports/netlist/schematic/spectre/runs"
        / RUN
    )
    assert paths.standalone_run("lib", "tb_inv", RUN).root == (
        tmp_path / "artifacts/verification/lib/tb_inv/standalone/runs" / RUN
    )
    assert paths.import_attempt("lib", "source", ATTEMPT).root == (
        tmp_path / "artifacts/imports/lib/source/attempts" / ATTEMPT
    )
    analysis = paths.analysis_run("lib", "cell", "timing", "model", RUN)
    assert analysis.root == (
        tmp_path
        / "artifacts/designs/lib/cell/timing/model/runs"
        / RUN
    )
    assert analysis.artifact_kind == "analysis"
    assert paths.operation_incident(RUN).incident == (
        tmp_path / "artifacts/system/operations" / RUN / "incident.json"
    )


@pytest.mark.parametrize(
    "unsafe",
    ("", ".", "..", "../inv", "/absolute", "a/b", "a\\b", "has space"),
)
def test_artifact_components_reject_escape_and_separators(unsafe: str) -> None:
    with pytest.raises(ValueError, match="invalid artifact component"):
        validate_artifact_component(unsafe, "component")


def test_ids_fingerprints_and_role_components_are_validated(tmp_path: Path) -> None:
    paths = ProjectContext.from_project_root(tmp_path).artifacts
    with pytest.raises(ValueError, match="attempt id"):
        paths.design_sync_attempt("lib", "inv", "short")
    execution = paths.standalone_run("lib", "tb", RUN)
    with pytest.raises(ValueError, match="path component"):
        execution.path("inputs", "../escape")


def test_project_root_cannot_be_reused_as_artifact_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not be the project root"):
        ProjectContext.from_project_root(tmp_path, artifact_root=tmp_path)


def test_project_context_rejects_unknown_path_field(tmp_path: Path) -> None:
    contract = tmp_path / "sigilicon.toml"
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            'workspace_root = "virtuoso"',
            'unexpected_root = "configs"\nworkspace_root = "virtuoso"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown fields.*unexpected_root"):
        ProjectContext.from_project_root(tmp_path)


@pytest.mark.parametrize(
    ("section", "unknown"),
    (
        ("root", 'unexpected = "root"\n'),
        ("project", 'unexpected = "project"\n'),
    ),
)
def test_project_context_rejects_unknown_schema_fields(
    tmp_path: Path,
    section: str,
    unknown: str,
) -> None:
    contract = tmp_path / "sigilicon.toml"
    source = contract.read_text(encoding="utf-8")
    if section == "root":
        source = unknown + source
    else:
        source = source.replace(f"[{section}]\n", f"[{section}]\n{unknown}")
    contract.write_text(source, encoding="utf-8")

    with pytest.raises(ValueError, match="contains unknown fields.*unexpected"):
        ProjectContext.from_project_root(tmp_path)


def test_repository_owner_filesets_cannot_escape_the_cataloged_root(
    tmp_path: Path,
) -> None:
    write_component_owner(
        tmp_path,
        "example",
        filesets={"source": ("catalogs/soc.toml",)},
    )

    with pytest.raises(ValueError, match="component source escapes"):
        RepositoryContext.from_project_root(tmp_path)


def test_repository_context_rejects_unknown_catalog_roles(tmp_path: Path) -> None:
    contract = tmp_path / "sigilicon.toml"
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            '[catalogs]\n', '[catalogs]\nunexpected = "catalogs/extra.toml"\n'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="exactly ip, soc, and platform"):
        RepositoryContext.from_project_root(tmp_path)


def test_execution_creation_rejects_symlinked_structural_components(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    artifacts.mkdir()
    outside.mkdir()
    (artifacts / "verification").mkdir()
    (artifacts / "verification" / "lib").symlink_to(
        outside,
        target_is_directory=True,
    )
    execution = ProjectContext.from_project_root(tmp_path).artifacts.standalone_run(
        "lib", "tb", RUN
    )

    with pytest.raises(RuntimeError, match="unsafe filesystem component"):
        execution.create()
    assert list(outside.iterdir()) == []
