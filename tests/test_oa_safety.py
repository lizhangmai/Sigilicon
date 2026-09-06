from __future__ import annotations

import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.virtuoso.locks import (
    exclusive_flow_operation,
    inspect_flow_operation_lock,
    require_clean_oa_cell,
    require_clean_oa_view,
)
from sigilicon.virtuoso.oa import (
    OpenCellViewInfo,
    delete_cell,
    delete_cell_view,
    open_cell_views,
)


def _write_lock(path: Path, *, host: str, pid: int) -> None:
    path.write_text(
        f"HostName {host}\nProcessIdentifier {pid}\n",
        encoding="utf-8",
    )


def test_oa_lock_is_refused_by_default(tmp_path: Path) -> None:
    view = tmp_path / "lib" / "cell" / "schematic"
    view.mkdir(parents=True)
    _write_lock(view / "sch.oa.cdslck", host=socket.gethostname(), pid=999_999_999)

    with pytest.raises(RuntimeError, match="OpenAccess lock files exist"):
        require_clean_oa_view(view)


def test_only_proven_local_stale_lock_is_quarantined(tmp_path: Path) -> None:
    view = tmp_path / "lib" / "cell" / "schematic"
    view.mkdir(parents=True)
    lock = view / "sch.oa.cdslck"
    _write_lock(lock, host=socket.gethostname(), pid=999_999_999)

    with pytest.raises(RuntimeError, match="atomic inode-conditional quarantine"):
        require_clean_oa_view(view, quarantine_root=tmp_path / "quarantine")

    captured = tuple((tmp_path / "quarantine").rglob("sch.oa.cdslck"))
    assert len(captured) == 1
    assert captured[0].read_text(encoding="utf-8").startswith("HostName")
    assert lock.exists()


def test_live_local_lock_is_never_quarantined(tmp_path: Path) -> None:
    view = tmp_path / "lib" / "cell" / "schematic"
    view.mkdir(parents=True)
    _write_lock(view / "sch.oa.cdslck", host=socket.gethostname(), pid=os.getpid())

    with pytest.raises(RuntimeError, match="still alive"):
        require_clean_oa_view(view, quarantine_root=tmp_path / "quarantine")


def test_lock_replaced_during_evidence_capture_is_preserved_and_reported(
    monkeypatch, tmp_path: Path
) -> None:
    view = tmp_path / "lib" / "cell" / "schematic"
    view.mkdir(parents=True)
    lock = view / "sch.oa.cdslck"
    _write_lock(lock, host=socket.gethostname(), pid=999_999_999)
    real_link = os.link

    def replace_then_link(source, target, **kwargs):
        _write_lock(Path(source), host=socket.gethostname(), pid=os.getpid())
        real_link(source, target, **kwargs)

    monkeypatch.setattr("sigilicon.virtuoso.locks.os.link", replace_then_link)
    quarantine = tmp_path / "quarantine"

    with pytest.raises(RuntimeError, match="replaced while evidence"):
        require_clean_oa_view(view, quarantine_root=quarantine)

    assert f"ProcessIdentifier {os.getpid()}" in lock.read_text()
    assert tuple(quarantine.rglob("sch.oa.cdslck")) == ()


def test_symbolic_link_lock_is_never_followed(tmp_path: Path) -> None:
    view = tmp_path / "lib" / "cell" / "schematic"
    view.mkdir(parents=True)
    target = tmp_path / "outside-lock"
    _write_lock(target, host=socket.gethostname(), pid=999_999_999)
    (view / "sch.oa.cdslck").symlink_to(target)

    with pytest.raises(RuntimeError, match="symbolic-link OA lock"):
        require_clean_oa_view(view, quarantine_root=tmp_path / "quarantine")


def test_symbolic_link_view_directory_is_never_followed(tmp_path: Path) -> None:
    from sigilicon.virtuoso.locks import require_clean_oa_cell

    cell = tmp_path / "lib" / "cell"
    outside = tmp_path / "outside-view"
    cell.mkdir(parents=True)
    outside.mkdir()
    _write_lock(
        outside / "sch.oa.cdslck",
        host=socket.gethostname(),
        pid=999_999_999,
    )
    (cell / "schematic").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symbolic-link OA view path"):
        require_clean_oa_cell(cell, quarantine_root=tmp_path / "quarantine")

    assert (outside / "sch.oa.cdslck").is_file()


def test_flow_operations_are_serialized(tmp_path: Path) -> None:
    with exclusive_flow_operation(tmp_path, "first"):
        with pytest.raises(RuntimeError, match="operation=first"):
            with exclusive_flow_operation(tmp_path, "second"):
                pytest.fail("the second operation must not acquire the lock")


def test_flow_operation_lock_inspection_distinguishes_marker_from_live_flock(
    tmp_path: Path,
) -> None:
    with exclusive_flow_operation(tmp_path, "inspection-test"):
        live = inspect_flow_operation_lock(tmp_path)
        assert live["exists"] is True
        assert live["held"] is True

    released = inspect_flow_operation_lock(tmp_path)
    assert released["exists"] is True
    assert released["held"] is False
    assert released["operation"] == "inspection-test"


def test_flow_operation_lock_never_follows_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("do-not-touch\n", encoding="utf-8")
    (tmp_path / ".flow-operation.lock").symlink_to(outside)

    with pytest.raises(OSError):
        with exclusive_flow_operation(tmp_path, "unsafe"):
            pytest.fail("symlink lock must not open")

    assert outside.read_text(encoding="utf-8") == "do-not-touch\n"


def test_flow_operation_lock_rejects_hardlinked_file(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("do-not-touch\n", encoding="utf-8")
    os.link(outside, tmp_path / ".flow-operation.lock")

    with pytest.raises(RuntimeError, match="have one link"):
        with exclusive_flow_operation(tmp_path, "unsafe"):
            pytest.fail("hardlink lock must not open")

    assert outside.read_text(encoding="utf-8") == "do-not-touch\n"


def test_config_lock_is_detected_and_only_current_vts_owner_can_update(
    tmp_path: Path,
) -> None:
    view = tmp_path / "lib" / "cell" / "config"
    view.mkdir(parents=True)
    lock = view / "expand.cfg.cdslck"
    _write_lock(lock, host=socket.gethostname(), pid=os.getpid())

    with pytest.raises(RuntimeError, match="lock files exist"):
        require_clean_oa_view(view)

    assert require_clean_oa_view(
        view,
        allowed_config_owner_pid=os.getpid(),
    ) == ()
    assert lock.is_file()


def test_mutation_scope_rejects_live_config_lock_after_write(
    monkeypatch, workspace_factory
) -> None:
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.virtuoso_pid",
        lambda _client: os.getpid(),
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.require_clean_oa_cell",
        require_clean_oa_cell,
    )

    client = object()
    operation = None
    lock = None
    with pytest.raises(RuntimeError, match="OpenAccess lock files exist"):
        with workspace_factory(client, library="lib") as operation:
            cell = operation.root / "lib" / "cell"
            lock = cell / "config" / "expand.cfg.cdslck"
            lock.parent.mkdir(parents=True)
            _write_lock(lock, host=socket.gethostname(), pid=os.getpid())

            with operation.mutation_scope(
                "lib",
                cells=("cell",),
                phase="config lock lifecycle",
                allow_current_config_lock=True,
            ):
                operation.require_active_mutation(
                    client,
                    "lib",
                    "cell",
                    phase="config lock lifecycle write",
                )

    assert operation is not None
    assert operation.uncertain_reason is not None
    assert lock is not None
    assert lock.is_file()


def test_open_view_inventory_reports_exact_mode_and_visibility() -> None:
    class Client:
        def execute_skill(self, _source, **_kwargs):
            return type(
                "Response",
                (),
                {
                    "output": '"lib|cell|schematic|\\\"r\\\"|t|db:0x123\\n"',
                    "errors": [],
                },
            )()

    assert open_cell_views(Client(), library="lib") == (
        OpenCellViewInfo("lib", "cell", "schematic", "r", True, "db:0x123"),
    )


def test_delete_cell_uses_exact_deletion_scope(workspace_factory) -> None:
    target = None

    class Client:
        def execute_skill(self, _source, **_kwargs):
            assert target is not None
            target.rmdir()
            return SimpleNamespace(output="t", errors=[])

    client = Client()
    with workspace_factory(client, library="lib") as operation:
        target = operation.root / "lib" / "retired"
        target.mkdir(parents=True)
        with operation.mutation_scope(
            "lib",
            cells=("retired",),
            expected_deleted_cells=("retired",),
            phase="OA cell deletion proof",
        ):
            delete_cell(
                client,
                "lib",
                "retired",
                operation=operation,
            )

    assert not target.exists()


def test_delete_cell_view_retains_parent_cell(workspace_factory) -> None:
    target = None

    class Client:
        def execute_skill(self, _source, **_kwargs):
            assert target is not None
            target.rmdir()
            return SimpleNamespace(output="t", errors=[])

    client = Client()
    with workspace_factory(client, library="lib") as operation:
        cell = operation.root / "lib" / "retained"
        target = cell / "retired_layout"
        target.mkdir(parents=True)
        with operation.mutation_scope(
            "lib",
            cells=("retained",),
            phase="OA view deletion proof",
        ):
            delete_cell_view(
                client,
                "lib",
                "retained",
                "retired_layout",
                operation=operation,
            )

    assert cell.is_dir()
    assert not target.exists()
