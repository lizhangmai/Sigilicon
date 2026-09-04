from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.project import Project
from sigilicon.execution._model import Resources
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation
from sigilicon.adapters.cadence.oa_check import (
    _library_ownership,
    _locks,
    _recommendation,
    check_oa_library,
)
from sigilicon.adapters.cadence.oa_library import OALibraryRebuildPlan, rebuild_oa_library


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


def test_current_clean_check_is_clean() -> None:
    assert (
        _recommendation(
            plan_error=None,
            parity={"passed": True},
            ownership={"conflicts": []},
            bridge={"active_maestro_sessions": [], "open_cell_views": []},
            locks={"edit_locks": [], "errors": []},
        )
        == "clean"
    )


def test_dead_bridge_process_makes_live_state_uncertain() -> None:
    assert (
        _recommendation(
            plan_error=None,
            parity={"passed": True},
            ownership={"conflicts": []},
            bridge={
                "active_maestro_sessions": [],
                "open_cell_views": [],
                "process": {"pid": 123, "alive": False},
            },
            locks={"edit_locks": [], "errors": []},
        )
        == "uncertain"
    )


def test_check_proves_live_library_path_is_the_manifest_target(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "virtuoso"
    expected = workspace / "fixture_lib"
    shadow = workspace / "shadow"
    expected.mkdir(parents=True)
    shadow.mkdir()
    plan = SimpleNamespace(
        library="fixture_lib",
        source=SimpleNamespace(
            project_root=tmp_path,
            workspace_root=workspace,
            oa_library=expected,
        ),
    )
    client = SimpleNamespace(
        library=SimpleNamespace(
            get=lambda _library, **_kwargs: SimpleNamespace(path=str(shadow))
        )
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.virtuoso_workdir", lambda _client: workspace
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.assert_no_active_maestro_sessions",
        lambda *_args, **_kwargs: None,
    )

    report = _library_ownership(plan, client)

    assert report["passed"] is False
    assert report["status"] == "blocked"
    assert report["registered_path"] == str(shadow)
    assert report["expected_path"] == str(expected)


def test_lock_inventory_includes_undeclared_oa_cache_views(tmp_path: Path) -> None:
    view = tmp_path / "virtuoso" / "fixture_lib" / "EXTRA" / "schematic"
    view.mkdir(parents=True)
    (view / ".cdslck.1").write_text(
        "HostName test-host\nProcessIdentifier 1\n", encoding="utf-8"
    )
    plan = SimpleNamespace(
        expected_views={},
        source=SimpleNamespace(
            project_root=tmp_path,
            oa_library=tmp_path / "virtuoso" / "fixture_lib",
            workspace_template=tmp_path / "virtuoso",
        ),
    )

    report = _locks(plan)

    assert len(report["edit_locks"]) == 1
    assert report["edit_locks"][0]["cell"] == "EXTRA"
    assert report["edit_locks"][0]["view"] == "schematic"
    assert report["edit_locks"][0]["declared"] is False


def test_released_flow_marker_is_not_reported_as_live_lock() -> None:
    base = {
        "edit_locks": [],
        "errors": [],
        "flow_operation_lock": {"exists": True, "held": False},
    }
    common = {
        "plan_error": None,
        "parity": {"passed": True},
        "ownership": {"conflicts": []},
        "bridge": {"active_maestro_sessions": [], "open_cell_views": []},
    }

    assert _recommendation(locks=base, **common) == "clean"
    assert (
        _recommendation(
            locks={**base, "flow_operation_lock": {"exists": True, "held": True}},
            **common,
        )
        == "blocked"
    )
