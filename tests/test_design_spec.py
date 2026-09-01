from __future__ import annotations

from dataclasses import replace

import pytest

from conftest import write_component_owner
from sigilicon.domain.design import load_design_spec, resolve_design_spec
from sigilicon.project import Project


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
        load_design_spec(path, project=Project.open(root))

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
        load_design_spec(path, project=Project.open(root))


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
        load_design_spec(path, project=Project.open(root))


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
    project = Project.open(root)
    spec = load_design_spec(path, project=project)
    resolved_path = path.resolve()

    assert tuple(spec.source_documents) == (resolved_path,)
    assert resolve_design_spec(path, project=project, snapshot=spec) is spec
    with pytest.raises(TypeError):
        spec.source_documents[resolved_path]["schema"] = 2
    with pytest.raises(ValueError, match="mutable snapshot"):
        resolve_design_spec(
            path,
            project=project,
            snapshot=replace(
                spec,
                source_documents={resolved_path: spec.source_documents[resolved_path]},
            ),
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
    source_text = spec.source_netlist.read_text(encoding="utf-8")
    spec.source_netlist.unlink()
    with pytest.raises(ValueError, match="source document drift"):
        resolve_design_spec(path, project=project, snapshot=spec)
    spec.source_netlist.write_text(source_text, encoding="utf-8")
