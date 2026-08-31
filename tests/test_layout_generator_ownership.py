from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import textwrap
from types import SimpleNamespace

import pytest

import sigilicon.domain.config_contracts as config_contracts
from conftest import write_project_context, write_test_layout_platform
from sigilicon.domain.config_contracts import (
    RepositorySourceInventory,
    inspect_project_configuration_sources,
)
from sigilicon.domain.repository import Project
from sigilicon.layout.spec import (
    _owner_oa_assembly,
    load_layout_spec,
    resolve_layout_spec,
)


def _write_component(
    root: Path,
    name: str,
    *,
    kind: str,
    filesets: dict[str, tuple[str, ...]],
    dependencies: tuple[tuple[str, str], ...] = (),
) -> Path:
    owner_root = root / "ip" / name
    owner_root.mkdir(parents=True, exist_ok=True)
    rows = [
        "schema = 1",
        'contract_kind = "ip-component"',
        'path_scope = "owner"',
        f'owner = "{name}"',
        "",
        f'name = "{name}"',
        f'kind = "{kind}"',
    ]
    for dependency, contract in dependencies:
        rows.extend(
            [
                "",
                "[[component]]",
                f'name = "{dependency}"',
                f'contract = "{contract}"',
            ]
        )
    rows.extend(["", "[filesets]"])
    for fileset, values in filesets.items():
        rendered = ", ".join(f'"{value}"' for value in values)
        rows.append(f"{fileset} = [{rendered}]")
    contract = owner_root / "component.toml"
    contract.write_text("\n".join(rows) + "\n", encoding="utf-8")

    catalog = root / "catalogs/ip.toml"
    with catalog.open("a", encoding="utf-8") as stream:
        stream.write(
            f'\n[components.{name}]\n'
            f'contract = "ip/{name}/component.toml"\n'
            f'root = "ip/{name}"\n'
        )
    return contract


def _write_fixture(
    tmp_path: Path,
    *,
    generator_source: str = "layout_generator.py",
    generator_dependencies: tuple[str, ...] = (
        "../own_dependency.py",
        "../../shared/shared_dependency.py",
        "../../../configs/platform/testpdk/layout.toml",
    ),
    generator_modules: tuple[str, ...] = (
        "ip.example.cell.recipe",
        "ip.shared.recipe",
        "json",
    ),
) -> tuple[Path, Path]:
    root = tmp_path / "project"
    write_project_context(root)
    write_test_layout_platform(root)

    shared_root = root / "ip/shared"
    shared_root.mkdir(parents=True)
    for name in ("recipe.py", "shared_dependency.py", "shared_generator.py"):
        (shared_root / name).write_text("VALUE = 1\n", encoding="utf-8")
    (shared_root / "__init__.py").write_text("", encoding="utf-8")
    _write_component(
        root,
        "shared",
        kind="source-library",
        filesets={
            "python": (
                "ip/shared/__init__.py",
                "ip/shared/recipe.py",
                "ip/shared/shared_dependency.py",
            ),
        },
    )

    cell_root = root / "ip/example/cell"
    cell_root.mkdir(parents=True)
    (cell_root / "layout_generator.py").write_text("", encoding="utf-8")
    (cell_root / "recipe.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "ip/example/own_dependency.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    (cell_root / "circuit.scs").write_text(
        "subckt cell IN OUT\nends cell\n", encoding="utf-8"
    )
    layout = cell_root / "layout.toml"
    rendered_dependencies = ",\n".join(
        f'    "{dependency}"' for dependency in generator_dependencies
    )
    rendered_modules = ",\n".join(f'    "{module}"' for module in generator_modules)
    layout.write_text(
        textwrap.dedent(
            f'''\
            schema = 1
            contract_kind = "cell-layout"
            path_scope = "cell"
            owner = "example"

            [layout]
            library = "example"
            cell = "cell"
            view = "layout"
            generator = "test_generator"
            generator_source = "{generator_source}"
            generator_dependencies = [
            {rendered_dependencies}
            ]
            generator_modules = [
            {rendered_modules}
            ]
            source_netlist = "circuit.scs"
            pdk = "testpdk"

            [ports]
            order = ["IN", "OUT"]

            [ports.directions]
            IN = "input"
            OUT = "output"
            ''',
        ),
        encoding="utf-8",
    )
    _write_component(
        root,
        "example",
        kind="rtl-ip",
        filesets={
            "layout_generation": (
                "ip/example/cell/layout.toml",
                "ip/example/cell/layout_generator.py",
                "ip/example/cell/recipe.py",
                "ip/example/own_dependency.py",
            ),
        },
        dependencies=(
            ("shared", "ip/shared/component.toml"),
        ),
    )

    return root, layout


def test_layout_generators_allow_owned_source_library_and_exact_platform_contract(
    tmp_path: Path,
) -> None:
    root, layout = _write_fixture(tmp_path)

    spec = load_layout_spec(layout, project=Project.from_project_root(root))

    assert spec.generator_source == root / "ip/example/cell/layout_generator.py"
    assert root / "ip/shared/shared_dependency.py" in spec.generator_dependencies
    assert root / "configs/platform/testpdk/layout.toml" in spec.generator_dependencies


def test_layout_spec_preserves_and_resolves_its_source_document(
    tmp_path: Path,
) -> None:
    root, layout = _write_fixture(tmp_path)
    project = Project.from_project_root(root)
    spec = load_layout_spec(layout, project=project)
    resolved = layout.resolve()

    assert tuple(spec.source_documents) == (resolved,)
    assert resolve_layout_spec(layout, project=project, snapshot=spec) is spec
    with pytest.raises(TypeError):
        spec.source_documents[resolved]["schema"] = 2

    drifted = dict(spec.source_documents[resolved])
    drifted["layout"] = {
        **drifted["layout"],
        "cell": "drift",
    }
    with pytest.raises(ValueError, match="source document drift"):
        resolve_layout_spec(
            layout,
            project=project,
            snapshot=replace(
                spec,
                source_documents={resolved: drifted},
            ),
        )
    with pytest.raises(ValueError, match="source document drift"):
        resolve_layout_spec(
            layout,
            project=project,
            snapshot=replace(
                spec,
                source_snapshot=replace(
                    spec.source_snapshot,
                    source_path=resolved,
                ),
            ),
        )
    forged_snapshot = replace(
        spec.source_snapshot,
        text="subckt other IN OUT\nends other\n",
        interfaces={"other": ("IN", "OUT")},
    )
    with pytest.raises(ValueError, match="source document drift"):
        resolve_layout_spec(
            layout,
            project=project,
            snapshot=replace(spec, source_snapshot=forged_snapshot),
        )
    forged_full_snapshot = replace(
        spec.source_snapshots[0],
        text=spec.source_snapshots[0].text
        + "\nsubckt unrelated A B\nends unrelated\n",
        interfaces={
            **spec.source_snapshots[0].interfaces,
            "unrelated": ("A", "B"),
        },
    )
    with pytest.raises(ValueError, match="source document drift"):
        resolve_layout_spec(
            layout,
            project=project,
            snapshot=replace(
                spec,
                source_snapshots=(forged_full_snapshot,),
            ),
        )

    cross_owner_document = dict(spec.source_documents[resolved])
    cross_owner_document["layout"] = {
        **cross_owner_document["layout"],
        "generator_source": "../../shared/shared_generator.py",
    }
    with pytest.raises(ValueError, match="must belong to cataloged owner"):
        resolve_layout_spec(
            layout,
            project=project,
            snapshot=replace(
                spec,
                generator_source=(
                    root / "ip/shared/shared_generator.py"
                ).resolve(),
                source_documents={resolved: cross_owner_document},
            ),
        )
    with pytest.raises(ValueError, match="platform identity drift"):
        resolve_layout_spec(
            layout,
            project=project,
            snapshot=replace(
                spec,
                layout_pdk=replace(
                    spec.layout_pdk,
                    layout_path=(root / "ip/shared/shared_dependency.py").resolve(),
                ),
            ),
        )
    forged_layout = replace(
        spec.layout_pdk,
        layout_path=(root / "ip/shared/shared_dependency.py").resolve(),
    )
    with pytest.raises(ValueError, match="platform identity drift"):
        resolve_layout_spec(
            layout,
            project=project,
            snapshot=replace(
                spec,
                pdk=replace(spec.pdk, layout=forged_layout),
                layout_pdk=forged_layout,
            ),
        )


def test_layout_loader_rejects_undeclared_cross_owner_netlist(
    tmp_path: Path,
) -> None:
    root, layout = _write_fixture(tmp_path)
    shared_netlist = root / "ip/shared/shared.scs"
    shared_netlist.write_text("subckt shared IN OUT\nends shared\n", encoding="utf-8")
    layout.write_text(
        layout.read_text(encoding="utf-8").replace(
            'source_netlist = "circuit.scs"',
            'source_netlist = "circuit.scs"\n'
            'dependency_netlists = ["../../shared/shared.scs"]',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="crosses owner boundary"):
        load_layout_spec(layout, project=Project.from_project_root(root))


def test_layout_resolver_rejects_dependency_netlist_snapshot_drift(
    tmp_path: Path,
) -> None:
    root, layout = _write_fixture(tmp_path)
    dependency = layout.parent / "dependency.scs"
    dependency.write_text(
        "subckt helper A B\nends helper\n",
        encoding="utf-8",
    )
    layout.write_text(
        layout.read_text(encoding="utf-8").replace(
            'source_netlist = "circuit.scs"',
            'source_netlist = "circuit.scs"\n'
            'dependency_netlists = ["dependency.scs"]',
        ),
        encoding="utf-8",
    )
    project = Project.from_project_root(root)
    spec = load_layout_spec(layout, project=project)
    dependency_snapshot = spec.source_snapshots[1]

    with pytest.raises(ValueError, match="source document drift"):
        resolve_layout_spec(
            layout,
            project=project,
            snapshot=replace(
                spec,
                source_snapshots=(
                    spec.source_snapshots[0],
                    replace(
                        dependency_snapshot,
                        text="subckt forged A B\nends forged\n",
                        interfaces={"forged": ("A", "B")},
                    ),
                ),
            ),
        )


def test_configuration_scanner_reuses_layout_source_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, layout = _write_fixture(tmp_path)
    project = Project.from_project_root(root)
    spec = load_layout_spec(layout, project=project)
    reads: list[Path] = []
    original_read_toml = config_contracts.read_toml

    def counted_read_toml(path: Path):
        if path.resolve() == layout.resolve():
            reads.append(path.resolve())
        return original_read_toml(path)

    monkeypatch.setattr(config_contracts, "read_toml", counted_read_toml)
    target_catalogs = tuple(
        project.owner_target_catalog(owner)
        for owner in project.owners
        if owner.component.target_catalog is not None
    )
    sources = RepositorySourceInventory.for_project(project)
    sources.verify("layout snapshot", spec.source_documents)
    report = inspect_project_configuration_sources(
        project,
        target_catalog_inventory=target_catalogs,
        sources=sources,
    )

    assert report["passed"] is True
    assert reads == []


def test_layout_without_snapshot_discovers_the_owner_assembly(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root, layout = _write_fixture(tmp_path)
    project = Project.from_project_root(root)
    manifest = root / "ip/example/oa.toml"
    source = SimpleNamespace(
        project=project,
        manifest_path=manifest,
        pdk="testpdk",
        primitive_masters=(),
        physical_verification=None,
        cells=(SimpleNamespace(layout_specs=(layout,)),),
    )
    loaded: list[tuple[Path, Project]] = []

    monkeypatch.setattr(
        Project,
        "oa_assembly_for",
        lambda _project, _path: manifest,
    )

    def load_assembly(path: Path, *, project: Project):
        loaded.append((path, project))
        return source

    monkeypatch.setattr(
        "sigilicon.layout.spec.load_oa_library_source",
        load_assembly,
    )

    spec = load_layout_spec(layout, project=project)

    assert spec.oa_assembly_manifest == manifest
    assert loaded == [(manifest, project)]


def test_layout_snapshot_must_share_the_explicit_project() -> None:
    project = object()
    source = SimpleNamespace(project=object(), cells=())

    with pytest.raises(ValueError, match="different Project"):
        _owner_oa_assembly(project, Path("layout.toml"), oa_source=source)


def test_layout_snapshot_must_declare_the_layout_spec(tmp_path: Path) -> None:
    project = object()
    source = SimpleNamespace(project=project, cells=())
    spec_path = tmp_path / "layout.toml"

    with pytest.raises(ValueError, match="is not declared"):
        _owner_oa_assembly(project, spec_path, oa_source=source)


def test_layout_generator_source_must_belong_to_spec_owner(tmp_path: Path) -> None:
    root, layout = _write_fixture(
        tmp_path,
        generator_source="../../shared/shared_generator.py",
    )

    with pytest.raises(ValueError, match="generator_source must belong to cataloged owner"):
        load_layout_spec(layout, project=Project.from_project_root(root))


def test_layout_generator_cannot_use_sibling_owner_without_component_graph_edge(
    tmp_path: Path,
) -> None:
    root, layout = _write_fixture(
        tmp_path,
        generator_dependencies=(
            "../../other/other_dependency.py",
            "../../../configs/platform/testpdk/layout.toml",
        ),
        generator_modules=("ip.example.cell.recipe",),
    )
    other_root = root / "ip/other"
    other_root.mkdir(parents=True)
    (other_root / "other_dependency.py").write_text("VALUE = 1\n", encoding="utf-8")
    _write_component(
        root,
        "other",
        kind="rtl-ip",
        filesets={"python": ("ip/other/other_dependency.py",)},
    )

    with pytest.raises(ValueError, match="source-library"):
        load_layout_spec(layout, project=Project.from_project_root(root))


def test_layout_generator_modules_cannot_use_uncomposed_sibling_owner(
    tmp_path: Path,
) -> None:
    root, layout = _write_fixture(
        tmp_path,
        generator_dependencies=("../../../configs/platform/testpdk/layout.toml",),
        generator_modules=("ip.other.recipe",),
    )
    other_root = root / "ip/other"
    other_root.mkdir(parents=True)
    (other_root / "recipe.py").write_text("VALUE = 1\n", encoding="utf-8")
    _write_component(
        root,
        "other",
        kind="rtl-ip",
        filesets={"python": ("ip/other/recipe.py",)},
    )

    with pytest.raises(ValueError, match="generator_modules.*source-library"):
        load_layout_spec(layout, project=Project.from_project_root(root))


def test_layout_generator_rejects_unowned_project_dependency_except_selected_platform(
    tmp_path: Path,
) -> None:
    root, layout = _write_fixture(
        tmp_path,
        generator_dependencies=(
            "../../../repository_helper.py",
            "../../../configs/platform/testpdk/layout.toml",
        ),
        generator_modules=("ip.example.cell.recipe",),
    )
    (root / "repository_helper.py").write_text("VALUE = 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="cataloged owner|owner component graph"):
        load_layout_spec(layout, project=Project.from_project_root(root))
