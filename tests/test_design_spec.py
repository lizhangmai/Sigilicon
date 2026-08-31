from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

import sigilicon.domain.config_contracts as config_contracts
from conftest import write_component_owner
from sigilicon.domain.config_contracts import (
    RepositorySourceInventory,
    inspect_project_configuration_sources,
)
from sigilicon.domain.design import load_design_spec, resolve_design_spec
from sigilicon.domain.repository import Project


def test_design_loader_rejects_project_escape_and_symlink(
    project_factory,
) -> None:
    root, path = project_factory()
    original = path.read_text(encoding="utf-8")
    external = root.parent / "external.scs"
    external.write_text("subckt inv IN OUT VDD VSS\nends inv\n", encoding="utf-8")
    path.write_text(
        original.replace(
            'source_netlist = "circuit.scs"',
            'source_netlist = "../../../../external.scs"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="project root"):
        load_design_spec(path, project=Project.from_project_root(root))

    link = path.with_name("linked.scs")
    link.symlink_to(external)
    path.write_text(
        original.replace(
            'source_netlist = "circuit.scs"',
            'source_netlist = "linked.scs"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="project root"):
        load_design_spec(path, project=Project.from_project_root(root))


def test_cataloged_design_loader_rejects_cross_owner_source(
    project_factory,
) -> None:
    root, path = project_factory()
    write_component_owner(
        root,
        "example",
        filesets={"source": ("ip/example/inv/design.toml",)},
    )
    write_component_owner(
        root,
        "other",
        filesets={"source": ("ip/other/circuit.scs",)},
    )
    other_source = root / "ip/other/circuit.scs"
    other_source.write_text(
        "subckt inv IN OUT VDD VSS\nends inv\n",
        encoding="utf-8",
    )
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'source_netlist = "circuit.scs"',
            'source_netlist = "../../other/circuit.scs"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="owning active IP"):
        load_design_spec(path, project=Project.from_project_root(root))


def test_standalone_design_snapshot_remains_resolvable(project_factory) -> None:
    root, path = project_factory()
    project = Project.from_project_root(root)
    spec = load_design_spec(path, project=project)

    assert project.owner_for(path) is None
    assert resolve_design_spec(path, project=project, snapshot=spec) is spec


def test_design_spec_preserves_and_resolves_its_source_document(
    project_factory,
) -> None:
    root, path = project_factory()
    write_component_owner(
        root,
        "example",
        filesets={
            "source": (
                "ip/example/inv/design.toml",
                "ip/example/inv/circuit.scs",
            )
        },
    )
    project = Project.from_project_root(root)
    spec = load_design_spec(path, project=project)
    resolved_path = path.resolve()

    assert tuple(spec.source_documents) == (resolved_path,)
    assert resolve_design_spec(path, project=project, snapshot=spec) is spec
    with pytest.raises(TypeError):
        spec.source_documents[resolved_path]["schema"] = 2
    for mutable_documents in (
        {resolved_path: spec.source_documents[resolved_path]},
        MappingProxyType(
            {resolved_path: dict(spec.source_documents[resolved_path])}
        ),
    ):
        with pytest.raises(ValueError, match="mutable snapshot"):
            resolve_design_spec(
                path,
                project=project,
                snapshot=replace(spec, source_documents=mutable_documents),
            )

    drifted_document = dict(spec.source_documents[resolved_path])
    drifted_document["design"] = {
        **drifted_document["design"],
        "library": "drift",
    }
    with pytest.raises(ValueError, match="source document drift"):
        resolve_design_spec(
            path,
            project=project,
            snapshot=replace(
                spec,
                source_documents={resolved_path: drifted_document},
            ),
        )
    with pytest.raises(ValueError, match="source document drift"):
        resolve_design_spec(
            path,
            project=project,
            snapshot=replace(
                spec,
                netlist_snapshot=replace(
                    spec.netlist_snapshot,
                    source_path=resolved_path,
                ),
            ),
        )
    extra = path.with_name("extra.toml")
    extra.write_text("schema = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source document identity drift"):
        resolve_design_spec(
            path,
            project=project,
            snapshot=replace(
                spec,
                source_documents={
                    **spec.source_documents,
                    extra.resolve(): {"schema": 1},
                },
            ),
        )

    source_text = spec.source_netlist.read_text(encoding="utf-8")
    spec.source_netlist.unlink()
    with pytest.raises(ValueError, match="source document drift"):
        resolve_design_spec(path, project=project, snapshot=spec)
    spec.source_netlist.write_text(source_text, encoding="utf-8")


def test_configuration_scanner_reuses_design_source_document(
    project_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, path = project_factory()
    write_component_owner(
        root,
        "example",
        filesets={
            "source": (
                "ip/example/inv/design.toml",
                "ip/example/inv/circuit.scs",
            )
        },
    )
    project = Project.from_project_root(root)
    spec = load_design_spec(path, project=project)
    reads: list[Path] = []
    original_read_toml = config_contracts.read_toml

    def counted_read_toml(source: Path):
        if source.resolve() == path.resolve():
            reads.append(source.resolve())
        return original_read_toml(source)

    monkeypatch.setattr(config_contracts, "read_toml", counted_read_toml)

    target_catalogs = tuple(
        project.owner_target_catalog(owner)
        for owner in project.owners
        if owner.component.target_catalog is not None
    )
    sources = RepositorySourceInventory.for_project(project)
    sources.verify("design snapshot", spec.source_documents)
    report = inspect_project_configuration_sources(
        project,
        target_catalog_inventory=target_catalogs,
        sources=sources,
    )

    assert report["passed"] is True
    assert reads == []
