from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import sys
from types import SimpleNamespace

import pytest

import sigilicon.adapters.cadence.oa_check as oa_check
from conftest import managed_execution_workspace, write_component_owner, write_test_platform
from sigilicon.project import Project
from sigilicon.execution._model import Resources
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation
from sigilicon.adapters.cadence.oa_check import (
    check_oa_library,
)
from sigilicon.adapters.cadence.oa_library import (
    OALibraryRebuildPlan,
    ViewRebuildStep,
    plan_oa_library_rebuild,
)
from sigilicon.adapters.cadence.oa_library_execution import rebuild_oa_library
from sigilicon.adapters.cadence.oa_testbench import materialize_oa_models
from sigilicon.domain.platform import load_platform
from sigilicon.domain.oa_library import OACellViewSource
from sigilicon.domain.source import load_text_source_snapshot
from sigilicon.adapters.cadence.oa_library_execution import check_oa_parity


OA_RESOURCES = Resources()


def test_native_oa_models_keep_sealed_bytes_names_and_relative_includes(tmp_path: Path) -> None:
    write_test_platform(tmp_path)
    platform_root = tmp_path / "configs/platform/testpdk"
    contract = platform_root / "simulation.toml"
    contract.write_text(contract.read_text() + 'support_files = ["devices/core.scs"]\n')
    top = platform_root / "model.scs"
    top.write_text('include "devices/core.scs"\n')
    support = platform_root / "devices/core.scs"
    support.parent.mkdir()
    support.write_text("// selected model bytes\n")
    model_set = load_platform(Project.open(tmp_path), "testpdk").simulation.default
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    paths = {}
    for i, source in enumerate(model_set.paths):
        captured = sealed / f"resource-{i}"
        captured.write_bytes(source.read_bytes())
        paths[source] = captured
        source.write_text("changed after planning\n")

    staged = materialize_oa_models(model_set, paths, managed_execution_workspace(tmp_path))

    assert staged.name == "model.scs"
    assert staged.read_text() == 'include "devices/core.scs"\n'
    assert (staged.parent / "devices/core.scs").read_text() == "// selected model bytes\n"


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


def test_oa_check_rejects_pure_layout_snapshot() -> None:
    step = SimpleNamespace(
        spec=SimpleNamespace(cell="CELL", view="layout"),
        planning=SimpleNamespace(plan=None),
    )
    plan = SimpleNamespace(layouts=(step,))
    plan.require_layout_ir = lambda operation: OALibraryRebuildPlan.require_layout_ir(
        plan, operation
    )

    with pytest.raises(ValueError, match="requires managed LayoutIR"):
        check_oa_library(plan, client=SimpleNamespace())


def test_oa_rebuild_rejects_pure_layout_snapshot_before_live_access(tmp_path: Path) -> None:
    step = SimpleNamespace(
        spec=SimpleNamespace(cell="CELL", view="layout"),
        planning=SimpleNamespace(plan=None),
    )
    plan = SimpleNamespace(layouts=(step,))
    plan.require_layout_ir = lambda operation: OALibraryRebuildPlan.require_layout_ir(
        plan, operation
    )
    client = SimpleNamespace(
        library=SimpleNamespace(
            list=lambda **_kwargs: pytest.fail("live OA accessed before IR validation")
        )
    )

    with pytest.raises(ValueError, match="OA rebuild requires managed LayoutIR"):
        rebuild_oa_library(
            plan,
            client,
            source_paths={},
            resource_paths={},
            resources=OA_RESOURCES,
            artifacts=managed_execution_workspace(tmp_path),
        )


def _typed_oa_plan(root: Path) -> OALibraryRebuildPlan:
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
    (owner / "configs/operations.toml").write_text('''schema = 4
contract_kind = "owner-operations"
path_scope = "owner"
owner = "fixture"
[operations.check]
uses = "cadence.oa-check"
filesets = ["oa_source"]
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
    (owner / "configs/operations.toml").write_text('''schema = 4
contract_kind = "owner-operations"
path_scope = "owner"
owner = "fixture"
[operations.rebuild]
uses = "cadence.oa-rebuild"
filesets = ["oa_source"]
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


@pytest.mark.parametrize("simulator", ("spectre", "ams"))
def test_native_maestro_plan_captures_its_simulator_dependencies(
    tmp_path: Path, simulator: str,
) -> None:
    _typed_oa_plan(tmp_path)
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
    (owner / "configs/operations.toml").write_text('''schema = 4
contract_kind = "owner-operations"
path_scope = "owner"
owner = "fixture"
[operations.simulate]
uses = "cadence.native-oa"
filesets = ["oa_source"]
config = { owner = "fixture", testbench = "tb", timeout_seconds = 30 }
''')
    with (tmp_path / "sigilicon.toml").open("a") as stream:
        stream.write(f'''\n[runtime.tools]
"runtime.python" = "{sys.executable}"
"cadence.virtuoso" = "/bin/true"
"cadence.spectre" = "/bin/true"
"cadence.xrun" = "/bin/true"
''')
    plan = Project.open(tmp_path).plan("fixture:simulate")
    identities = {resource.identity for resource in plan.resources}
    assert "cadence.spectre" in identities
    assert ("cadence.xrun" in identities) is (simulator == "ams")
