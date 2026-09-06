from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import json
import sys
import textwrap
import pytest

from conftest import write_project_context, write_test_layout_platform
from sigilicon.project import Project
from sigilicon.layout.spec import (
    load_layout_spec,
    resolve_layout_spec,
)


@pytest.mark.parametrize("fault", [None, "source-drift", "missing-oa"])
def test_graph_owned_generator_dependencies_enter_managed_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None) -> None:
    root, layout = _write_fixture(tmp_path)
    component = root / "ip/example/component.toml"
    component.write_text(component.read_text().replace(
        'kind = "rtl-ip"', 'kind = "rtl-ip"\noperation_catalog = "operations"'
    ).replace('[sources]', '[sources]\noperations = "ip/example/operations.toml"'))
    (root / "ip/example/operations.toml").write_text('''schema = 5
contract_kind = "owner-operations"
path_scope = "owner"
owner = "example"
[operations.layout]
uses = "cadence.layout"
filesets = [{ component = "example", fileset = "layout_generation" }]
config = { owner = "example", spec = "cell/layout.toml", timeout_seconds = 30 }
''')
    with (root / "sigilicon.toml").open("a") as stream:
        stream.write(f'\n[runtime.tools]\n"runtime.python" = "{sys.executable}"\n')
    manifest = root / "sigilicon.toml"
    manifest.write_text(manifest.read_text().replace(
        "[runtime.values]", '[runtime]\ncapabilities = ["license.cadence-oa", "tool.virtuoso-bridge"]\n\n[runtime.values]'))
    (root / "ip/shared/recipe.py").write_text("VALUE = 7\n")
    (layout.parent / "layout_generator.py").write_text('''from sigilicon.layout.ir import LayoutPlan, LayoutRect
from ip.example.cell.recipe import VALUE as local_value
from ip.shared.recipe import VALUE as shared_value

def build_layout_plan(spec):
    return LayoutPlan(library=spec.library, cell=spec.cell, view=spec.view,
                      stage=spec.stage, generator=spec.generator, dbu_per_micron=spec.dbu_per_micron,
                      instances=(), rectangles=(LayoutRect("shared", "M1", "drawing", ((0, 0), (local_value, shared_value))),))
''')
    def fake_oa_write(planning, _client, *, artifacts, **_kwargs):
        artifacts.write_json("outputs", ("layout.json",), planning.plan.payload())

    monkeypatch.setattr("sigilicon.adapters.cadence.oa_client.get_client", lambda _resources: object())
    monkeypatch.setattr("sigilicon.adapters.cadence.layout_generation.generate_layout", fake_oa_write)
    if fault == "missing-oa":
        platform = root / "configs/platform/testpdk/platform.toml"
        platform.write_text(platform.read_text().replace('oa = "oa.toml"\n', ''))
    project = Project.open(root)
    assert load_layout_spec(layout, project=project).generator_dependencies
    if fault == "missing-oa":
        with pytest.raises(ValueError, match="platform OA capability"):
            project.plan("example:layout")
        return
    plan = project.plan("example:layout")
    shared = next(source for source in plan.sources if source.location == root / "ip/shared/recipe.py")
    assert shared.reference.component == "shared"
    assert shared.reference.source == "source_1"
    assert project.preflight(plan).ready
    if fault == "source-drift":
        (root / "ip/shared/recipe.py").write_text("VALUE = 11\n")
        with pytest.raises((ValueError, RuntimeError), match="drift|changed"):
            project.run(plan)
        return
    result = project.run(plan)
    assert result.status == "succeeded"
    artifact = next(artifact for step in result.outcomes for artifact in step.result.artifacts if artifact.path.name == "layout.json")
    assert json.loads(artifact.path.read_text())["rectangles"][0]["bbox_dbu"] == [[0, 0], [1, 7]]


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
    source_ids: dict[str, str] = {}
    for values in filesets.values():
        for value in values:
            source_ids.setdefault(value, f"source_{len(source_ids)}")
    rows = [
        "schema = 6",
        'contract_kind = "ip-component"',
        f'root = "ip/{name}"',
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
    rows.extend(["", "[sources]"])
    for path, source_id in source_ids.items():
        rows.append(f'{source_id} = "{path}"')
    rows.extend(["", "[filesets]"])
    for fileset, values in filesets.items():
        rendered = ", ".join(f'"{source_ids[value]}"' for value in values)
        rows.append(f"{fileset} = [{rendered}]")
    contract = owner_root / "component.toml"
    contract.write_text("\n".join(rows) + "\n", encoding="utf-8")

    catalog = root / "catalogs/ip.toml"
    with catalog.open("a", encoding="utf-8") as stream:
        stream.write(
            f'\n[components.{name}]\n'
            f'contract = "ip/{name}/component.toml"\n'
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

    spec = load_layout_spec(layout, project=Project.open(root))

    assert spec.generator_source == root / "ip/example/cell/layout_generator.py"
    assert root / "ip/shared/shared_dependency.py" in spec.generator_dependencies
    assert root / "configs/platform/testpdk/layout.toml" in spec.generator_dependencies


def test_layout_module_resolution_never_executes_owner_packages(tmp_path: Path) -> None:
    root, layout = _write_fixture(tmp_path)
    marker = tmp_path / "planning-executed-owner-code"
    (root / "ip/example/__init__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )

    load_layout_spec(layout, project=Project.open(root))

    assert not marker.exists()


def test_layout_spec_preserves_and_resolves_its_source_document(
    tmp_path: Path,
) -> None:
    root, layout = _write_fixture(tmp_path)
    project = Project.open(root)
    spec = load_layout_spec(layout, project=project)
    resolved = layout.resolve()

    assert tuple(spec.source_documents) == (resolved,)
    assert resolve_layout_spec(layout, project=project, snapshot=spec) is spec
    with pytest.raises(TypeError):
        spec.source_documents[resolved]["schema"] = 2

    original = layout.read_text(encoding="utf-8")
    layout.write_text(
        original.replace('generator = "test_generator"', 'generator = "drift_generator"'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="layout snapshot source document drift"):
        resolve_layout_spec(layout, project=project, snapshot=spec)
    layout.write_text(original, encoding="utf-8")

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
        load_layout_spec(layout, project=Project.open(root))


def test_layout_generator_source_must_belong_to_spec_owner(tmp_path: Path) -> None:
    root, layout = _write_fixture(
        tmp_path,
        generator_source="../../shared/shared_generator.py",
    )

    with pytest.raises(ValueError, match="generator_source must belong to cataloged owner"):
        load_layout_spec(layout, project=Project.open(root))


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
        load_layout_spec(layout, project=Project.open(root))


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
        load_layout_spec(layout, project=Project.open(root))
