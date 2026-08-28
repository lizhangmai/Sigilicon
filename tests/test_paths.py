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

    execution = paths.execution(
        owner="lib",
        target="inv",
        flow="design-sync",
        variant="recursive",
        identity=ATTEMPT,
        artifact_kind="design_sync",
        identity_kind="attempt_id",
    )
    assert execution.root == (
        tmp_path / "artifacts/runs/lib/inv/design-sync/recursive" / ATTEMPT
    )
    assert execution.roles == ("inputs", "work", "outputs", "logs")
    assert paths.export("lib", "netlist", "inv.scs") == (
        tmp_path / "artifacts/exports/lib/netlist/inv.scs"
    )
    assert paths.system_operation(RUN).incident == (
        tmp_path / "artifacts/system/operations" / RUN / "incident.json"
    )


@pytest.mark.parametrize(
    "unsafe",
    ("", ".", "..", "../inv", "/absolute", "a/b", "a\\b", "has space"),
)
def test_artifact_components_reject_escape_and_separators(unsafe: str) -> None:
    with pytest.raises(ValueError, match="invalid artifact component"):
        validate_artifact_component(unsafe, "component")


def test_ids_and_role_components_are_validated(tmp_path: Path) -> None:
    paths = ProjectContext.from_project_root(tmp_path).artifacts
    with pytest.raises(ValueError, match="attempt id"):
        paths.execution(
            owner="lib",
            target="inv",
            flow="design-sync",
            variant="recursive",
            identity="../unsafe",
            artifact_kind="design_sync",
            identity_kind="attempt_id",
        )
    execution = paths.execution(
        owner="lib",
        target="tb",
        flow="spectre",
        variant="nominal",
        identity=RUN,
        artifact_kind="standalone_simulation",
        identity_kind="run_id",
    )
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
        ("paths", 'unexpected = "path"\n'),
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
        filesets={"source": ("configs/platform/catalog.toml",)},
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

    with pytest.raises(ValueError, match="unknown catalog roles.*unexpected"):
        RepositoryContext.from_project_root(tmp_path)


def test_repository_context_rejects_a_retired_catalog_domain(tmp_path: Path) -> None:
    contract = tmp_path / "sigilicon.toml"
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            '[catalogs]\n', '[catalogs]\nlegacy = "catalogs/legacy.toml"\n'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown catalog roles.*legacy"):
        RepositoryContext.from_project_root(tmp_path)


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
    execution = ProjectContext.from_project_root(tmp_path).artifacts.execution(
        owner="lib",
        target="tb",
        flow="spectre",
        variant="nominal",
        identity=RUN,
        artifact_kind="standalone_simulation",
        identity_kind="run_id",
    )

    with pytest.raises(RuntimeError, match="unsafe filesystem component"):
        execution.create()
    assert list(outside.iterdir()) == []
