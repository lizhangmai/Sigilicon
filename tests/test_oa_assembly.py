from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from sigilicon.domain.oa_library import (
    load_oa_library_source,
    resolve_oa_library_source,
)
from sigilicon.domain.netlist import NetlistSnapshot
from sigilicon.flow import SourceMember
from sigilicon.domain.repository import Project
from sigilicon.domain.source import load_text_source_snapshot
from sigilicon.workflows.oa_library import (
    OALibraryRebuildPlan,
    check_oa_parity,
    rebuild_oa_library,
    validate_oa_plan_source_members,
)

from conftest import write_component_owner


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _cell(
    root: Path,
    owner: str,
    name: str,
    *,
    cell_root: str = "design/cells",
    dependency: str = "",
) -> None:
    directory = root / "ip" / owner / cell_root / name
    _write(directory / "circuit.scs", f"subckt {name} A B\nends {name}\n")
    dependencies = f'"{dependency}"' if dependency else ""
    _write(
        directory / "cell.toml",
        f'''schema = 1
contract_kind = "oa-cell"
path_scope = "cell"
owner = "{owner}"
cell = "{name}"
role = "design"
canonical_source = "circuit.scs"
views = [
  {{ name = "netlist", kind = "spectre_netlist", source = "circuit.scs", dependencies = [{dependencies}] }},
  {{ name = "schematic", kind = "schematic", source = "design.toml", dependencies = ["{name}/netlist"] }},
  {{ name = "symbol", kind = "symbol", source = "design.toml", dependencies = ["{name}/schematic"] }},
]
''',
    )
    _write(directory / "design.toml", "[design]\n")


def _assembly(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "virtuoso").mkdir()
    (tmp_path / "src" / "flow" / "layout").mkdir(parents=True)
    _cell(tmp_path, "alpha", "CELL_A")
    _cell(
        tmp_path,
        "alpha",
        "CELL_B",
        cell_root="design/blocks",
        dependency="CELL_A/symbol",
    )
    _write(
        tmp_path / "ip" / "alpha" / "configs" / "physical_verification.toml",
        '''schema = 1
contract_kind = "physical-verification-policy"
path_scope = "owner"
owner = "alpha"

[drc]
configuration_warnings = []
waiver_layers = []

[drc.disabled_defines]
''',
    )
    manifest = _write(
        tmp_path / "ip" / "alpha" / "configs" / "oa.toml",
        '''schema = 1
contract_kind = "oa-assembly"
path_scope = "owner"
owner = "alpha"
name = "assembled"
pdk = "testpdk"
workspace_template = "virtuoso"
oa_library = "virtuoso/assembled"
primitive_masters = ["nch"]
physical_verification = "ip/alpha/configs/physical_verification.toml"
cell_roots = ["design/cells", "design/blocks"]
        ''',
    )
    write_component_owner(
        tmp_path,
        "alpha",
        filesets={"oa_source": ("ip/alpha/configs/oa.toml",)},
    )
    return tmp_path, manifest


def test_ip_oa_contract_can_own_sources_and_assemble_the_library(
    tmp_path: Path,
) -> None:
    root, manifest = _assembly(tmp_path)

    assembly = load_oa_library_source(manifest, project=Project.from_project_root(root))

    assert assembly.manifest_path == manifest.resolve()
    assert assembly.primitive_masters == ("nch",)
    assert assembly.physical_verification.path == (
        root / "ip/alpha/configs/physical_verification.toml"
    ).resolve()
    assert [source.owner for source in assembly.source_roots] == ["alpha"]
    assert [cell.cell for cell in assembly.cells] == ["CELL_A", "CELL_B"]
    assert assembly.cells[0].source_manifest_path == manifest.resolve()
    assert [view.name for view in assembly.cells[1].views] == [
        "netlist",
        "schematic",
        "symbol",
    ]


def test_oa_assembly_rejects_cell_directory_symlink_escape(
    tmp_path: Path,
) -> None:
    root, manifest = _assembly(tmp_path)
    external = root / "external/CELL_ESCAPE"
    external.mkdir(parents=True)
    (root / "ip/alpha/design/cells/CELL_ESCAPE").symlink_to(
        external,
        target_is_directory=True,
    )

    with pytest.raises(ValueError, match="escapes its declared cell root"):
        load_oa_library_source(manifest, project=Project.from_project_root(root))


def test_oa_assembly_rejects_cell_manifest_symlink_escape(
    tmp_path: Path,
) -> None:
    root, manifest = _assembly(tmp_path)
    external_manifest = _write(
        root / "external/cell.toml",
        "schema = 1\n",
    )
    cell_directory = root / "ip/alpha/design/cells/LINKED_MANIFEST"
    cell_directory.mkdir()
    (cell_directory / "cell.toml").symlink_to(external_manifest)

    with pytest.raises(ValueError, match="manifest escapes"):
        load_oa_library_source(manifest, project=Project.from_project_root(root))


def test_oa_snapshot_rejects_forged_source_root_membership(
    tmp_path: Path,
) -> None:
    root, manifest = _assembly(tmp_path)
    project = Project.from_project_root(root)
    assembly = load_oa_library_source(manifest, project=project)
    source_root = assembly.source_roots[0]
    manifest_document = dict(
        source_root.source_documents[source_root.manifest_path]
    )
    manifest_document["cell_roots"] = (
        "design/cells",
        "design/cells/",
        "design/blocks",
    )
    duplicate_documents = dict(source_root.source_documents)
    duplicate_documents[source_root.manifest_path] = manifest_document
    duplicate_root = replace(
        source_root,
        cell_roots=(
            source_root.cell_roots[0],
            source_root.cell_roots[0],
            *source_root.cell_roots[1:],
        ),
        source_documents=duplicate_documents,
    )
    duplicate_library_documents = dict(assembly.source_documents)
    duplicate_library_documents[source_root.manifest_path] = manifest_document
    duplicate_source = replace(
        assembly,
        source_roots=(duplicate_root,),
        source_documents=duplicate_library_documents,
    )

    with pytest.raises(ValueError, match="duplicate cell roots"):
        resolve_oa_library_source(
            manifest,
            project=project,
            snapshot=duplicate_source,
        )

    verification_documents = dict(source_root.source_documents)
    for source_path in tuple(verification_documents):
        if source_path == source_root.manifest_path:
            continue
        document = dict(verification_documents[source_path])
        document["contract_kind"] = "verification-cell"
        verification_documents[source_path] = document
    empty_root = replace(
        source_root,
        cells=(),
        source_documents=verification_documents,
    )
    empty_library_documents = dict(assembly.source_documents)
    empty_library_documents.update(verification_documents)
    empty_source = replace(
        assembly,
        source_roots=(empty_root,),
        cells=(),
        source_documents=empty_library_documents,
    )

    with pytest.raises(ValueError, match="source root declares no OA cells"):
        resolve_oa_library_source(
            manifest,
            project=project,
            snapshot=empty_source,
        )

    (root / "ip/alpha/design/cells/CELL_B_ALIAS").symlink_to(
        root / "ip/alpha/design/blocks/CELL_B",
        target_is_directory=True,
    )
    with pytest.raises(ValueError, match="cell directory escapes its cell root"):
        resolve_oa_library_source(
            manifest,
            project=project,
            snapshot=assembly,
        )
def test_native_oa_source_members_must_match_retained_netlist_snapshot(
    tmp_path: Path,
) -> None:
    source = _write(tmp_path / "cell.scs", "subckt CELL_A A B\nends CELL_A\n")
    snapshot = NetlistSnapshot(
        source,
        source.read_text(encoding="utf-8"),
        MappingProxyType({"CELL_A": ("A", "B")}),
    )
    plan = OALibraryRebuildPlan(
        source=SimpleNamespace(source_documents={}, cells=(), source_roots=()),
        library="assembled",
        cells=(),
        designs=(),
        layouts=(),
        testbenches=(),
        views=(),
        expected_views={},
        netlist_snapshots=MappingProxyType({source: snapshot}),
    )
    member = SourceMember(
        "cell.scs",
        tmp_path,
        "subckt CELL_A A B C\nends CELL_A\n",
        False,
        source,
    )

    with pytest.raises(ValueError, match="source snapshot drift"):
        validate_oa_plan_source_members(plan, (member,))


def test_pre_layout_oa_assembly_can_omit_physical_verification(
    tmp_path: Path,
) -> None:
    root, manifest = _assembly(tmp_path)
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            'physical_verification = "ip/alpha/configs/physical_verification.toml"\n',
            "",
        ),
        encoding="utf-8",
    )

    assembly = load_oa_library_source(manifest, project=Project.from_project_root(root))

    assert assembly.physical_verification is None


def test_oa_assembly_rejects_unknown_physical_verification_policy_fields(
    tmp_path: Path,
) -> None:
    root, manifest = _assembly(tmp_path)
    policy = root / "ip/alpha/configs/physical_verification.toml"
    policy.write_text(
        policy.read_text(encoding="utf-8").replace(
            "\n[drc]\n", "\naccepted_violations = []\n\n[drc]\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported fields.*accepted_violations"):
        load_oa_library_source(manifest, project=Project.from_project_root(root))


def test_oa_assembly_rejects_cross_owner_physical_policy(
    tmp_path: Path,
) -> None:
    root, manifest = _assembly(tmp_path)
    _write(
        root / "ip/beta/configs/physical_verification.toml",
        '''schema = 1
contract_kind = "physical-verification-policy"
path_scope = "owner"
owner = "alpha"

[drc]
configuration_warnings = []
waiver_layers = []
[drc.disabled_defines]
''',
    )
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "ip/alpha/configs/physical_verification.toml",
            "ip/beta/configs/physical_verification.toml",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="inside the assembly owner"):
        load_oa_library_source(manifest, project=Project.from_project_root(root))


def test_assembly_rejects_unmanaged_owner(tmp_path: Path) -> None:
    root, _manifest = _assembly(tmp_path)
    unmanaged = root / "ip" / "unmanaged"
    manifest = _write(
        unmanaged / "configs" / "oa.toml",
        '''schema = 1
contract_kind = "oa-assembly"
path_scope = "owner"
owner = "unmanaged"
name = "assembled"
pdk = "testpdk"
workspace_template = "virtuoso"
oa_library = "virtuoso/assembled"
cell_roots = ["design/cells"]
''',
    )
    _cell(root, "unmanaged", "OLD")

    with pytest.raises(ValueError, match="no cataloged owner"):
        load_oa_library_source(manifest, project=Project.from_project_root(root))


def test_read_only_check_classifies_missing_and_extra_objects(monkeypatch) -> None:
    plan = SimpleNamespace(
        library="assembled",
        cells=("EXPECTED",),
        expected_views={"EXPECTED": ("schematic", "symbol")},
        designs=(),
        layouts=(),
        testbenches=(),
        views=(),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.list_cells",
        lambda _client, _library: {
            "cells": [
                {"name": "EXPECTED", "views": ["schematic"]},
                {"name": "EXTRA", "views": ["schematic"]},
            ]
        },
    )

    report = check_oa_parity(plan, object())

    assert report["passed"] is False
    assert report["extra_cells"] == ["EXTRA"]
    assert report["missing_views"] == {"EXPECTED": ["symbol"]}


def test_testbench_check_includes_transitive_dependency_materialization(
    tmp_path: Path, monkeypatch
) -> None:
    cells = ("CHILD", "DUT", "STIMULUS", "tb_NATIVE")
    library_path = tmp_path / "virtuoso" / "assembled"
    expected_views = {
        "CHILD": ("netlist", "schematic", "symbol"),
        "DUT": ("netlist", "schematic", "symbol"),
        "STIMULUS": ("systemVerilog",),
        "tb_NATIVE": ("netlist", "schematic", "config", "measurement", "maestro"),
    }
    for cell, views in expected_views.items():
        for view in views:
            _write(library_path / cell / view / "data.dm", f"{cell}/{view}\n")
    design_steps = tuple(
        SimpleNamespace(
            inspection=SimpleNamespace(
                spec=SimpleNamespace(cell=cell)
            ),
            dependencies=dependencies,
            instance_parameters=(),
        )
        for cell, dependencies in (("CHILD", ()), ("DUT", ("CHILD",)))
    )
    testbench_step = SimpleNamespace(
        cell="tb_NATIVE",
        dependencies=("DUT", "STIMULUS"),
        simulation=object(),
        canonical_source=tmp_path / "testbench.scs",
    )
    plan = SimpleNamespace(
        library="assembled",
        cells=cells,
        expected_views=expected_views,
        designs=design_steps,
        layouts=(),
        testbenches=(testbench_step,),
        views=(),
        source=SimpleNamespace(project_root=tmp_path, oa_library=library_path),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.list_cells",
        lambda _client, _library: {
            "cells": [
                *(
                    {"name": cell, "views": list(views)}
                    for cell, views in expected_views.items()
                ),
                {"name": "UNRELATED", "views": ["schematic"]},
            ]
        },
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.attest_oa_design",
        lambda inspection, *_args, **_kwargs: {
            "passed": True,
            "cell": inspection.spec.cell,
        },
    )

    report = check_oa_parity(plan, object(), testbench="tb_NATIVE")

    assert report["passed"] is True
    assert report["dependency_cells"] == list(cells)
    assert report["cell_count"] == len(cells)
    assert [item["cell"] for item in report["designs"]] == ["CHILD", "DUT"]
    assert report["extra_cells"] == []


def test_check_reports_current_text_view_without_duplicate_content_identity(
    tmp_path: Path, monkeypatch
) -> None:
    cell = "MODEL"
    view = "systemVerilog"
    library_path = tmp_path / "virtuoso" / "assembled"
    view_path = library_path / cell / view
    _write(view_path / "data.dm", "canonical OA payload\n")
    source = _write(tmp_path / "ip" / "alpha" / "MODEL.sv", "module MODEL; endmodule\n")
    step = SimpleNamespace(
        cell=cell,
        view=SimpleNamespace(
            name=view,
            kind="system_verilog",
            source=source,
        ),
    )
    plan = SimpleNamespace(
        library="assembled",
        cells=(cell,),
        expected_views={cell: (view,)},
        designs=(),
        layouts=(),
        testbenches=(),
        views=(step,),
        source=SimpleNamespace(project_root=tmp_path, oa_library=library_path),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.list_cells",
        lambda _client, _library: {"cells": [{"name": cell, "views": [view]}]},
    )

    report = check_oa_parity(plan, object())

    assert report["passed"] is True, report
    assert report["text_views"] == [
        {
            "cell": cell,
            "view": view,
        }
    ]


def test_check_checks_present_views_of_an_incomplete_testbench(
    tmp_path: Path, monkeypatch
) -> None:
    cell = "tb_PARTIAL"
    library_path = tmp_path / "virtuoso" / "assembled"
    present = ("netlist", "schematic", "config", "maestro")
    for view in present:
        _write(library_path / cell / view / "data.dm", f"{view}\n")
    step = SimpleNamespace(
        cell=cell,
        simulation=object(),
        canonical_source=tmp_path / "testbench.scs",
    )
    plan = SimpleNamespace(
        library="assembled",
        cells=(cell,),
        expected_views={cell: (*present, "measurement")},
        designs=(),
        layouts=(),
        testbenches=(step,),
        views=(),
        source=SimpleNamespace(project_root=tmp_path, oa_library=library_path),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.list_cells",
        lambda _client, _library: {
            "cells": [{"name": cell, "views": list(present)}]
        },
    )
    report = check_oa_parity(plan, object())

    assert report["missing_views"] == {cell: ["measurement"]}
    assert report["stale_or_modified_views"] == {}
    assert report["testbenches"][0]["complete"] is False

    (library_path / cell / "maestro" / "data.dm").write_text(
        "partial automatic rewrite\n", encoding="utf-8"
    )
    changed = check_oa_parity(plan, object())

    assert changed["stale_or_modified_views"] == {}


def test_rebuild_recreates_selected_testbench_view_set(
    monkeypatch,
) -> None:
    cell = "tb_REFRESH"
    views = ("netlist", "schematic", "config", "measurement", "maestro")
    step = SimpleNamespace(
        cell=cell,
        simulation=SimpleNamespace(),
        source_snapshot=object(),
    )
    plan = SimpleNamespace(
        library="assembled",
        cells=(cell,),
        expected_views={cell: views},
        designs=(),
        layouts=(),
        testbenches=(step,),
        views=(),
        source=SimpleNamespace(project_root=Path(".")),
    )
    client = SimpleNamespace(
        library=SimpleNamespace(list=lambda timeout: ["assembled"])
    )
    rebuilt: list[dict[str, object]] = []
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.sync_oa_testbench",
        lambda *_args, **kwargs: rebuilt.append(kwargs),
    )
    monkeypatch.setattr("sigilicon.workflows.oa_library.check_oa_parity", lambda *_args, **_kwargs: {"passed": True})

    assert rebuild_oa_library(plan, client, testbench=cell) == {"passed": True}
    assert rebuilt == [{"overwrite": True, "timeout": 300}]


def test_rebuild_consumes_planned_text_and_layout_inputs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source_path = _write(tmp_path / "model.va", "module MODEL; endmodule\n")
    source_snapshot = load_text_source_snapshot(source_path)
    source_path.write_text("module DRIFTED; endmodule\n", encoding="utf-8")
    layout_planning = object()
    plan = SimpleNamespace(
        library="assembled",
        expected_views={"MODEL": ("veriloga", "layout")},
        designs=(),
        layouts=(
            SimpleNamespace(
                spec=SimpleNamespace(cell="MODEL", view="layout"),
                planning=layout_planning,
            ),
        ),
        testbenches=(),
        views=(
            SimpleNamespace(
                cell="MODEL",
                view=SimpleNamespace(name="veriloga", kind="veriloga"),
                source_snapshot=source_snapshot,
            ),
        ),
        source=SimpleNamespace(project=object(), project_root=tmp_path),
    )
    client = SimpleNamespace(
        library=SimpleNamespace(list=lambda timeout: ["assembled"])
    )
    text_inputs: list[object] = []
    layout_inputs: list[object] = []
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.list_cells",
        lambda *_args, **_kwargs: {
            "cells": [{"name": "MODEL", "views": []}]
        },
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.cell_view_exists",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.sync_oa_text_view",
        lambda *_args, **kwargs: text_inputs.append(kwargs["source"]),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.generate_layout",
        lambda planning, *_args, **_kwargs: layout_inputs.append(planning),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.check_oa_parity",
        lambda *_args, **_kwargs: {"passed": True},
    )

    assert rebuild_oa_library(plan, client, cell="MODEL") == {"passed": True}
    assert [item.text for item in text_inputs] == ["module MODEL; endmodule\n"]
    assert layout_inputs == [layout_planning]


def test_full_rebuild_discards_undeclared_cache_without_history_gate(monkeypatch) -> None:
    plan = SimpleNamespace(
        library="assembled",
        views=(),
        designs=(),
        layouts=(),
        testbenches=(),
        expected_views={},
    )
    client = SimpleNamespace(library=SimpleNamespace(list=lambda timeout: ["assembled"]))
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.list_cells",
        lambda *_args, **_kwargs: {
            "cells": [{"name": "MANUAL", "views": ["schematic"]}]
        },
    )
    discarded: list[dict[str, object]] = []
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library._discard_undeclared_oa_cache",
        lambda *_args, **kwargs: discarded.append(kwargs),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.check_oa_parity",
        lambda *_args, **_kwargs: {"passed": True},
    )

    assert rebuild_oa_library(plan, client) == {"passed": True}
    assert discarded == [{"timeout": 300}]
