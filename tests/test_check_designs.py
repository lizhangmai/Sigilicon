from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import write_component_owner
from sigilicon.cli.main import main as sigilicon_main
from sigilicon.project import Project
import sigilicon.project.checks as repository_checks


def test_core_project_and_check_do_not_require_domain_catalogs(
    tmp_path: Path,
) -> None:
    (tmp_path / "sigilicon.toml").write_text(
        '''schema = 1
contract_kind = "sigilicon-project"
path_scope = "repository"
owner = "standalone"

[catalogs]

[paths]
project_root = "."
workspace_root = "workspace"
artifact_root = "artifacts"
''',
        encoding="utf-8",
    )

    project = Project.open(tmp_path)
    report = repository_checks.inspect_repository_designs(project)

    assert project.catalog_paths == ()
    assert project.owners == ()
    assert report["passed"] is True
    assert report["catalogs"] == {"operation_catalogs": {}}
    assert report["components"] == {}
    assert report["platforms"] == {}


def test_core_project_preserves_an_arbitrary_domain_catalog(
    tmp_path: Path,
) -> None:
    catalog = tmp_path / "domains/custom.toml"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        '''schema = 1
contract_kind = "custom-circuit-catalog"
path_scope = "repository"
owner = "standalone"
''',
        encoding="utf-8",
    )
    (tmp_path / "sigilicon.toml").write_text(
        '''schema = 1
contract_kind = "sigilicon-project"
path_scope = "repository"
owner = "standalone"

[catalogs]
custom = "domains/custom.toml"

[paths]
project_root = "."
workspace_root = "workspace"
artifact_root = "artifacts"
''',
        encoding="utf-8",
    )

    project = Project.open(tmp_path)
    report = repository_checks.inspect_repository_designs(project)

    assert project.catalog("custom") == catalog.resolve()
    assert report["catalogs"] == {
        "custom": "domains/custom.toml",
        "operation_catalogs": {},
    }


def test_check_cli_defaults_to_an_operator_sized_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    assert sigilicon_main(["check"]) == 0
    summary = json.loads(capsys.readouterr().out)

    assert summary["passed"] is True
    assert summary["configuration"]["documents"] >= 1
    assert set(summary) == {
        "passed",
        "project",
        "configuration",
        "components",
        "operations",
        "releases",
        "oa_assemblies",
        "platforms",
    }


def test_project_manifest_source_document_is_frozen_and_resolved(
    tmp_path: Path,
) -> None:
    project = Project.open(tmp_path)

    assert project.manifest_source_document() == project.manifest_document
    with pytest.raises(TypeError):
        project.manifest_document["catalogs"]["ip"] = "other.toml"

    run_scoped = project.with_artifact_root(tmp_path / "run-artifacts")
    assert run_scoped.manifest_source_document() == project.manifest_document
    manifest = tmp_path / "sigilicon.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            'owner = "test"', 'owner = "changed"', 1
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="source document drift"):
        project.manifest_source_document()


def test_architecture_inventory_rejects_owner_drift(
    tmp_path: Path,
) -> None:
    architecture = tmp_path / "ip/example/configs/behavior.toml"
    architecture.parent.mkdir(parents=True)
    architecture.write_text(
        '''schema = 1
contract_kind = "ip-architecture-behavior"
path_scope = "owner"
owner = "other"
''',
        encoding="utf-8",
    )
    write_component_owner(
        tmp_path,
        "example",
        filesets={
            "architecture": ("ip/example/configs/behavior.toml",),
        },
    )

    with pytest.raises(ValueError, match="owner must be 'example'"):
        repository_checks.inspect_repository_designs(Project.open(tmp_path))


def test_architecture_inventory_rejects_cross_owner_sources(
    tmp_path: Path,
) -> None:
    architecture = tmp_path / "ip/other/configs/behavior.toml"
    architecture.parent.mkdir(parents=True)
    architecture.write_text("name = 'native'\n", encoding="utf-8")
    write_component_owner(tmp_path, "other", filesets={})
    write_component_owner(
        tmp_path,
        "example",
        filesets={
            "architecture": ("ip/other/configs/behavior.toml",),
        },
    )

    with pytest.raises(ValueError, match="cataloged root|architecture fileset source"):
        repository_checks.inspect_repository_designs(Project.open(tmp_path))
