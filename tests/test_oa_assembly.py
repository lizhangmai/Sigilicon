from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.domain.oa_library import load_oa_library_source
from sigilicon.domain.netlist import NetlistSubcircuit
from sigilicon.workflows.oa_library import (
    _instance_parameter_expectations,
    check_oa_parity,
    rebuild_oa_library,
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

    assembly = load_oa_library_source(manifest, project_root=root)

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

    assembly = load_oa_library_source(manifest, project_root=root)

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
        load_oa_library_source(manifest, project_root=root)


def test_instance_parameter_contract_elaborates_child_defaults_and_overrides(
    tmp_path: Path,
) -> None:
    child = NetlistSubcircuit(
        name="CHILD",
        ports=("A", "B"),
        parameters=(),
        statements=("parameters lch=30n w=100n",),
        source_path=tmp_path / "child.scs",
    )
    parent = NetlistSubcircuit(
        name="PARENT",
        ports=("A", "B"),
        parameters=("parent_l=120n",),
        statements=("X0 (A B) CHILD lch=parent_l",),
        source_path=tmp_path / "parent.scs",
    )

    expectations = _instance_parameter_expectations(
        parent,
        {"CHILD": child, "PARENT": parent},
        {"CHILD", "PARENT"},
    )

    assert len(expectations) == 1
    assert expectations[0].instance == "X0"
    assert expectations[0].master == "CHILD"
    assert dict(expectations[0].parameters) == {"lch": "parent_l", "w": "100n"}


def test_assembly_rejects_duplicate_global_cell_ownership(tmp_path: Path) -> None:
    root, manifest = _assembly(tmp_path)
    original = root / "ip" / "alpha" / "design" / "blocks" / "CELL_B"
    original.rename(original.with_name("CELL_A"))
    cell_manifest = original.with_name("CELL_A") / "cell.toml"
    cell_manifest.write_text(
        cell_manifest.read_text(encoding="utf-8").replace("CELL_B", "CELL_A"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="globally unique"):
        load_oa_library_source(manifest, project_root=root)


def test_assembly_rejects_unresolved_view_dependency(tmp_path: Path) -> None:
    root, manifest = _assembly(tmp_path)
    cell_manifest = (
        root / "ip" / "alpha" / "design" / "blocks" / "CELL_B" / "cell.toml"
    )
    cell_manifest.write_text(
        cell_manifest.read_text(encoding="utf-8").replace(
            "CELL_A/symbol", "MISSING/symbol"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unresolved view dependencies"):
        load_oa_library_source(manifest, project_root=root)


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
        load_oa_library_source(manifest, project_root=root)


def test_source_root_rejects_undeclared_top_level_directory(tmp_path: Path) -> None:
    root, manifest = _assembly(tmp_path)
    (root / "ip" / "alpha" / "design" / "cells" / "misc").mkdir()

    with pytest.raises(ValueError, match="directories without cell.toml"):
        load_oa_library_source(manifest, project_root=root)


def test_one_ip_contract_can_select_multiple_functional_cell_roots(
    tmp_path: Path,
) -> None:
    root, manifest = _assembly(tmp_path)

    assembly = load_oa_library_source(manifest, project_root=root)

    alpha = assembly.source_roots[0]
    assert [path.relative_to(root).as_posix() for path in alpha.cell_roots] == [
        "ip/alpha/design/cells",
        "ip/alpha/design/blocks",
    ]
    assert [cell.cell for cell in alpha.cells] == ["CELL_A", "CELL_B"]


def test_functional_verification_root_skips_rtl_only_cells(tmp_path: Path) -> None:
    root, manifest = _assembly(tmp_path)
    _cell(root, "alpha", "OA_CELL", cell_root="verification/mixed")
    rtl = root / "ip" / "alpha" / "verification" / "mixed" / "RTL_CELL"
    _write(rtl / "testbench.sv", "module RTL_CELL; endmodule\n")
    _write(
        rtl / "cell.toml",
        '''schema = 1
contract_kind = "verification-cell"
path_scope = "cell"
owner = "alpha"
cell = "RTL_CELL"
role = "rtl-testbench"
canonical_source = "testbench.sv"
dut = "DUT"
simulator = "xcelium"
''',
    )
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            'cell_roots = ["design/cells", "design/blocks"]',
            'cell_roots = ["design/cells", "design/blocks", "verification/mixed"]',
        ),
        encoding="utf-8",
    )

    assembly = load_oa_library_source(manifest, project_root=root)

    assert [cell.cell for cell in assembly.cells] == [
        "CELL_A", "CELL_B", "OA_CELL",
    ]


def test_assembly_can_aggregate_another_ip_without_changing_ownership(
    tmp_path: Path,
) -> None:
    root, manifest = _assembly(tmp_path)
    _cell(root, "beta", "CELL_C", dependency="CELL_A/symbol")
    _write(
        root / "ip" / "beta" / "configs" / "oa.toml",
        '''schema = 1
contract_kind = "oa-source-root"
path_scope = "owner"
owner = "beta"
cell_roots = ["design/cells"]
        ''',
    )
    write_component_owner(
        root,
        "beta",
        filesets={"oa_source": ("ip/beta/configs/oa.toml",)},
    )
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + 'additional_source_manifests = ["ip/beta/configs/oa.toml"]\n',
        encoding="utf-8",
    )

    assembly = load_oa_library_source(manifest, project_root=root)

    assert [source.owner for source in assembly.source_roots] == ["alpha", "beta"]
    assert [cell.cell for cell in assembly.cells] == ["CELL_A", "CELL_B", "CELL_C"]
    assert assembly.cells[-1].owner == "beta"
    assert assembly.cells[-1].source_manifest_path == (
        root / "ip" / "beta" / "configs" / "oa.toml"
    ).resolve()


def test_assembly_rejects_itself_as_an_additional_source(tmp_path: Path) -> None:
    root, manifest = _assembly(tmp_path)
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + 'additional_source_manifests = ["ip/alpha/configs/oa.toml"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must not list itself"):
        load_oa_library_source(manifest, project_root=root)


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


def test_check_accepts_git_owned_design_without_prior_state(
    tmp_path: Path, monkeypatch
) -> None:
    cell = "DESIGN"
    library_path = tmp_path / "virtuoso" / "assembled"
    for view in ("netlist", "schematic", "symbol"):
        _write(library_path / cell / view / "data.dm", f"{view}\n")
    inspection = SimpleNamespace(spec=SimpleNamespace(cell=cell))
    plan = SimpleNamespace(
        library="assembled",
        cells=(cell,),
        expected_views={cell: ("netlist", "schematic", "symbol")},
        designs=(SimpleNamespace(inspection=inspection),),
        layouts=(),
        testbenches=(),
        views=(),
        source=SimpleNamespace(project_root=tmp_path, oa_library=library_path),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.list_cells",
        lambda _client, _library: {
            "cells": [
                {"name": cell, "views": ["netlist", "schematic", "symbol"]}
            ]
        },
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.attest_oa_design",
        lambda *_args, **_kwargs: {"passed": True, "cell": cell},
    )

    report = check_oa_parity(plan, object())

    assert report["passed"] is True
    assert report["designs"] == [{"passed": True, "cell": cell}]


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


def test_check_accepts_current_layout_cache_without_prior_state(
    tmp_path: Path, monkeypatch
) -> None:
    cell = "LAYOUT_CELL"
    view = "layout"
    library_path = tmp_path / "virtuoso" / "assembled"
    view_path = library_path / cell / view
    _write(view_path / "data.dm", "canonical geometry\n")
    step = SimpleNamespace(
        spec=SimpleNamespace(
            cell=cell,
            view=view,
        ),
        plan=SimpleNamespace(),
    )
    plan = SimpleNamespace(
        library="assembled",
        cells=(cell,),
        expected_views={cell: (view,)},
        designs=(),
        layouts=(step,),
        testbenches=(),
        views=(),
        source=SimpleNamespace(project_root=tmp_path, oa_library=library_path),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.list_cells",
        lambda _client, _library: {
            "cells": [{"name": cell, "views": [view]}]
        },
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library._attest_layout_steps",
        lambda *_args, **_kwargs: None,
    )

    assert check_oa_parity(plan, object())["passed"] is True

    (view_path / "data.dm").write_text("manually moved geometry\n", encoding="utf-8")
    report = check_oa_parity(plan, object())

    assert report["passed"] is True
    assert report["stale_or_modified_views"] == {}


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


def test_check_ignores_unrelated_native_setup_files(
    tmp_path: Path, monkeypatch
) -> None:
    cell = "tb_MEASUREMENT_REFRESH"
    views = ("netlist", "schematic", "config", "measurement", "maestro")
    library_path = tmp_path / "virtuoso" / "assembled"
    for view in views:
        _write(library_path / cell / view / "data.dm", f"{view}\n")
    canonical = _write(
        tmp_path / "ip" / "alpha" / cell / "testbench.scs",
        f"subckt {cell} OUT\nends {cell}\n",
    )
    simulation = _write(
        canonical.parent / "simulation.toml",
        """schema = 3

[testbench]
library = "assembled"
cell = "tb_MEASUREMENT_REFRESH"
dut = "tb_MEASUREMENT_REFRESH"
source_view = "schematic"
simulator = "spectre"

[platform]
pdk = "testpdk"

[setup]
source = "setup.il"
config_procedure = "fixtureNativeConfig"
maestro_procedure = "fixtureNativeMaestro"
""",
    )
    setup = _write(
        canonical.parent / "setup.il",
        "procedure(fixtureNativeConfig(lib cell dut sourceView refs) t)\n"
        "procedure(fixtureNativeMaestro(session lib cell modelFile modelSection) t)\n",
    )
    step = SimpleNamespace(
        cell=cell,
        canonical_source=canonical,
        simulation=SimpleNamespace(
            path=simulation,
            native_setup=SimpleNamespace(source=setup),
        ),
    )
    plan = SimpleNamespace(
        library="assembled",
        cells=(cell,),
        expected_views={cell: views},
        designs=(),
        layouts=(),
        testbenches=(step,),
        views=(),
        source=SimpleNamespace(project_root=tmp_path, oa_library=library_path),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.list_cells",
        lambda _client, _library: {
            "cells": [{"name": cell, "views": list(views)}]
        },
    )
    report = check_oa_parity(plan, object())

    assert report["passed"] is True
    assert report["stale_or_modified_views"] == {}


def test_rebuild_recreates_selected_testbench_view_set(
    monkeypatch,
) -> None:
    cell = "tb_REFRESH"
    views = ("netlist", "schematic", "config", "measurement", "maestro")
    step = SimpleNamespace(
        cell=cell,
        simulation=SimpleNamespace(),
        canonical_source=Path("testbench.scs"),
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


def test_rebuild_refreshes_only_selected_design_cell(monkeypatch) -> None:
    def design_step(cell: str) -> SimpleNamespace:
        return SimpleNamespace(
            inspection=SimpleNamespace(spec=SimpleNamespace(cell=cell))
        )

    plan = SimpleNamespace(
        library="assembled",
        expected_views={
            "CELL_A": ("netlist", "schematic", "symbol"),
            "CELL_B": ("netlist", "schematic", "symbol"),
        },
        designs=(design_step("CELL_A"), design_step("CELL_B")),
        layouts=(),
        testbenches=(),
        views=(),
        source=SimpleNamespace(project_root=Path(".")),
    )
    client = SimpleNamespace(
        library=SimpleNamespace(list=lambda timeout: ["assembled"])
    )
    rebuilt: list[str] = []
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.synchronize_design",
        lambda inspection, *_args, **_kwargs: rebuilt.append(inspection.spec.cell),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.list_cells",
        lambda *_args, **_kwargs: {
            "cells": [
                {
                    "name": cell,
                    "views": ["netlist", "schematic", "symbol"],
                }
                for cell in ("CELL_A", "CELL_B")
            ]
        },
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.check_oa_parity",
        lambda *_args, **_kwargs: {"passed": True},
    )

    assert rebuild_oa_library(plan, client, cell="CELL_B") == {"passed": True}
    assert rebuilt == ["CELL_B"]


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
