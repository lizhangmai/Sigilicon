from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import sigilicon.adapters.cadence.oa_check as oa_check
from conftest import write_component_owner, write_test_platform
from sigilicon.project import Project
from sigilicon.execution._model import Resources
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation
from sigilicon.adapters.cadence.oa_check import (
    check_oa_library,
)
from sigilicon.adapters.cadence.oa_library import (
    OALibraryRebuildPlan,
    plan_oa_library_rebuild,
)
from sigilicon.adapters.cadence.oa_library_execution import rebuild_oa_library


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


def test_oa_rebuild_rejects_pure_layout_snapshot_before_live_access() -> None:
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
