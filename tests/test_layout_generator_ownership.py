from __future__ import annotations

from pathlib import Path
import textwrap
from types import SimpleNamespace

import pytest

from conftest import write_project_context, write_test_layout_platform
from sigilicon.domain.repository import Project
from sigilicon.layout.spec import _owner_oa_assembly, load_layout_spec


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

    spec = load_layout_spec(layout, project_root=root)

    assert spec.generator_source == root / "ip/example/cell/layout_generator.py"
    assert root / "ip/shared/shared_dependency.py" in spec.generator_dependencies
    assert root / "configs/platform/testpdk/layout.toml" in spec.generator_dependencies


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
        load_layout_spec(layout, project_root=root)


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
        load_layout_spec(layout, project_root=root)


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
        load_layout_spec(layout, project_root=root)


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
        load_layout_spec(layout, project_root=root)
