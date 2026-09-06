from __future__ import annotations

from pathlib import Path
from dataclasses import replace
from contextlib import nullcontext
import sys
from types import SimpleNamespace

import pytest

import sigilicon.adapters.cadence.oa_check as oa_check
from conftest import managed_execution_workspace, write_component_owner, write_test_platform, write_test_layout_platform
from sigilicon.project import Project
from sigilicon.execution._resources import Resources
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation
from sigilicon.adapters.cadence.oa_check import check_oa_library
from sigilicon.adapters.cadence.oa_library import (
    OALibraryRebuildPlan,
    TestbenchRebuildStep as _TestbenchRebuildStep,
    ViewRebuildStep,
    plan_oa_library_rebuild,
)
from sigilicon.adapters.cadence.oa_library_execution import rebuild_oa_library
from sigilicon.domain.platform import load_platform
from sigilicon.domain.netlist import load_netlist_snapshot
from sigilicon.domain.oa_library import OACellViewSource
from sigilicon.domain.source import load_text_source_snapshot
from sigilicon.adapters.cadence.oa_library_execution import check_oa_parity


OA_RESOURCES = Resources()


def test_read_only_check_workspace_does_not_create_flow_lock(
    monkeypatch, tmp_path: Path
) -> None:
    root = tmp_path / "virtuoso"
    root.mkdir()
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.virtuoso_workdir", lambda _client: root
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.assert_no_active_maestro_sessions",
        lambda *_args, **_kwargs: None,
    )

    with workspace_operation(
        object(),
        root,
        "check-read-only",
        policy=OperationPolicy.READ_ONLY,
        acquire_flow_lock=False,
        record_incident=False,
    ):
        pass

    assert not (root / ".flow-operation.lock").exists()


@pytest.mark.parametrize("operation", ("check", "rebuild"))
def test_oa_assembly_requires_managed_layout_ir_before_live_access(tmp_path: Path, operation: str) -> None:
    plan = _typed_oa_plan(tmp_path, with_layout=True)
    with pytest.raises(ValueError, match="requires managed LayoutIR"):
        if operation == "check":
            check_oa_library(plan, client=object())
        else:
            rebuild_oa_library(
                plan, object(), source_paths={}, resource_paths={}, resources=OA_RESOURCES,
                artifacts=managed_execution_workspace(tmp_path),
            )


def _typed_oa_plan(root: Path, *, with_layout: bool = False) -> OALibraryRebuildPlan:
    if with_layout:
        write_test_layout_platform(root)
    else:
        write_test_platform(root)
    workspace = root / "virtuoso"
    workspace.mkdir(exist_ok=True)
    cell = root / "ip/fixture/cells/MODEL"
    cell.mkdir(parents=True)
    (cell / "circuit.scs").write_text(
        "subckt MODEL IN OUT VDD VSS\nends MODEL\n",
        encoding="utf-8",
    )
    (cell / "design.toml").write_text(
        '''schema = 1
contract_kind = "cell-design"
path_scope = "cell"
owner = "fixture"

[design]
library = "fixture_lib"
cell = "MODEL"
source_netlist = "circuit.scs"
pdk = "testpdk"

[ports]
inputs = ["IN"]
outputs = ["OUT"]
supplies = ["VDD", "VSS"]
order = ["IN", "OUT", "VDD", "VSS"]

[ports.directions]
IN = "input"
OUT = "output"
VDD = "inputOutput"
VSS = "inputOutput"
''',
        encoding="utf-8",
    )
    (cell / "cell.toml").write_text(
        '''schema = 1
contract_kind = "oa-cell"
path_scope = "cell"
owner = "fixture"
cell = "MODEL"
role = "design"
canonical_source = "circuit.scs"
views = [
  { name = "netlist", kind = "spectre_netlist", source = "circuit.scs" },
  { name = "schematic", kind = "schematic", source = "design.toml", dependencies = ["MODEL/netlist"] },
  { name = "symbol", kind = "symbol", source = "design.toml", dependencies = ["MODEL/schematic"] },
]
''',
        encoding="utf-8",
    )
    layout_sources = ()
    if with_layout:
        (cell / "layout_generator.py").write_text("def build_layout_plan(spec): raise AssertionError('planning must not run a generator')\n")
        (cell / "layout.toml").write_text('''schema = 1
contract_kind = "cell-layout"
path_scope = "cell"
owner = "fixture"
[layout]
library = "fixture_lib"
cell = "MODEL"
view = "layout"
generator = "test_generator"
generator_source = "layout_generator.py"
source_netlist = "circuit.scs"
pdk = "testpdk"
[ports]
order = ["IN", "OUT", "VDD", "VSS"]
[ports.directions]
IN = "input"
OUT = "output"
VDD = "inputOutput"
VSS = "inputOutput"
''')
        cell_manifest = cell / "cell.toml"
        cell_manifest.write_text(cell_manifest.read_text().replace("views = [", 'views = [\n  { name = "layout", kind = "layout", source = "layout.toml" },'))
        layout_sources = ("ip/fixture/cells/MODEL/layout.toml", "ip/fixture/cells/MODEL/layout_generator.py")
    manifest = root / "ip/fixture/configs/oa.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        '''schema = 1
contract_kind = "oa-assembly"
path_scope = "owner"
owner = "fixture"
name = "fixture_lib"
pdk = "testpdk"
cell_roots = ["cells"]
primitive_masters = []
''',
        encoding="utf-8",
    )
    write_component_owner(
        root,
        "fixture",
        filesets={
            "oa_source": (
                "ip/fixture/configs/oa.toml",
                "ip/fixture/cells/MODEL/cell.toml",
                "ip/fixture/cells/MODEL/circuit.scs",
                "ip/fixture/cells/MODEL/design.toml",
                *layout_sources,
            )
        },
    )
    plan = plan_oa_library_rebuild(manifest, project=Project.open(root))
    plan.source.oa_library.mkdir()
    return plan


@pytest.mark.parametrize("state", ("local", "temporary-link", "modified"))
def test_oa_parity_checks_native_master_content_and_persistence(
    monkeypatch, tmp_path: Path, state: str,
) -> None:
    plan = _typed_oa_plan(tmp_path)
    source = tmp_path / "ip/fixture/cells/MODEL/circuit.scs"
    snapshot = load_text_source_snapshot(source)
    view = OACellViewSource("spectre", "spectre_model", source, ())
    plan = replace(
        plan, designs=(), expected_views={"MODEL": ("spectre",)},
        views=(ViewRebuildStep("MODEL", "fixture", view, snapshot),),
    )
    directory = plan.source.oa_library / "MODEL/spectre"
    directory.mkdir(parents=True)
    (directory / "master.tag").write_text("-- Master.tag File, Rev:1.0\nspectre.scs\n")
    master = directory / "spectre.scs"
    if state == "temporary-link":
        master.symlink_to(tmp_path / "deleted-tool-input.scs")
    else:
        master.write_text(snapshot.text if state == "local" else "modified model\n")
    monkeypatch.setattr(
        "sigilicon.adapters.cadence.oa_library_execution.list_cells",
        lambda *a, **k: {"cells": [{"name": "MODEL", "views": ["spectre"]}]},
    )

    result = check_oa_parity(plan, object())

    assert result["passed"] is (state == "local")
    if state != "local":
        assert "MODEL/spectre" in result["stale_or_modified_views"]


@pytest.mark.parametrize(
    ("live_vdd_net", "live_model_vdd"),
    (
        ("VDD", "VDD"),
        ("STALE_VDD", "VDD"),
        ("VDD", "STALE_VDD"),
    ),
)
def test_oa_parity_checks_testbench_schematic_connectivity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    live_vdd_net: str,
    live_model_vdd: str,
) -> None:
    plan = _typed_oa_plan(tmp_path)
    source = tmp_path / "ip/fixture/cells/TB/testbench.scs"
    source.parent.mkdir(parents=True)
    source.write_text(
        "subckt TB IN OUT VDD VSS\n"
        "VDD_SRC (VDD 0) vsource dc=1\n"
        "XMODEL (IN OUT VDD VSS) MODEL\n"
        "ends TB\n",
        encoding="utf-8",
    )
    step = _TestbenchRebuildStep(
        cell="TB",
        source_snapshot=load_netlist_snapshot(source),
        dependencies=(),
        simulation=SimpleNamespace(),
    )
    plan = replace(
        plan,
        cells=("TB",),
        designs=(),
        layouts=(),
        views=(),
        testbenches=(step,),
        expected_views={
            "TB": ("netlist", "schematic", "config", "measurement", "maestro")
        },
    )
    monkeypatch.setattr(
        "sigilicon.adapters.cadence.oa_library_execution.list_cells",
        lambda *args, **kwargs: {
            "cells": [
                {
                    "name": "TB",
                    "views": [
                        "netlist",
                        "schematic",
                        "config",
                        "measurement",
                        "maestro",
                    ],
                }
            ]
        },
    )
    monkeypatch.setattr(
        "sigilicon.adapters.cadence.oa_testbench_schematic.read_schematic",
        lambda *args, **kwargs: {
            "instances": [
                {
                    "name": "gnd0",
                    "cell": "gnd",
                    "terms": {"GND": "gnd!"},
                    "params": {},
                },
                {
                    "name": "VDD_SRC",
                    "cell": "vsource",
                    "terms": {"PLUS": live_vdd_net, "MINUS": "0"},
                    "params": {"vdc": "1", "srcType": "dc"},
                },
                {
                    "name": "XMODEL",
                    "cell": "MODEL",
                    "terms": {
                        "IN": "IN",
                        "OUT": "OUT",
                        "VDD": live_model_vdd,
                        "VSS": "VSS",
                    },
                    "params": {},
                },
            ],
            "pins": {"IN": {}, "OUT": {}, "VDD": {}, "VSS": {}},
        },
    )
    operation = SimpleNamespace(
        view_lease=lambda *args, **kwargs: nullcontext(),
    )

    result = check_oa_parity(plan, object(), operation=operation)

    expected_passed = live_vdd_net == "VDD" and live_model_vdd == "VDD"
    assert result["passed"] is expected_passed
    assert result["testbench_schematic"]["TB"]["passed"] is expected_passed
    if not expected_passed:
        assert "TB/schematic+netlist" in result["stale_or_modified_views"]


@pytest.mark.parametrize(
    ("mutation", "expected_passed"),
    (
        ("none", True),
        ("dc-value", False),
        ("pwl-value", False),
        ("pwl-time", False),
        ("terminal-order", False),
        ("dc-type", False),
        ("voltage-gain", False),
        ("transconductance", False),
        ("pdk-terminal-order", False),
    ),
)
def test_oa_parity_checks_testbench_source_parameters_and_terminal_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
    expected_passed: bool,
) -> None:
    plan = _typed_oa_plan(tmp_path)
    source = tmp_path / "ip/fixture/cells/TB/testbench.scs"
    source.parent.mkdir(parents=True)
    source.write_text(
        "subckt TB IN VDD\n"
        "VDD_SRC (VDD 0) vsource dc=0.9\n"
        "VSTEP (IN 0) vsource type=pwl wave=[0 0 1n 0.9]\n"
        "EGAIN (IN 0 VDD 0) vcvs gain=2\n"
        "GGAIN (IN 0 VDD 0) vccs gm=1m\n"
        "CAP (IN 0) pdk_cap c=1f\n"
        "ends TB\n",
        encoding="utf-8",
    )
    step = _TestbenchRebuildStep(
        cell="TB",
        source_snapshot=load_netlist_snapshot(source),
        dependencies=(),
        simulation=SimpleNamespace(),
    )
    plan = replace(
        plan,
        cells=("TB",),
        designs=(),
        layouts=(),
        views=(),
        testbenches=(step,),
        platform_documents={
            source.parent / "pdk.toml": {"primitive_subcircuits": {"pdk_cap": ("TOP", "BOTTOM")}}
        },
        expected_views={
            "TB": ("netlist", "schematic", "config", "measurement", "maestro")
        },
    )
    monkeypatch.setattr(
        "sigilicon.adapters.cadence.oa_library_execution.list_cells",
        lambda *args, **kwargs: {
            "cells": [
                {
                    "name": "TB",
                    "views": [
                        "netlist",
                        "schematic",
                        "config",
                        "measurement",
                        "maestro",
                    ],
                }
            ]
        },
    )
    vdd_terms = {"PLUS": "VDD", "MINUS": "0"}
    if mutation == "terminal-order":
        vdd_terms = {"PLUS": "0", "MINUS": "VDD"}
    vdd_params = {
        "vdc": "900m" if mutation != "dc-value" else "800m",
        "srcType": "dc" if mutation != "dc-type" else "pulse",
    }
    pwl_params = {
        "srcType": "pwl",
        "pwlEntryMethod": "Voltage/Time points",
        "tvpairs": "2",
        "t1": "0",
        "v1": "0",
        "t2": "1n" if mutation != "pwl-time" else "2n",
        "v2": "900m" if mutation != "pwl-value" else "800m",
    }
    monkeypatch.setattr(
        "sigilicon.adapters.cadence.oa_testbench_schematic.read_schematic",
        lambda *args, **kwargs: {
            "instances": [
                {
                    "name": "VDD_SRC",
                    "cell": "vsource",
                    "terms": vdd_terms,
                    "params": vdd_params,
                },
                {
                    "name": "VSTEP",
                    "cell": "vsource",
                    "terms": {"PLUS": "IN", "MINUS": "0"},
                    "params": pwl_params,
                },
                {
                    "name": "EGAIN", "cell": "vcvs",
                    "terms": {"PLUS": "IN", "MINUS": "0", "NC+": "VDD", "NC-": "0"},
                    "params": {"egain": "2" if mutation != "voltage-gain" else "3"},
                },
                {
                    "name": "GGAIN", "cell": "vccs",
                    "terms": {"PLUS": "IN", "MINUS": "0", "NC+": "VDD", "NC-": "0"},
                    "params": {"ggain": "0.001" if mutation != "transconductance" else "0.002"},
                },
                {
                    "name": "CAP", "cell": "pdk_cap",
                    "terms": (
                        {"TOP": "IN", "BOTTOM": "0"} if mutation != "pdk-terminal-order"
                        else {"TOP": "0", "BOTTOM": "IN"}
                    ),
                    "params": {"c": "1f"},
                },
            ],
            "pins": {"IN": {}, "VDD": {}},
        },
    )
    operation = SimpleNamespace(
        view_lease=lambda *args, **kwargs: nullcontext(),
    )

    result = check_oa_parity(plan, object(), operation=operation)

    assert result["passed"] is expected_passed
    report = result["testbench_schematic"]["TB"]
    assert report["passed"] is expected_passed
    if mutation in {"terminal-order", "pdk-terminal-order"}:
        assert report["terminal_mismatches"]
    elif mutation != "none":
        assert report["parameter_mismatches"]


@pytest.mark.parametrize(
    ("live_views", "pid_error", "expected_status", "expected_passed"),
    (
        ((), False, "clean", True),
        (
            (
                SimpleNamespace(
                    library="fixture_lib",
                    cell="MODEL",
                    view="spectre",
                    mode="r",
                    visible=True,
                    identity="open-view",
                ),
            ),
            False,
            "blocked",
            False,
        ),
        ((), True, "uncertain", False),
    ),
)
def test_oa_check_reports_public_clean_blocked_and_uncertain_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    live_views: tuple[SimpleNamespace, ...],
    pid_error: bool,
    expected_status: str,
    expected_passed: bool,
) -> None:
    plan = _typed_oa_plan(tmp_path)
    monkeypatch.setattr(oa_check, "active_maestro_sessions", lambda _client: ())
    monkeypatch.setattr(oa_check, "open_cell_views", lambda _client: live_views)
    monkeypatch.setattr(
        oa_check,
        "virtuoso_workdir",
        lambda _client: plan.source.workspace_template,
    )
    if pid_error:
        def unavailable_pid(_client):
            raise RuntimeError("bridge process unavailable")

        monkeypatch.setattr(oa_check, "virtuoso_pid", unavailable_pid)
    else:
        monkeypatch.setattr(oa_check, "virtuoso_pid", lambda _client: 1)
    monkeypatch.setattr(
        oa_check,
        "check_oa_parity",
        lambda *_args, **_kwargs: {"passed": True},
    )
    operation = SimpleNamespace(
        require_project_library_target=(
            lambda _client, _library: plan.source.oa_library
        )
    )

    report = check_oa_library(plan, client=object(), operation=operation)

    assert report["status"] == expected_status
    assert report["passed"] is expected_passed


def test_managed_oa_plan_uses_the_owner_selected_assembly_path(tmp_path: Path) -> None:
    import sys
    _typed_oa_plan(tmp_path)
    manifest_path = tmp_path / "sigilicon.toml"
    with manifest_path.open("a") as stream:
        stream.write(f'\n[runtime.tools]\n"runtime.python" = "{sys.executable}"\n')
    owner = tmp_path / "ip/fixture"
    manifest = owner / "configs/oa.toml"
    renamed = manifest.with_name("native_assembly.toml")
    manifest.rename(renamed)
    component = owner / "component.toml"
    component.write_text(component.read_text().replace(
        "configs/oa.toml", "configs/native_assembly.toml"
    ).replace("[sources]", '\noperation_catalog = "operations"\n\n[sources]\noperations = "ip/fixture/configs/operations.toml"'))
    (owner / "configs/operations.toml").write_text('''schema = 5
contract_kind = "owner-operations"
path_scope = "owner"
owner = "fixture"
[operations.check]
uses = "cadence.oa-check"
filesets = [{ component = "fixture", fileset = "oa_source" }]
config = { owner = "fixture", timeout_seconds = 30 }
''')
    project = Project.open(tmp_path)
    plan = project.plan("fixture:check")
    assert renamed in {source.location for source in plan.sources}
    assert project.preflight(plan).status in {"ready", "blocked"}


@pytest.mark.parametrize(("kind", "view", "suffix"), (
    ("system_verilog", "systemVerilog", "sv"),
    ("veriloga", "veriloga", "va"),
    ("spectre_model", "spectre", "scs"),
))
def test_oa_rebuild_captures_text_import_compiler_dependency(
    tmp_path: Path, kind: str, view: str, suffix: str,
) -> None:
    _typed_oa_plan(tmp_path)
    owner = tmp_path / "ip/fixture"
    cell = owner / "cells/MODEL"
    source_name = f"model.{suffix}"
    (cell / source_name).write_text("module MODEL; endmodule\n")
    manifest = cell / "cell.toml"
    manifest.write_text(manifest.read_text().replace(
        "views = [", f'views = [\n  {{ name = "{view}", kind = "{kind}", source = "{source_name}" }},'
    ))
    component = owner / "component.toml"
    component.write_text(component.read_text().replace(
        "[sources]", '\noperation_catalog = "operations"\n\n[sources]\n'
        'operations = "ip/fixture/configs/operations.toml"\n'
        f'model = "ip/fixture/cells/MODEL/{source_name}"'
    ).replace('oa_source = [', 'oa_source = ["model", '))
    (owner / "configs/operations.toml").write_text('''schema = 5
contract_kind = "owner-operations"
path_scope = "owner"
owner = "fixture"
[operations.rebuild]
uses = "cadence.oa-rebuild"
filesets = [{ component = "fixture", fileset = "oa_source" }]
config = { owner = "fixture", timeout_seconds = 30 }
''')
    project_contract = tmp_path / "sigilicon.toml"
    with project_contract.open("a") as stream:
        stream.write(f'''\n[runtime.tools]
"runtime.python" = "{sys.executable}"
"cadence.spice-in" = "/bin/true"
"cadence.cds-text-to-5x" = "/bin/true"
"cadence.xrun" = "/bin/true"
''')
    plan = Project.open(tmp_path).plan("fixture:rebuild")
    identities = {resource.identity for resource in plan.resources}
    assert "cadence.cds-text-to-5x" in identities
    assert ("cadence.xrun" in identities) is (kind != "spectre_model")


def _native_maestro_project(tmp_path: Path, simulator: str, *, with_layout: bool = False) -> Project:
    _typed_oa_plan(tmp_path, with_layout=with_layout)
    owner = tmp_path / "ip/fixture"
    tb = owner / "cells/tb"
    tb.mkdir()
    (tb / "testbench.scs").write_text("subckt tb\nXD (a y vdd vss) MODEL\nends tb\n")
    (tb / "setup.il").write_text(
        "procedure(fixtureConfig(lib cell dut sourceView refs) t)\n"
        "procedure(fixtureMaestro(session lib cell modelFile modelSection) t)\n"
    )
    (tb / "simulation.toml").write_text(f'''schema = 3
[testbench]
library = "fixture_lib"
cell = "tb"
dut = "MODEL"
source_view = "schematic"
simulator = "{simulator}"
[platform]
pdk = "testpdk"
[setup]
source = "setup.il"
config_procedure = "fixtureConfig"
maestro_procedure = "fixtureMaestro"
''')
    (tb / "cell.toml").write_text('''schema = 1
contract_kind = "oa-cell"
path_scope = "cell"
owner = "fixture"
cell = "tb"
role = "testbench"
canonical_source = "testbench.scs"
views = [
  { name = "netlist", kind = "spectre_netlist", source = "testbench.scs" },
  { name = "schematic", kind = "schematic", source = "testbench.scs", dependencies = ["tb/netlist"] },
  { name = "config", kind = "config", source = "simulation.toml", dependencies = ["tb/schematic"] },
  { name = "measurement", kind = "skill", source = "setup.il", dependencies = ["tb/config"] },
  { name = "maestro", kind = "maestro", source = "simulation.toml", dependencies = ["tb/config", "tb/measurement"] },
]
''')
    component = owner / "component.toml"
    filenames = ("cell.toml", "testbench.scs", "simulation.toml", "setup.il")
    declarations = "\n".join(
        f'tb_{i} = "ip/fixture/cells/tb/{name}"' for i, name in enumerate(filenames)
    )
    component.write_text(component.read_text().replace(
        "[sources]", '\noperation_catalog = "operations"\n\n[sources]\n'
        'operations = "ip/fixture/configs/operations.toml"\n' + declarations
    ).replace('oa_source = [', 'oa_source = ["tb_0", "tb_1", "tb_2", "tb_3", '))
    (owner / "configs/operations.toml").write_text('''schema = 5
contract_kind = "owner-operations"
path_scope = "owner"
owner = "fixture"
[operations.simulate]
uses = "cadence.native-oa"
filesets = [{ component = "fixture", fileset = "oa_source" }]
config = { owner = "fixture", testbench = "tb", timeout_seconds = 30 }
''')
    with (tmp_path / "sigilicon.toml").open("a") as stream:
        stream.write(f'''\n[runtime.tools]
"runtime.python" = "{sys.executable}"
"cadence.virtuoso" = "/bin/true"
"cadence.spectre" = "/bin/true"
"cadence.xrun" = "/bin/true"
''')
    return Project.open(tmp_path)


@pytest.mark.parametrize("simulator", ("spectre", "ams"))
def test_native_maestro_plan_captures_its_simulator_dependencies(
    tmp_path: Path, simulator: str,
) -> None:
    plan = _native_maestro_project(tmp_path, simulator).plan("fixture:simulate")
    identities = {resource.identity for resource in plan.resources}
    assert "cadence.spectre" in identities
    assert ("cadence.xrun" in identities) is (simulator == "ams")


@pytest.mark.parametrize("unrelated_file", ("testbench.scs", "simulation.toml", "setup.il", "cell.toml"))
def test_native_maestro_inputs_follow_the_selected_testbench_and_real_masters(
    tmp_path: Path, unrelated_file: str,
) -> None:
    import shutil
    _native_maestro_project(tmp_path, "spectre")
    owner = tmp_path / "ip/fixture"
    unrelated = owner / "cells/other_tb"
    shutil.copytree(owner / "cells/tb", unrelated)
    for path in unrelated.iterdir():
        path.write_text(path.read_text().replace('"tb"', '"other_tb"').replace("tb/", "other_tb/").replace("subckt tb", "subckt other_tb").replace("ends tb", "ends other_tb"))
    component = owner / "component.toml"
    declarations = "\n".join(
        f'other_{i} = "ip/fixture/cells/other_tb/{path.name}"'
        for i, path in enumerate(sorted(unrelated.iterdir()))
    )
    component.write_text(component.read_text().replace("[sources]", "[sources]\n" + declarations))
    before = Project.open(tmp_path).plan("fixture:simulate")
    selected_paths = {source.location for source in before.sources}
    master = owner / "cells/MODEL/circuit.scs"
    assert master in selected_paths
    assert owner / "cells/tb/setup.il" in selected_paths
    assert not any(path.is_relative_to(unrelated) for path in selected_paths)

    changed = unrelated / unrelated_file
    changed.write_text(changed.read_text() + "\n// unrelated change\n" if changed.suffix != ".toml" else changed.read_text() + "\n# unrelated change\n")
    after = Project.open(tmp_path).plan("fixture:simulate")
    assert after.identity == before.identity

    master.write_text(master.read_text() + "\n// selected master revision\n")
    revised = Project.open(tmp_path).plan("fixture:simulate")
    assert revised.identity != before.identity


@pytest.mark.parametrize("operation", ("check", "rebuild"))
def test_selected_oa_simulation_plan_cannot_replace_assembly_verification(
    tmp_path: Path, operation: str,
) -> None:
    project = _native_maestro_project(tmp_path, "spectre", with_layout=True)
    owner = tmp_path / "ip/fixture"
    plan = plan_oa_library_rebuild(owner / "configs/oa.toml", project=project, testbench="tb")
    assert plan.selected_testbench == "tb"
    assert set(plan.cells) == {"MODEL", "tb"}
    assert not plan.layouts
    with pytest.raises(ValueError, match="requires a complete OA assembly"):
        if operation == "check":
            check_oa_library(plan, client=object())
        else:
            rebuild_oa_library(
                plan, object(), source_paths={}, resource_paths={}, resources=OA_RESOURCES,
                artifacts=managed_execution_workspace(tmp_path),
            )
