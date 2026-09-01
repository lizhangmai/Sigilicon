from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from sigilicon.artifacts import ArtifactRecord, load_manifest
from sigilicon.external_tools import ProcessGroupCleanupUncertainError
from sigilicon.paths import ProjectContext
from sigilicon.virtuoso.confirmation import require_bridge_confirmation
from sigilicon.virtuoso.oa import OpenCellViewInfo
from sigilicon.virtuoso.operation_journal import (
    rollback_unreferenced_operation_incident,
    write_operation_incident,
)
from sigilicon.virtuoso.workspace import (
    OperationPolicy,
    WorkspaceOperation,
    workspace_operation,
)
from conftest import write_project_context


class Client:
    def __init__(self, workdir, *, remote: bool = False) -> None:
        self.workdir = workdir
        self.ssh_runner = object() if remote else None

    def execute_skill(self, source: str, **_kwargs):
        output = f'"{self.workdir}"' if source == "getWorkingDir()" else "nil"
        return SimpleNamespace(output=output, errors=[])


def _design_execution(project, identity):
    return ProjectContext.from_project_root(project).artifacts.execution(
        owner="lib",
        target="cell",
        flow="design-sync",
        variant="recursive",
        identity=identity,
        artifact_kind="design_sync",
        identity_kind="attempt_id",
    )


def test_incident_rollback_refuses_replaced_symlink_parent(tmp_path) -> None:
    project = tmp_path / "project"
    write_project_context(project)
    workspace = project / "virtuoso"
    workspace.mkdir(parents=True)
    operation_id = "f" * 32
    incident = write_operation_incident(
        workspace_root=workspace,
        artifact_root=project / "artifacts",
        operation_id=operation_id,
        name="test",
        policy="read-only",
        status="failed",
        error=RuntimeError("failure"),
        uncertain_reason=None,
        view_snapshots=(),
        ownership_scopes=(),
    )
    original_parent = incident.parent
    moved_parent = original_parent.with_name("saved-operation")
    original_parent.rename(moved_parent)
    external = tmp_path / "external-operation"
    external.mkdir()
    external_incident = external / "incident.json"
    external_incident.write_text("do not delete\n", encoding="utf-8")
    original_parent.symlink_to(external, target_is_directory=True)

    with pytest.raises(RuntimeError, match="safely roll back"):
        rollback_unreferenced_operation_incident(
            artifact_root=project / "artifacts",
            operation_id=operation_id,
            incident_path=incident,
        )

    assert external_incident.read_text(encoding="utf-8") == "do not delete\n"
    assert (moved_parent / "incident.json").is_file()


def test_incident_uses_explicit_artifact_root_for_external_workspace(
    tmp_path,
) -> None:
    workspace = tmp_path / "external-workspace"
    artifacts = tmp_path / "project-artifacts"
    workspace.mkdir()
    operation_id = "e" * 32

    incident = write_operation_incident(
        workspace_root=workspace,
        artifact_root=artifacts,
        operation_id=operation_id,
        name="external-workspace-test",
        policy="read-only",
        status="failed",
        error=RuntimeError("failure"),
        uncertain_reason=None,
        view_snapshots=(),
        ownership_scopes=(),
    )

    assert incident == (
        artifacts / "system/operations" / operation_id / "incident.json"
    )
    payload = json.loads(incident.read_text(encoding="utf-8"))
    assert payload["workspace_root"] == str(workspace)


def test_workspace_rejects_remote_mutation(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="remote OA locking"):
        with workspace_operation(Client(tmp_path, remote=True), tmp_path, "sync"):
            pytest.fail("remote mutation must not start")


def test_workspace_rejects_wrong_virtuoso_workdir(tmp_path) -> None:
    expected = tmp_path / "expected"
    actual = tmp_path / "actual"
    expected.mkdir()
    actual.mkdir()
    with pytest.raises(RuntimeError, match="Virtuoso workdir"):
        with workspace_operation(Client(actual), expected, "sync"):
            pytest.fail("wrong workspace must not acquire the operation lock")


def test_workspace_accepts_caller_owned_operation_identity(tmp_path) -> None:
    operation_id = "a" * 32

    with workspace_operation(
        Client(tmp_path),
        tmp_path,
        "bound-operation",
        operation_id=operation_id,
    ) as operation:
        assert operation.operation_id == operation_id


def test_view_lease_preserves_unowned_new_hidden_read_view(
    monkeypatch, tmp_path, workspace_factory
) -> None:
    client = object()
    opened = OpenCellViewInfo("lib", "cell", "schematic", "r", False)
    snapshots = iter(((), (opened,)))
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.require_project_library_path",
        lambda *_args: tmp_path / "lib",
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.open_cell_views",
        lambda *_args, **_kwargs: next(snapshots),
    )
    operation = None
    with pytest.raises(RuntimeError, match="name-based ownership is not proof"):
        with workspace_factory(client) as operation:
            with operation.view_lease("lib"):
                pass

    assert operation is not None
    assert operation.uncertain_reason is not None


def test_view_lease_refuses_preexisting_view_when_quiescence_is_required(
    monkeypatch, tmp_path, workspace_factory
) -> None:
    client = object()
    existing = OpenCellViewInfo("lib", "cell", "symbol", "r", False)
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.require_project_library_path",
        lambda *_args: tmp_path / "lib",
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.open_cell_views",
        lambda *_args, **_kwargs: (existing,),
    )

    with workspace_factory(client) as operation:
        with pytest.raises(RuntimeError, match="requires quiescent state"):
            with operation.view_lease("lib"):
                pytest.fail("lease must not start")


def test_exact_view_lease_grants_only_its_declared_cellview(
    monkeypatch, tmp_path, workspace_factory
) -> None:
    client = object()
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.require_project_library_path",
        lambda *_args: tmp_path / "lib",
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.open_cell_views",
        lambda *_args, **_kwargs: (),
    )

    with workspace_factory(client) as operation:
        with operation.view_lease(
            "lib", cells=("cell",), views=(("cell", "schematic"),)
        ):
            assert operation.has_active_view_lease(
                "lib", cell="cell", view="schematic"
            )
            assert not operation.has_active_view_lease(
                "lib", cell="cell", view="symbol"
            )
            assert not operation.has_active_view_lease("lib", cell="cell")


def test_exact_view_mutation_scope_accepts_only_covered_views(
    workspace_factory,
) -> None:
    client = object()

    with workspace_factory(client) as operation:
        with operation.view_lease(
            "lib", cells=("cell",), views=(("cell", "schematic"),)
        ):
            with operation.mutation_scope(
                "lib",
                cells=("cell",),
                views=(("cell", "schematic"),),
                phase="exact schematic write",
            ):
                pass
            with pytest.raises(RuntimeError, match="lib/cell/symbol"):
                with operation.mutation_scope(
                    "lib",
                    cells=("cell",),
                    views=(("cell", "symbol"),),
                    phase="unauthorized symbol write",
                ):
                    pytest.fail("a schematic lease must not authorize a symbol write")


def test_lost_bridge_confirmation_marks_uncertain_before_lease_cleanup(
    monkeypatch, tmp_path, workspace_factory
) -> None:
    client = object()
    inventory_calls = 0

    def inventory(*_args, **_kwargs):
        nonlocal inventory_calls
        inventory_calls += 1
        return ()

    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.require_project_library_path",
        lambda *_args: tmp_path / "lib",
    )
    monkeypatch.setattr("sigilicon.virtuoso.workspace.open_cell_views", inventory)

    with pytest.raises(TimeoutError, match="bridge timed out"):
        with workspace_factory(client) as operation:
            with operation.view_lease("lib"):
                require_bridge_confirmation(
                    operation,
                    "write OA",
                    lambda: (_ for _ in ()).throw(TimeoutError("bridge timed out")),
                )

    assert inventory_calls == 1
    assert operation.uncertain_reason == (
        "lost completion confirmation during write OA: "
        "TimeoutError: bridge timed out"
    )


def test_uncertainty_accumulates_without_losing_exact_scope_evidence(
    workspace_factory,
) -> None:
    client = object()
    operation = None

    with pytest.raises(RuntimeError, match="ended with uncertain state"):
        with workspace_factory(client) as operation:
            operation.mark_uncertain("exact-view scope abc123 may still be active")
            operation.mark_uncertain("could not determine whether session opened")

    assert operation is not None
    assert operation.uncertain_reason == (
        "exact-view scope abc123 may still be active | "
        "could not determine whether session opened"
    )
    assert operation.incident_path is None


def test_quiescent_cell_rejects_symlink_escape_before_any_oa_check(
    tmp_path,
    workspace_factory,
) -> None:
    from sigilicon.virtuoso.workspace import require_quiescent_project_cell

    client = object()
    outside = tmp_path / "outside-cell"
    outside.mkdir()
    with workspace_factory(client, library="lib") as operation:
        library_path = operation.root / "lib"
        library_path.mkdir()
        (library_path / "escaped").symlink_to(outside, target_is_directory=True)

        with pytest.raises(RuntimeError, match="symbolic-link OA cell path"):
            require_quiescent_project_cell(operation, "lib", "escaped")


def test_library_mutation_scope_rejects_library_symlink_before_adapter(
    tmp_path,
    workspace_factory,
) -> None:
    client = SimpleNamespace(library=SimpleNamespace(list=lambda **_kwargs: []))
    outside = tmp_path / "outside-library"
    outside.mkdir()
    with workspace_factory(client) as operation:
        link = operation.root / "escaped"
        link.symlink_to(outside, target_is_directory=True)

        with pytest.raises(RuntimeError, match="symbolic-link expected library path"):
            with operation.mutation_scope(
                "escaped",
                cells=None,
                phase="library symlink proof",
                expected_library_path=link,
                require_view_lease=False,
            ):
                pytest.fail("external library adapter must not run")


def test_library_mutation_scope_rejects_open_view_before_technology_binding(
    monkeypatch,
    workspace_factory,
) -> None:
    client = SimpleNamespace(library=SimpleNamespace(list=lambda **_kwargs: []))
    open_view = OpenCellViewInfo(
        "lib", "cell", "schematic", "r", False, "db:0xopen"
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.open_cell_views",
        lambda _client, **kwargs: (open_view,) if kwargs.get("library") == "lib" else (),
    )

    with workspace_factory(client) as operation:
        with pytest.raises(RuntimeError, match="library lib has open views"):
            with operation.mutation_scope(
                "lib",
                cells=None,
                phase="technology binding proof",
                expected_library_path=operation.root / "lib",
                require_view_lease=False,
            ):
                pytest.fail("technology adapter must not run")


def test_dispatched_mutation_failure_is_uncertain_even_when_resource_audit_is_clean(
    workspace_factory,
) -> None:
    client = object()
    operation = None

    with pytest.raises(ValueError, match="adapter failed"):
        with workspace_factory(client, library="lib") as current:
            operation = current
            with current.mutation_scope(
                "lib",
                cells=("cell",),
                phase="transactional failure proof",
            ):
                current.require_active_mutation(
                    client,
                    "lib",
                    "cell",
                    phase="test adapter dispatch",
                )
                raise ValueError("adapter failed")

    assert operation is not None
    assert "raised after write dispatch" in (operation.uncertain_reason or "")
    assert operation.incident_path is None


def test_mutation_scope_requires_lease_covering_the_exact_cell(
    workspace_factory,
) -> None:
    client = object()

    with workspace_factory(client) as operation:
        with operation.view_lease("lib", cells=("leased",)):
            with pytest.raises(RuntimeError, match="covering exact target"):
                with operation.mutation_scope(
                    "lib",
                    cells=("foreign",),
                    phase="wrong-cell proof",
                ):
                    pytest.fail("a lease for another cell must not authorize writes")


def test_mutation_scope_detects_cell_inode_replacement(
    workspace_factory,
) -> None:
    client = object()

    with pytest.raises(RuntimeError, match="identity changed"):
        with workspace_factory(client, library="lib") as operation:
            cell = operation.root / "lib" / "cell"
            cell.mkdir(parents=True)
            with operation.mutation_scope(
                "lib",
                cells=("cell",),
                phase="cell identity proof",
            ):
                cell.rename(operation.root / "lib" / "old-cell")
                cell.mkdir()


def test_mutation_scope_accepts_exact_declared_cell_deletion(
    workspace_factory,
) -> None:
    client = object()

    with workspace_factory(client, library="lib") as operation:
        cell = operation.root / "lib" / "retired"
        cell.mkdir(parents=True)
        with operation.mutation_scope(
            "lib",
            cells=("retired",),
            expected_deleted_cells=("retired",),
            phase="declared deletion proof",
        ):
            operation.require_active_mutation(
                client,
                "lib",
                "retired",
                phase="delete retired cell",
            )
            cell.rmdir()


def test_mutation_scope_rejects_deletion_outside_exact_cell_scope(
    workspace_factory,
) -> None:
    client = object()

    with workspace_factory(client, library="lib") as operation:
        with pytest.raises(ValueError, match="contained in the exact cell scope"):
            with operation.mutation_scope(
                "lib",
                cells=("retained",),
                expected_deleted_cells=("retired",),
                phase="out-of-scope deletion proof",
            ):
                pytest.fail("out-of-scope deletion must not be authorized")


def test_library_wide_mutation_scope_cannot_authorize_a_cell_adapter(
    workspace_factory,
) -> None:
    client = SimpleNamespace(library=SimpleNamespace(list=lambda **_kwargs: []))

    with workspace_factory(client) as operation:
        with operation.mutation_scope(
            "lib",
            cells=None,
            phase="library-only proof",
            expected_library_path=operation.root / "lib",
            require_view_lease=False,
        ):
            with pytest.raises(RuntimeError, match="no unique active OA mutation scope"):
                operation.require_active_mutation(
                    client,
                    "lib",
                    "cell",
                    phase="cell adapter",
                )


def test_read_only_policy_cannot_open_mutation_scope(workspace_factory) -> None:
    client = object()

    with workspace_factory(
        client,
        policy=OperationPolicy.READ_ONLY,
    ) as operation:
        with pytest.raises(RuntimeError, match="does not grant oa-mutation"):
            with operation.mutation_scope(
                "lib",
                cells=("cell",),
                phase="read-only escalation proof",
                require_view_lease=False,
            ):
                pytest.fail("read-only policy must not escalate to mutation")


def test_view_lease_detects_same_name_mode_count_with_replaced_dbid(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    root = project / "virtuoso"
    root.mkdir(parents=True)
    before = OpenCellViewInfo(
        "lib", "cell", "schematic", "r", False, "db:0xold"
    )
    replacement = OpenCellViewInfo(
        "lib", "cell", "schematic", "r", False, "db:0xforeign"
    )
    snapshots = iter(((before,), (replacement,)))
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.require_project_library_path",
        lambda *_args: root / "lib",
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.open_cell_views",
        lambda *_args, **_kwargs: next(snapshots),
    )

    with pytest.raises(RuntimeError, match="changed shared Virtuoso open-view state"):
        with workspace_operation(Client(root), root, "identity-replacement") as operation:
            with operation.view_lease("lib", require_quiescent=False):
                pass


def test_view_lease_never_auto_closes_new_visible_or_writable_view(
    monkeypatch, tmp_path, workspace_factory
) -> None:
    client = object()
    unsafe = OpenCellViewInfo("lib", "cell", "schematic", "a", True)
    snapshots = iter(((), (unsafe,)))
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.require_project_library_path",
        lambda *_args: tmp_path / "lib",
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.open_cell_views",
        lambda *_args, **_kwargs: next(snapshots),
    )
    with pytest.raises(RuntimeError, match="changed shared Virtuoso open-view state"):
        with workspace_factory(client) as operation:
            with operation.view_lease("lib"):
                pass


def test_view_checkpoint_reports_the_first_leaking_phase(
    monkeypatch, tmp_path, workspace_factory
) -> None:
    client = object()
    leaked = OpenCellViewInfo("analogLib", "dependency", "symbol", "r", False)
    snapshots = iter(((), (leaked,)))
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.require_project_library_path",
        lambda *_args: tmp_path / "lib",
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.open_cell_views",
        lambda *_args, **_kwargs: next(snapshots),
    )

    with pytest.raises(RuntimeError, match="during config view creation"):
        with workspace_factory(client) as operation:
            with operation.view_lease("lib") as lease:
                lease.checkpoint("config view creation")

    assert operation.uncertain_reason is not None


def test_workspace_operation_cannot_be_constructed_without_boundary_capability(
    tmp_path,
) -> None:
    with pytest.raises(TypeError, match="_workspace_capability"):
        WorkspaceOperation(object(), tmp_path, "forged")


def test_workspace_capability_expires_at_context_exit(
    workspace_factory,
) -> None:
    from sigilicon.virtuoso.capability import require_workspace_capability

    client = object()
    with workspace_factory(client) as operation:
        require_workspace_capability(operation, client)

    with pytest.raises(RuntimeError, match="inactive or invalid"):
        require_workspace_capability(operation, client)


def test_after_inventory_failure_marks_operation_uncertain(
    monkeypatch, workspace_factory
) -> None:
    client = object()
    calls = 0

    def inventory(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ()
        raise TimeoutError("inventory response lost")

    monkeypatch.setattr("sigilicon.virtuoso.workspace.open_cell_views", inventory)
    operation = None
    with pytest.raises(TimeoutError, match="inventory response lost"):
        with workspace_factory(client) as operation:
            with operation.view_lease("lib"):
                pass

    assert operation is not None
    assert operation.uncertain_reason is not None
    assert "could not inventory open views after" in operation.uncertain_reason


def test_final_audit_uncertainty_reaches_manifest_before_terminal_transition(
    monkeypatch,
    tmp_path,
) -> None:
    project = tmp_path / "project"
    write_project_context(project)
    root = project / "virtuoso"
    root.mkdir(parents=True)
    record = ArtifactRecord.begin(
        _design_execution(project, "1" * 32),
        entities={"library": "lib", "cell": "cell"},
        operation="sync-design",
        backend="virtuoso-oa",
    )
    calls = 0

    def audit(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise TimeoutError("late session inventory loss")

    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.assert_no_active_maestro_sessions",
        audit,
    )

    with pytest.raises(RuntimeError, match="workspace exit audit also failed"):
        with workspace_operation(Client(root), root, "sync-design") as operation:
            operation.register_artifact(record)
            operation.defer_commit(
                lambda: pytest.fail("failure must not commit"),
                on_failure=lambda error: record.fail(
                    error,
                    partial_failure={
                        "completed_cells": ["leaf"],
                        "failed_cell": "top",
                        "failed_stage": "symbol",
                    },
                    uncertain_reason=operation.uncertain_reason,
                ),
            )
            raise RuntimeError("body mutation failed")

    manifest = load_manifest(record.paths.manifest)
    assert manifest["status"] == "uncertain"
    assert manifest["uncertain_reason"] == "late session inventory loss"
    assert manifest["partial_failure"]["completed_cells"] == ["leaf"]
    assert manifest["incident_reference"] == (
        f"system/operations/{operation.operation_id}/incident.json"
    )


def test_operation_incident_is_referenced_by_the_related_attempt_manifest(tmp_path) -> None:
    project = tmp_path / "project"
    write_project_context(project)
    root = project / "virtuoso"
    root.mkdir(parents=True)
    record = ArtifactRecord.begin(
        _design_execution(project, "1" * 32),
        entities={"library": "lib", "cell": "cell"},
        operation="sync-design",
        backend="virtuoso-oa",
    )

    with pytest.raises(RuntimeError, match="write failed"):
        with workspace_operation(Client(root), root, "sync-design") as operation:
            operation.register_artifact(record)
            operation.defer_commit(
                lambda: pytest.fail("failure must not commit"),
                on_failure=lambda error: record.fail(error),
            )
            raise RuntimeError("write failed")

    manifest = load_manifest(record.paths.manifest)
    assert manifest["status"] == "failed"
    assert manifest["operation_id"] == operation.operation_id
    assert manifest["incident_reference"] == (
        f"system/operations/{operation.operation_id}/incident.json"
    )
    assert (record.paths.artifact_root / manifest["incident_reference"]).is_file()


def test_wrapped_process_cleanup_failure_marks_workspace_artifact_uncertain(
    tmp_path,
) -> None:
    project = tmp_path / "project"
    write_project_context(project)
    root = project / "virtuoso"
    root.mkdir(parents=True)
    record = ArtifactRecord.begin(
        _design_execution(project, "3" * 32),
        entities={"library": "lib", "cell": "cell"},
        operation="sync-design",
        backend="virtuoso-oa",
    )

    with pytest.raises(RuntimeError, match="input identity also changed"):
        with workspace_operation(Client(root), root, "sync-design") as operation:
            operation.register_artifact(record)
            operation.defer_commit(
                lambda: pytest.fail("failure must not commit"),
                on_failure=lambda error: record.fail(
                    error,
                    uncertain_reason=operation.uncertain_reason,
                ),
            )
            try:
                raise ProcessGroupCleanupUncertainError("owned group unknown")
            finally:
                raise RuntimeError("input identity also changed")

    state = load_manifest(record.paths.manifest)
    assert state["status"] == "uncertain"
    assert "owned group unknown" in state["uncertain_reason"]
    assert state["incident_reference"] == (
        f"system/operations/{operation.operation_id}/incident.json"
    )


def test_incident_link_failure_rolls_back_unreferenced_journal(
    monkeypatch, tmp_path
) -> None:
    project = tmp_path / "project"
    write_project_context(project)
    root = project / "virtuoso"
    root.mkdir(parents=True)
    record = ArtifactRecord.begin(
        _design_execution(project, "2" * 32),
        entities={"library": "lib", "cell": "cell"},
        operation="sync-design",
        backend="virtuoso-oa",
    )
    monkeypatch.setattr(
        record,
        "attach_incident",
        lambda _path: (_ for _ in ()).throw(OSError("manifest link failed")),
    )

    with pytest.raises(RuntimeError, match="write failed"):
        with workspace_operation(Client(root), root, "sync-design") as operation:
            operation.register_artifact(record)
            operation.defer_commit(
                lambda: pytest.fail("failure must not commit"),
                on_failure=lambda error: record.fail(error),
            )
            raise RuntimeError("write failed")

    assert operation.incident_path is None
    assert not (
        project / "artifacts/system/operations" / operation.operation_id / "incident.json"
    ).exists()
    assert load_manifest(record.paths.manifest)["incident_reference"] is None


def test_deferred_commit_runs_only_after_view_reconciliation(
    monkeypatch, tmp_path
) -> None:
    project = tmp_path / "project"
    root = project / "virtuoso"
    root.mkdir(parents=True)
    unsafe = OpenCellViewInfo("lib", "cell", "schematic", "a", False)
    snapshots = iter(((), (unsafe,)))
    events: list[str] = []
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.require_project_library_path",
        lambda *_args: root / "lib",
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.open_cell_views",
        lambda *_args, **_kwargs: next(snapshots),
    )

    with pytest.raises(RuntimeError, match="changed shared Virtuoso open-view state"):
        with workspace_operation(Client(root), root, "transaction") as operation:
            with operation.view_lease("lib"):
                operation.defer_commit(
                    lambda: events.append("commit"),
                    on_failure=lambda _error: events.append("incomplete"),
                )

    assert events == ["incomplete"]
