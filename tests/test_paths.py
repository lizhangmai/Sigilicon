from __future__ import annotations

from pathlib import Path

import pytest

from sigilicon.cli.common import open_cli_project
from sigilicon.paths import (
    ArtifactLayout,
    ProjectContext,
    validate_artifact_component,
)
from sigilicon.project import Project

from conftest import write_component_owner


RUN = "1" * 32
def test_cli_discovery_uses_the_project_contract_not_pixi_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PIXI_PROJECT_ROOT", "/not/the/project")

    context = open_cli_project(None)

    assert context.project_root == tmp_path.resolve()
    assert context.workspace_root == (tmp_path / "virtuoso").resolve()


@pytest.mark.parametrize(
    "unsafe",
    ("", ".", "..", "../inv", "/absolute", "a/b", "a\\b", "has space"),
)
def test_artifact_components_reject_escape_and_separators(unsafe: str) -> None:
    with pytest.raises(ValueError, match="invalid artifact component"):
        validate_artifact_component(unsafe, "component")


def test_ids_and_role_components_are_validated(tmp_path: Path) -> None:
    paths = ArtifactLayout(tmp_path / "artifacts")
    with pytest.raises(ValueError, match="run id"):
        paths.operation_run(
            owner="lib",
            operation="design-sync",
            variant="recursive",
            run_id="../unsafe",
        )
    execution = paths.operation_run(
        owner="lib",
        operation="spectre",
        variant="nominal",
        run_id=RUN,
    )
    with pytest.raises(ValueError, match="path component"):
        execution.path("inputs", "../escape")


def test_project_root_cannot_be_reused_as_artifact_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not be the project root"):
        ProjectContext.from_roots(
            tmp_path,
            artifact_root=tmp_path,
            workspace_root=tmp_path / "virtuoso",
        )


def test_repository_owner_filesets_cannot_escape_the_cataloged_root(
    tmp_path: Path,
) -> None:
    write_component_owner(
        tmp_path,
        "example",
        filesets={"source": ("configs/platform/catalog.toml",)},
    )

    with pytest.raises(ValueError, match="component source escapes"):
        Project.open(tmp_path)


def test_project_resolves_its_manifest_contract(tmp_path: Path) -> None:
    project = Project.open(tmp_path)

    assert project.manifest_owner == "test"
    assert project.project_root == tmp_path.resolve()
    assert project.artifact_root == (tmp_path / "artifacts").resolve()


def test_execution_creation_rejects_symlinked_structural_components(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    artifacts.mkdir()
    outside.mkdir()
    (artifacts / "runs").symlink_to(
        outside,
        target_is_directory=True,
    )
    execution = ArtifactLayout(tmp_path / "artifacts").operation_run(
        owner="lib",
        operation="spectre",
        variant="nominal",
        run_id=RUN,
    )

    with pytest.raises(RuntimeError, match="unsafe filesystem component"):
        execution.create()
    assert list(outside.iterdir()) == []
