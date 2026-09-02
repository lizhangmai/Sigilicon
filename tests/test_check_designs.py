from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tomllib

import pytest

from conftest import write_component_owner
import sigilicon.project._project as repository_module
from sigilicon.cli.check_designs import main as check_designs_main
from sigilicon.contracts import freeze_toml_document
from sigilicon.project import Project
import sigilicon.workflows.repository_checks as repository_checks


def test_check_designs_parses_the_project_manifest_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    contract = (tmp_path / "sigilicon.toml").resolve()
    original = repository_module.read_toml
    original_load = tomllib.load
    manifest_reads = 0
    toml_reads = 0

    def counted(path: Path):
        nonlocal manifest_reads
        if path.resolve() == contract:
            manifest_reads += 1
        return original(path)

    def counted_load(stream):
        nonlocal toml_reads
        if Path(stream.name).resolve() == contract:
            toml_reads += 1
        return original_load(stream)

    monkeypatch.setattr(repository_module, "read_toml", counted)
    monkeypatch.setattr(tomllib, "load", counted_load)
    monkeypatch.chdir(tmp_path)

    assert check_designs_main([]) == 0
    assert manifest_reads == 1
    assert toml_reads == 1
    assert '"passed": true' in capsys.readouterr().out


def test_project_manifest_source_document_is_frozen_and_resolved(
    tmp_path: Path,
) -> None:
    project = Project.open(tmp_path)

    assert project.manifest_source_document() is project.manifest_document
    with pytest.raises(TypeError):
        project.manifest_document["catalogs"]["ip"] = "other.toml"
    with pytest.raises(ValueError, match="source document drift"):
        replace(
            project,
            manifest_document=dict(project.manifest_document),
        ).manifest_source_document()

    drifted = dict(project.manifest_document)
    drifted["catalogs"] = {
        **project.manifest_document["catalogs"],
        "ip": "configs/platform/catalog.toml",
    }
    with pytest.raises(ValueError, match="catalog|source document drift"):
        replace(
            project,
            manifest_document=freeze_toml_document(drifted),
        ).manifest_source_document()

    run_scoped = project.with_artifact_root(tmp_path / "run-artifacts")
    assert run_scoped.manifest_source_document() is project.manifest_document
    path_drift = dict(project.manifest_document)
    path_drift["paths"] = {
        **project.manifest_document["paths"],
        "artifact_root": "other-artifacts",
    }
    with pytest.raises(ValueError, match="source document drift"):
        replace(
            project,
            manifest_document=freeze_toml_document(path_drift),
        ).manifest_source_document()


def test_architecture_inventory_preserves_typed_variant_sources_for_reuse(
    tmp_path: Path,
) -> None:
    behavior = tmp_path / "ip/example/configs/behavior.toml"
    variant = tmp_path / "ip/example/configs/variant.toml"
    behavior.parent.mkdir(parents=True)
    behavior.write_text(
        '''schema = 1
contract_kind = "ip-architecture-behavior"
path_scope = "owner"
owner = "example"
''',
        encoding="utf-8",
    )
    variant.write_text(
        '''schema = 1
contract_kind = "ip-operating-variant"
path_scope = "variant"
owner = "example"
''',
        encoding="utf-8",
    )
    component = write_component_owner(
        tmp_path,
        "example",
        filesets={
            "architecture": (
                "ip/example/configs/behavior.toml",
                "ip/example/configs/variant.toml",
            )
        },
    )
    component.write_text(
        component.read_text(encoding="utf-8")
        + '\n[variants]\ndefault = "ip/example/configs/variant.toml"\n',
        encoding="utf-8",
    )

    project = Project.open(tmp_path)
    documents = repository_checks._architecture_source_documents(project)
    variants = repository_checks._integration_variant_inventory(
        project,
        component.resolve(),
        documents,
    )

    assert set(documents) == {behavior.resolve(), variant.resolve()}
    assert variants is not None
    assert set(variants) == {variant.resolve()}
    assert variants[variant.resolve()] is documents[variant.resolve()]
    with pytest.raises(TypeError):
        documents[behavior.resolve()]["schema"] = 2


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
        repository_checks._architecture_source_documents(
            Project.open(tmp_path)
        )


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
        repository_checks._architecture_source_documents(
            Project.open(tmp_path)
        )
