from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.cli.flow import _parser, _print_oa_check_summary
from sigilicon.domain.repository import Project
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation
from sigilicon.workflows.oa_check import (
    _library_ownership,
    _locks,
    _recommendation,
    check_oa_library,
)


def test_oa_check_is_a_parser_action_with_no_mutating_arguments() -> None:
    args = _parser().parse_args(
        [
            "oa",
            "check",
            "--owner",
            "fixture",
        ]
    )

    assert args.action == "check"
    assert not hasattr(args, "testbench")
    assert not hasattr(args, "cell")


@pytest.mark.parametrize("retired_action", ("audit", "doctor"))
def test_oa_check_has_no_alias_actions(retired_action: str) -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(
            ["oa", retired_action, "--owner", "fixture"]
        )


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


def test_invalid_manifest_is_reported_as_blocked_without_artifact_write(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "sigilicon.workflows.oa_check.active_maestro_sessions", lambda _client: ()
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_check.open_cell_views", lambda _client: ()
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_check.virtuoso_workdir", lambda _client: tmp_path
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_check.virtuoso_pid", lambda _client: 1
    )
    report = check_oa_library(
        tmp_path / "missing-oa.toml",
        project=Project.from_project_root(tmp_path),
        library="fixture_lib",
        client=SimpleNamespace(),
    )

    assert report["status"] == "blocked"
    assert report["source_contract"]["passed"] is False
    assert "simulation_run" not in report
    assert "product_qualification_conclusion" not in report
    assert not (tmp_path / "artifacts").exists()


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
            project=Project.from_project_root(tmp_path),
            project_root=tmp_path,
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


def test_plain_check_summary_exposes_current_operator_state(capsys) -> None:
    _print_oa_check_summary(
        {
            "status": "clean",
            "source_contract": {
                "passed": True,
                "library": "fixture_lib",
                "cell_count": 33,
                "view_count": 135,
                "testbench_count": 7,
            },
            "ownership": {
                "library": {"passed": True, "registered_path": "/tmp/virtuoso/fixture_lib"}
            },
            "parity": {
                "passed": True,
                "missing_cells": [],
                "missing_views": {},
                "extra_cells": [],
                "extra_views": {},
                "stale_or_modified_views": {},
            },
            "live": {
                "active_maestro_sessions": [],
                "open_cell_views": [],
                "process": {"alive": True},
            },
            "locks": {
                "edit_locks": [],
                "flow_operation_lock": {"held": False},
            },
        }
    )

    output = capsys.readouterr().out
    assert "OA check: clean" in output
    assert "recommendation" not in output
    assert "flow_lock_held=False" in output
    assert "native_attestation: explicit --testbench only" in output
