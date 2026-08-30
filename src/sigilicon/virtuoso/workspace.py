"""Single safety boundary for every automated Virtuoso workspace operation."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from enum import Enum
import os
from pathlib import Path
import stat
from typing import Any, Callable, Iterator
import uuid

from sigilicon.external_tools import process_group_cleanup_uncertainty
from sigilicon.paths import validate_artifact_id
from sigilicon.virtuoso.capability import (
    WorkspaceAuthority,
    _WorkspaceCapability,
    _bind_workspace_capability,
    _issue_workspace_capability,
    _revoke_workspace_capability,
    require_workspace_capability,
)
from sigilicon.virtuoso.locks import exclusive_flow_operation
from sigilicon.virtuoso.locks import require_clean_oa_cell
from sigilicon.virtuoso.maestro import assert_no_active_maestro_sessions
from sigilicon.virtuoso.operation_journal import (
    rollback_unreferenced_operation_incident,
    write_operation_incident,
)
from sigilicon.virtuoso.oa import (
    OpenCellViewInfo,
    assert_cell_has_no_open_views,
    assert_cell_has_no_open_windows,
    open_cell_views,
    virtuoso_pid,
    virtuoso_workdir,
)


class OperationPolicy(str, Enum):
    READ_ONLY = "read-only"
    DIRECT_MUTATION = "direct-mutation"
    RECURSIVE_OA = "recursive-oa"
    MAESTRO_RUN = "maestro-run"
    GUI_ACTION = "gui-action"


@dataclass(frozen=True, order=True)
class OpenViewKey:
    library: str
    cell: str
    view: str


def _view_key(info: OpenCellViewInfo) -> OpenViewKey:
    return OpenViewKey(info.library, info.cell, info.view)


@dataclass
class DeferredCommit:
    callback: Callable[[], Any]
    on_failure: Callable[[BaseException], None] | None
    result: Any = None
    completed: bool = False
    aborted: bool = False


@dataclass
class WorkspaceOperation:
    client: Any
    root: Path
    name: str
    policy: OperationPolicy = OperationPolicy.DIRECT_MUTATION
    _workspace_capability: _WorkspaceCapability = field(repr=False, kw_only=True)
    _root_identity: tuple[int, int] = field(repr=False, kw_only=True)
    operation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    uncertain_reason: str | None = None
    _uncertain_reasons: list[str] = field(default_factory=list, repr=False)
    incident_path: Path | None = None
    _leases: list["ViewLease"] = field(default_factory=list, repr=False)
    _mutation_scopes: list["OaMutationScope"] = field(default_factory=list, repr=False)
    _view_snapshots: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _ownership_scopes: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _deferred_commits: list[DeferredCommit] = field(default_factory=list, repr=False)
    _artifact_records: list[Any] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        validate_artifact_id(self.operation_id, "workspace operation id")
        _bind_workspace_capability(self._workspace_capability, self)

    def require_root_identity(self) -> None:
        metadata = self.root.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or (
            metadata.st_dev,
            metadata.st_ino,
        ) != self._root_identity:
            raise RuntimeError(
                f"Virtuoso workspace root identity changed during {self.name}: "
                f"{self.root}"
            )

    def mark_uncertain(self, reason: str) -> None:
        if not reason:
            raise ValueError("uncertain workspace state requires a reason")
        if self.uncertain_reason and not self._uncertain_reasons:
            self._uncertain_reasons.append(self.uncertain_reason)
        if reason not in self._uncertain_reasons:
            self._uncertain_reasons.append(reason)
        self.uncertain_reason = " | ".join(self._uncertain_reasons)

    def defer_commit(
        self,
        callback: Callable[[], Any],
        *,
        on_failure: Callable[[BaseException], None] | None = None,
    ) -> DeferredCommit:
        commit = DeferredCommit(callback=callback, on_failure=on_failure)
        self._deferred_commits.append(commit)
        return commit

    def register_artifact(self, record: Any) -> None:
        """Associate one persisted execution so any incident is back-linked."""

        if self._artifact_records:
            raise RuntimeError(
                "one workspace operation may own only one execution record"
            )
        record.bind_operation(self.operation_id)
        self._artifact_records.append(record)

    def abort_deferred_commits(self, error: BaseException) -> None:
        for commit in self._deferred_commits:
            if commit.completed or commit.aborted:
                continue
            commit.aborted = True
            if commit.on_failure is None:
                continue
            try:
                commit.on_failure(error)
            except Exception as abort_error:
                error.add_note(f"deferred failure record failed: {abort_error}")

    def commit_deferred(self) -> None:
        if self._leases:
            raise RuntimeError("cannot commit while a view lease is still active")
        if self._mutation_scopes:
            raise RuntimeError("cannot commit while an OA mutation scope is still active")
        for commit in self._deferred_commits:
            if commit.aborted:
                raise RuntimeError("cannot commit an aborted workspace transaction")
            if commit.completed:
                continue
            try:
                commit.result = commit.callback()
                commit.completed = True
            except BaseException as error:
                self.abort_deferred_commits(error)
                raise

    def has_active_view_lease(
        self,
        library: str,
        *,
        cell: str | None = None,
        view: str | None = None,
    ) -> bool:
        if view is not None and cell is None:
            raise ValueError("an exact view lease check requires a cell")
        return any(
            lease.active
            and lease.library == library
            and (
                (
                    view is None
                    and lease.views is None
                    and (cell is None or lease.cells is None or cell in lease.cells)
                )
                or (
                    view is not None
                    and (
                        (lease.views is not None and (cell, view) in lease.views)
                        or (
                            lease.views is None
                            and (lease.cells is None or cell in lease.cells)
                        )
                    )
                )
            )
            for lease in self._leases
        )

    def has_active_library_view_lease(self, library: str) -> bool:
        return any(
            lease.active and lease.library == library and lease.cells is None
            for lease in self._leases
        )

    def record_ownership_scope(self, kind: str, **identity: Any) -> None:
        if not kind or not identity:
            raise ValueError("ownership evidence requires a kind and exact identity")
        self._ownership_scopes.append({"kind": kind, **identity})

    def require_project_library_target(self, client: Any, library: str) -> Path:
        """Bind a non-lease action such as GUI mutation to one project library."""

        if client is not self.client:
            raise RuntimeError("workspace target belongs to a different bridge client")
        return require_project_library_path(self, library)

    def require_project_file_target(
        self,
        client: Any,
        path: Path,
        *,
        label: str,
    ) -> Path:
        """Prove a project file target is contained, regular, and symlink-free."""

        if client is not self.client:
            raise RuntimeError("workspace target belongs to a different bridge client")
        candidate = _lexical_project_path(
            self,
            path,
            label=label,
            require_directory=False,
        )
        metadata = candidate.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError(f"{label} is not a single-link regular file: {candidate}")
        return candidate

    def ensure_project_directory_target(
        self,
        client: Any,
        path: Path,
        *,
        label: str,
    ) -> Path:
        """Create/open one contained directory chain through nofollow dirfds."""

        if client is not self.client:
            raise RuntimeError("workspace target belongs to a different bridge client")
        candidate = _lexical_project_path(
            self,
            path,
            label=label,
            require_directory=False,
        )
        descriptor = os.open(
            self.root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            root_metadata = os.fstat(descriptor)
            if (root_metadata.st_dev, root_metadata.st_ino) != self._root_identity:
                raise RuntimeError("workspace root identity changed before mkdir")
            for component in candidate.relative_to(self.root).parts:
                try:
                    next_descriptor = os.open(
                        component,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | os.O_CLOEXEC
                        | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    os.mkdir(component, mode=0o755, dir_fd=descriptor)
                    next_descriptor = os.open(
                        component,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | os.O_CLOEXEC
                        | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                os.close(descriptor)
                descriptor = next_descriptor
            final_metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(final_metadata.st_mode):
                raise RuntimeError(f"{label} is not a directory: {candidate}")
        finally:
            os.close(descriptor)
        return candidate

    def mutation_scope(
        self,
        library: str,
        *,
        cells: tuple[str, ...] | None,
        views: tuple[tuple[str, str], ...] | None = None,
        expected_deleted_cells: tuple[str, ...] = (),
        phase: str,
        expected_library_path: Path | None = None,
        quarantine_root: Path | None = None,
        allow_current_config_lock: bool = False,
        require_view_lease: bool = True,
    ) -> Iterator["OaMutationScope"]:
        return oa_mutation_scope(
            self,
            library,
            cells=cells,
            views=views,
            expected_deleted_cells=expected_deleted_cells,
            phase=phase,
            expected_library_path=expected_library_path,
            quarantine_root=quarantine_root,
            allow_current_config_lock=allow_current_config_lock,
            require_view_lease=require_view_lease,
        )

    def require_mutation_target(
        self,
        client: Any,
        library: str,
        cell: str | None,
        *,
        phase: str,
    ) -> "OaMutationScope":
        if client is not self.client:
            raise RuntimeError("mutation scope belongs to a different bridge client")
        matches = [
            scope
            for scope in self._mutation_scopes
            if scope.active
            and scope.library == library
            and (
                (cell is None and scope.cells is None)
                or (cell is not None and scope.cells is not None and cell in scope.cells)
            )
        ]
        if len(matches) != 1:
            target = f"{library}/{cell}" if cell is not None else library
            raise RuntimeError(
                f"no unique active OA mutation scope authorizes {target} during {phase}"
            )
        return matches[0]

    def require_active_mutation(
        self,
        client: Any,
        library: str,
        cell: str | None,
        *,
        phase: str,
    ) -> "OaMutationScope":
        scope = self.require_mutation_target(
            client,
            library,
            cell,
            phase=phase,
        )
        scope.revalidate(f"dispatch:{phase}")
        scope.mutation_started = True
        return scope

    def record_view_snapshot(
        self,
        phase: str,
        library: str,
        cells: frozenset[str] | None,
        views: tuple[OpenCellViewInfo, ...],
    ) -> None:
        self._view_snapshots.append(
            {
                "phase": phase,
                "library": library,
                "cells": sorted(cells) if cells is not None else None,
                "views": [
                    {
                        "library": view.library,
                        "cell": view.cell,
                        "view": view.view,
                        "mode": view.mode,
                        "visible": view.visible,
                        "identity": view.identity,
                    }
                    for view in views
                ],
            }
        )

    def record_incident(self, error: BaseException) -> None:
        if self.incident_path is not None:
            return
        if not self._artifact_records:
            # An operation incident is not a free-standing log: the artifact
            # contract requires an owning persisted execution.  Artifactless
            # interactive/read operations report their exception directly.
            return
        self.require_root_identity()
        status = "uncertain" if self.uncertain_reason is not None else "failed"
        record = self._artifact_records[0]
        incident = write_operation_incident(
            workspace_root=self.root,
            artifact_root=record.paths.artifact_root,
            operation_id=self.operation_id,
            name=self.name,
            policy=self.policy.value,
            status=status,
            error=error,
            uncertain_reason=self.uncertain_reason,
            view_snapshots=self._view_snapshots,
            ownership_scopes=self._ownership_scopes,
        )
        try:
            record.attach_incident(incident)
        except Exception:
            # Roll back a just-created, still-unreferenced journal entry rather
            # than leave an orphan that no manifest can discover.
            try:
                rollback_unreferenced_operation_incident(
                    artifact_root=record.paths.artifact_root,
                    operation_id=self.operation_id,
                    incident_path=incident,
                )
            except Exception as rollback_error:
                error.add_note(
                    "could not safely roll back unreferenced operation incident: "
                    f"{rollback_error}"
                )
            raise
        self.incident_path = incident

    def view_lease(
        self,
        library: str,
        *,
        cells: tuple[str, ...] | None = None,
        views: tuple[tuple[str, str], ...] | None = None,
        require_quiescent: bool = True,
    ) -> Iterator["ViewLease"]:
        return view_lease(
            self,
            library,
            cells=cells,
            views=views,
            require_quiescent=require_quiescent,
        )

@dataclass
class ViewLease:
    operation: WorkspaceOperation
    library: str
    cells: frozenset[str] | None
    views: frozenset[tuple[str, str]] | None
    require_quiescent: bool
    before: dict[OpenViewKey, tuple[OpenCellViewInfo, ...]]
    active: bool = True

    def _snapshot(
        self,
        phase: str,
    ) -> dict[OpenViewKey, tuple[OpenCellViewInfo, ...]]:
        # Mutation APIs can implicitly open masters in analogLib, basic, or a
        # PDK library.  Track the whole process even though write eligibility
        # remains scoped to this lease's project library/cells.
        views = open_cell_views(self.operation.client)
        self.operation.record_view_snapshot(phase, self.library, self.cells, views)
        grouped: dict[OpenViewKey, list[OpenCellViewInfo]] = {}
        for info in views:
            grouped.setdefault(_view_key(info), []).append(info)
        return {key: tuple(states) for key, states in grouped.items()}

    def _state_change_error(
        self,
        current: dict[OpenViewKey, tuple[OpenCellViewInfo, ...]],
        *,
        phase: str | None = None,
    ) -> RuntimeError:
        changed = sorted(set(current).union(self.before))
        details = ", ".join(
            f"{key.library}/{key.cell}/{key.view}:"
            f"{len(self.before.get(key, ()))}->{len(current.get(key, ()))}"
            for key in changed
        )
        stage = f" during {phase}" if phase else ""
        return RuntimeError(
            f"operation {self.operation.name} changed shared Virtuoso "
            f"open-view state{stage} ({details}); no handles were closed because "
            "name-based ownership is not proof"
        )

    def checkpoint(self, phase: str) -> None:
        """Fail at the first phase that changes shared open-view state."""

        if not self.active:
            raise RuntimeError("cannot checkpoint an inactive view lease")
        if not phase:
            raise ValueError("view checkpoint requires a phase")
        if self.operation.uncertain_reason is not None:
            raise RuntimeError(
                f"cannot checkpoint uncertain operation {self.operation.name}: "
                f"{self.operation.uncertain_reason}"
            )
        try:
            current = self._snapshot(f"checkpoint:{phase}")
        except BaseException as error:
            self.operation.mark_uncertain(
                f"could not inventory open views during {phase}: "
                f"{type(error).__name__}: {error}"
            )
            raise
        if current != self.before:
            error = self._state_change_error(current, phase=phase)
            self.operation.mark_uncertain(str(error))
            raise error

    def reconcile(self) -> None:
        if not self.active:
            raise RuntimeError("view lease was already reconciled")
        try:
            if self.operation.uncertain_reason is not None:
                return
            try:
                after = self._snapshot("after")
            except BaseException as error:
                self.operation.mark_uncertain(
                    f"could not inventory open views after {self.operation.name}: "
                    f"{type(error).__name__}: {error}"
                )
                raise
            if after != self.before:
                error = self._state_change_error(after)
                self.operation.mark_uncertain(str(error))
                raise error
            try:
                final = self._snapshot("final")
            except BaseException as error:
                self.operation.mark_uncertain(
                    f"could not inventory open views during final audit for "
                    f"{self.operation.name}: {type(error).__name__}: {error}"
                )
                raise
            if final != self.before:
                error = RuntimeError(
                    f"operation {self.operation.name} did not restore its open-view state"
                )
                self.operation.mark_uncertain(str(error))
                raise error
        finally:
            self.active = False
            if self in self.operation._leases:
                self.operation._leases.remove(self)


@dataclass
class OaMutationScope:
    """A short-lived, target-specific permit revalidated at every write dispatch."""

    operation: WorkspaceOperation
    library: str
    cells: frozenset[str] | None
    views: frozenset[tuple[str, str]] | None
    expected_deleted_cells: frozenset[str]
    phase: str
    expected_library_path: Path | None
    quarantine_root: Path | None
    allow_current_config_lock: bool
    require_view_lease: bool
    active: bool = True
    mutation_started: bool = False
    library_identity: tuple[int, int] | None = None
    cell_identities: dict[str, tuple[int, int]] = field(default_factory=dict)

    @staticmethod
    def _bind_directory_identity(
        path: Path,
        previous: tuple[int, int] | None,
        *,
        label: str,
    ) -> tuple[int, int] | None:
        try:
            metadata = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            if previous is not None:
                raise RuntimeError(f"{label} disappeared during OA mutation: {path}")
            return None
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(f"{label} is not a directory: {path}")
        current = (metadata.st_dev, metadata.st_ino)
        if previous is not None and current != previous:
            raise RuntimeError(
                f"{label} identity changed during OA mutation: {path}"
            )
        return current

    def revalidate(self, checkpoint: str, *, allow_uncertain: bool = False) -> Path:
        if not self.active:
            raise RuntimeError("cannot revalidate an inactive OA mutation scope")
        if self.operation.uncertain_reason is not None and not allow_uncertain:
            raise RuntimeError(
                f"cannot mutate from uncertain operation {self.operation.name}: "
                f"{self.operation.uncertain_reason}"
            )
        if self.require_view_lease:
            if self.views is not None:
                covered = all(
                    self.operation.has_active_view_lease(
                        self.library,
                        cell=cell,
                        view=view,
                    )
                    for cell, view in self.views
                )
            elif self.cells is None:
                covered = self.operation.has_active_library_view_lease(self.library)
            else:
                covered = all(
                    self.operation.has_active_view_lease(self.library, cell=cell)
                    for cell in self.cells
                )
            if not covered:
                target = (
                    self.library
                    if self.cells is None
                    else (
                        ", ".join(
                            f"{self.library}/{cell}/{view}"
                            for cell, view in sorted(self.views)
                        )
                        if self.views is not None
                        else f"{self.library}/{{{', '.join(sorted(self.cells))}}}"
                    )
                )
                raise RuntimeError(
                    f"OA mutation scope {self.phase} requires an active view lease "
                    f"covering exact target {target}"
                )
        library_path = require_project_library_location(
            self.operation,
            self.library,
            expected_path=self.expected_library_path,
        )
        self.library_identity = self._bind_directory_identity(
            library_path,
            self.library_identity,
            label=f"OA library {self.library}",
        )
        quarantine = self.quarantine_root if checkpoint == "entry" else None
        if self.cells is None:
            views = open_cell_views(self.operation.client, library=self.library)
            if views:
                raise RuntimeError(
                    f"refusing {self.phase}: library {self.library} has open views"
                )
            if library_path.is_dir():
                for candidate in sorted(library_path.iterdir()):
                    if candidate.is_symlink():
                        raise RuntimeError(
                            f"refusing symbolic-link OA cell path: {candidate}"
                        )
                    if candidate.is_dir():
                        require_clean_oa_cell(
                            candidate,
                            quarantine_root=quarantine,
                            allowed_config_owner_pid=virtuoso_pid(
                                self.operation.client
                            )
                            if self.allow_current_config_lock
                            else None,
                        )
        else:
            for cell in sorted(self.cells):
                cell_dir = _lexical_project_path(
                    self.operation,
                    library_path / cell,
                    label=f"OA cell path {self.library}/{cell}",
                    require_directory=False,
                )
                if cell in self.expected_deleted_cells:
                    exists = cell_dir.exists()
                    if checkpoint == "entry" and not exists:
                        raise RuntimeError(
                            f"expected OA deletion target does not exist: "
                            f"{self.library}/{cell}"
                        )
                    if checkpoint == "completion":
                        if exists:
                            raise RuntimeError(
                                f"expected deleted OA cell still exists: "
                                f"{self.library}/{cell}"
                            )
                        continue
                    if not exists:
                        # A multi-cell deletion dispatch is intentionally
                        # sequential.  Previously confirmed targets may be
                        # absent when the next exact target is revalidated.
                        continue
                cell_dir = require_quiescent_project_cell(
                    self.operation,
                    self.library,
                    cell,
                    allow_current_config_lock=self.allow_current_config_lock,
                    quarantine_root=quarantine,
                )
                identity = self._bind_directory_identity(
                    cell_dir,
                    self.cell_identities.get(cell),
                    label=f"OA cell {self.library}/{cell}",
                )
                if identity is not None:
                    self.cell_identities[cell] = identity
        self.operation.record_view_snapshot(
            f"mutation:{self.phase}:{checkpoint}",
            self.library,
            self.cells,
            open_cell_views(self.operation.client),
        )
        return library_path


@contextmanager
def oa_mutation_scope(
    operation: WorkspaceOperation,
    library: str,
    *,
    cells: tuple[str, ...] | None,
    views: tuple[tuple[str, str], ...] | None = None,
    expected_deleted_cells: tuple[str, ...] = (),
    phase: str,
    expected_library_path: Path | None = None,
    quarantine_root: Path | None = None,
    allow_current_config_lock: bool = False,
    require_view_lease: bool = True,
) -> Iterator[OaMutationScope]:
    """Authorize one narrowly scoped OA mutation with before/after audits."""

    if not phase:
        raise ValueError("OA mutation scope requires a phase")
    require_workspace_capability(
        operation,
        operation.client,
        authority=WorkspaceAuthority.OA_MUTATION,
    )
    cell_scope = frozenset(cells) if cells is not None else None
    if cell_scope is not None and (not cell_scope or len(cell_scope) != len(cells)):
        raise ValueError("OA mutation cells must be non-empty and unique")
    view_scope = frozenset(views) if views is not None else None
    if view_scope is not None:
        if cell_scope is None or not view_scope:
            raise ValueError("exact OA mutation views require a non-empty cell scope")
        if len(view_scope) != len(views):
            raise ValueError("exact OA mutation views must be unique")
        if any(
            not cell or not view or cell not in cell_scope
            for cell, view in view_scope
        ):
            raise ValueError(
                "exact OA mutation views must stay inside the cell scope"
            )
    deleted_scope = frozenset(expected_deleted_cells)
    if len(deleted_scope) != len(expected_deleted_cells):
        raise ValueError("expected deleted OA cells must be unique")
    if deleted_scope and (cell_scope is None or not deleted_scope.issubset(cell_scope)):
        raise ValueError(
            "expected deleted OA cells must be contained in the exact cell scope"
        )
    scope = OaMutationScope(
        operation=operation,
        library=library,
        cells=cell_scope,
        views=view_scope,
        expected_deleted_cells=deleted_scope,
        phase=phase,
        expected_library_path=expected_library_path,
        quarantine_root=quarantine_root,
        allow_current_config_lock=allow_current_config_lock,
        require_view_lease=require_view_lease,
    )
    for existing in operation._mutation_scopes:
        if existing.active:
            raise RuntimeError(
                f"nested OA mutation scopes are forbidden: {existing.phase} -> {phase}"
            )
    scope.revalidate("entry")
    operation._mutation_scopes.append(scope)
    try:
        yield scope
    except BaseException as body_error:
        if scope.mutation_started:
            operation.mark_uncertain(
                f"OA mutation {phase} raised after write dispatch: "
                f"{type(body_error).__name__}: {body_error}"
            )
            try:
                scope.revalidate("failure-audit", allow_uncertain=True)
            except BaseException as audit_error:
                operation.mark_uncertain(
                    f"OA mutation {phase} failed and its target state could not be "
                    f"reconciled: {type(audit_error).__name__}: {audit_error}"
                )
                body_error.add_note(str(audit_error))
        raise
    else:
        try:
            scope.revalidate("completion")
        except BaseException as audit_error:
            if scope.mutation_started:
                operation.mark_uncertain(
                    f"OA mutation {phase} completed without a clean target audit: "
                    f"{type(audit_error).__name__}: {audit_error}"
                )
            raise
    finally:
        scope.active = False
        if scope in operation._mutation_scopes:
            operation._mutation_scopes.remove(scope)


@contextmanager
def view_lease(
    operation: WorkspaceOperation,
    library: str,
    *,
    cells: tuple[str, ...] | None = None,
    views: tuple[tuple[str, str], ...] | None = None,
    require_quiescent: bool = True,
) -> Iterator[ViewLease]:
    """Prove view ownership from an exact before/after inventory."""

    require_project_library_path(operation, library)
    cell_scope = frozenset(cells) if cells is not None else None
    view_scope = frozenset(views) if views is not None else None
    if view_scope is not None:
        if cell_scope is None or not view_scope:
            raise ValueError("exact view leases require a non-empty cell scope")
        if len(view_scope) != len(views):
            raise ValueError("exact view lease targets must be unique")
        if any(not cell or not view or cell not in cell_scope for cell, view in view_scope):
            raise ValueError("exact view lease targets must stay inside the cell scope")
    before_views = open_cell_views(operation.client)
    operation.record_view_snapshot("before", library, cell_scope, before_views)
    grouped_before: dict[OpenViewKey, list[OpenCellViewInfo]] = {}
    for info in before_views:
        grouped_before.setdefault(_view_key(info), []).append(info)
    before = {key: tuple(states) for key, states in grouped_before.items()}
    target_before = {
        key: states
        for key, states in before.items()
        if key.library == library
        and (
            (view_scope is not None and (key.cell, key.view) in view_scope)
            or (
                view_scope is None
                and (cell_scope is None or key.cell in cell_scope)
            )
        )
    }
    if require_quiescent and target_before:
        details = ", ".join(
            f"{key.library}/{key.cell}/{key.view}:"
            + "/".join(state.mode for state in states)
            for key, states in sorted(target_before.items())
        )
        raise RuntimeError(
            f"refusing {operation.name}: view lease requires quiescent state; {details}"
        )
    for existing in operation._leases:
        overlaps = (
            existing.active
            and existing.library == library
            and (
                existing.cells is None
                or cell_scope is None
                or bool(existing.cells.intersection(cell_scope))
            )
        )
        if overlaps:
            raise RuntimeError(
                f"refusing overlapping active view leases for library {library}"
            )
    lease = ViewLease(
        operation=operation,
        library=library,
        cells=cell_scope,
        views=view_scope,
        require_quiescent=require_quiescent,
        before=before,
    )
    operation._leases.append(lease)
    try:
        yield lease
    except BaseException as body_error:
        try:
            lease.reconcile()
        except Exception as cleanup_error:
            raise RuntimeError(
                f"{body_error}; view-lease cleanup also failed: {cleanup_error}"
            ) from body_error
        raise
    else:
        lease.reconcile()


def _lexical_project_path(
    operation: WorkspaceOperation,
    raw_path: str | os.PathLike[str],
    *,
    label: str,
    require_directory: bool,
) -> Path:
    """Return one contained path only after lstat rejects every symlink component."""

    operation.require_root_identity()
    raw = Path(raw_path)
    candidate = Path(
        os.path.abspath(raw if raw.is_absolute() else operation.root / raw)
    )
    if candidate == operation.root or not candidate.is_relative_to(operation.root):
        raise RuntimeError(
            f"refusing {operation.name}: {label} leaves the project workspace "
            f"({candidate})"
        )
    current = operation.root
    missing = False
    for component in candidate.relative_to(operation.root).parts:
        current = current / component
        if missing:
            continue
        try:
            metadata = current.stat(follow_symlinks=False)
        except FileNotFoundError:
            missing = True
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError(f"refusing symbolic-link {label}: {current}")
        if current != candidate and not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(f"non-directory component in {label}: {current}")
    if require_directory:
        try:
            metadata = candidate.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise RuntimeError(f"{label} does not exist: {candidate}") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(f"{label} is not a directory: {candidate}")
    return candidate


def require_project_library_path(
    operation: WorkspaceOperation,
    library: str,
) -> Path:
    """Resolve a library and refuse modification outside this workspace."""

    info = operation.client.library.get(library, timeout=30)
    return _lexical_project_path(
        operation,
        str(info.path),
        label=f"library {library}",
        require_directory=True,
    )


def require_project_library_location(
    operation: WorkspaceOperation,
    library: str,
    *,
    expected_path: Path | None = None,
) -> Path:
    """Prove both the expected filesystem target and any registered library path."""

    if expected_path is None:
        return require_project_library_path(operation, library)
    candidate = _lexical_project_path(
        operation,
        expected_path,
        label=f"expected library path for {library}",
        require_directory=False,
    )
    visible = operation.client.library.list(timeout=30)
    if library in visible:
        registered = require_project_library_path(operation, library)
        if registered != candidate:
            raise RuntimeError(
                f"library {library} resolves to {registered}, expected {candidate}"
            )
    return candidate


def require_quiescent_project_cell(
    operation: WorkspaceOperation,
    library: str,
    cell: str,
    *,
    allow_current_config_lock: bool = False,
    quarantine_root: Path | None = None,
) -> Path:
    """Apply the common lock/open-view policy before modifying a cell."""

    client = operation.client
    library_path = require_project_library_path(operation, library)
    cell_dir = _lexical_project_path(
        operation,
        library_path / cell,
        label=f"OA cell path {library}/{cell}",
        require_directory=False,
    )
    if cell_dir == library_path or not cell_dir.is_relative_to(library_path):
        raise RuntimeError(
            f"refusing cell path outside project library {library}: {cell_dir}"
        )
    assert_cell_has_no_open_windows(client, library, cell)
    assert_cell_has_no_open_views(client, library, cell)
    require_clean_oa_cell(
        cell_dir,
        quarantine_root=quarantine_root,
        allowed_config_owner_pid=virtuoso_pid(client)
        if allow_current_config_lock
        else None,
    )
    return cell_dir


@contextmanager
def workspace_operation(
    client: Any,
    root: Path,
    name: str,
    *,
    policy: OperationPolicy = OperationPolicy.DIRECT_MUTATION,
    acquire_flow_lock: bool = True,
    record_incident: bool = True,
    operation_id: str | None = None,
) -> Iterator[WorkspaceOperation]:
    """Require local, correctly rooted, session-free automation.

    A read-only inventory such as ``flow oa check`` may set
    ``acquire_flow_lock=False``.  That mode is deliberately restricted to the
    read-only policy so the diagnostic command cannot alter the shared lock
    file or accidentally become a write path.  Such diagnostics may also set
    ``record_incident=False`` so an error report cannot mutate the artifact
    journal.
    """

    if not record_incident and policy is not OperationPolicy.READ_ONLY:
        raise ValueError("incident suppression is restricted to read-only operations")
    if not acquire_flow_lock and policy is not OperationPolicy.READ_ONLY:
        raise ValueError("only read-only workspace operations may skip the flow lock")

    if getattr(client, "ssh_runner", None) is not None:
        raise RuntimeError(
            f"refusing remote {name}: remote OA locking and path safety are not implemented"
        )
    expected = root.resolve()
    actual = virtuoso_workdir(client)
    if actual != expected:
        raise RuntimeError(
            f"refusing {name}: Virtuoso workdir is {actual}, expected {expected}"
        )
    lock_scope = (
        exclusive_flow_operation(expected, name)
        if acquire_flow_lock
        else nullcontext()
    )
    with lock_scope:
        assert_no_active_maestro_sessions(client, name)
        root_metadata = expected.stat(follow_symlinks=False)
        capability = _issue_workspace_capability(client)
        operation = WorkspaceOperation(
            client=client,
            root=expected,
            name=name,
            policy=policy,
            _workspace_capability=capability,
            _root_identity=(root_metadata.st_dev, root_metadata.st_ino),
            operation_id=(
                uuid.uuid4().hex
                if operation_id is None
                else validate_artifact_id(operation_id, "workspace operation id")
            ),
        )
        try:
            try:
                yield operation
            except BaseException as body_error:
                cleanup_reason = process_group_cleanup_uncertainty(body_error)
                if cleanup_reason is not None:
                    operation.mark_uncertain(
                        "external process-group cleanup could not be proven: "
                        + cleanup_reason
                    )
                final_error: BaseException = body_error
                if operation.uncertain_reason is None:
                    try:
                        operation.require_root_identity()
                        assert_no_active_maestro_sessions(client, f"finish {name}")
                    except Exception as audit_error:
                        operation.mark_uncertain(str(audit_error))
                        final_error = RuntimeError(
                            f"{body_error}; workspace exit audit also failed: {audit_error}"
                        )
                # Failure callbacks run only after the final workspace audit so
                # manifests see the operation's authoritative uncertainty state.
                operation.abort_deferred_commits(final_error)
                if record_incident:
                    try:
                        operation.record_incident(final_error)
                    except Exception as journal_error:
                        final_error.add_note(
                            f"could not record Virtuoso incident: {journal_error}"
                        )
                if final_error is not body_error:
                    raise final_error from body_error
                raise
            else:
                if operation.uncertain_reason is not None:
                    error = RuntimeError(
                        f"operation {name} ended with uncertain state: "
                        f"{operation.uncertain_reason}"
                    )
                    operation.abort_deferred_commits(error)
                    if record_incident:
                        try:
                            operation.record_incident(error)
                        except Exception as journal_error:
                            error.add_note(
                                f"could not record Virtuoso incident: {journal_error}"
                            )
                    raise error
                try:
                    operation.require_root_identity()
                    assert_no_active_maestro_sessions(client, f"finish {name}")
                except Exception as audit_error:
                    operation.mark_uncertain(str(audit_error))
                    operation.abort_deferred_commits(audit_error)
                    if record_incident:
                        try:
                            operation.record_incident(audit_error)
                        except Exception as journal_error:
                            audit_error.add_note(
                                f"could not record Virtuoso incident: {journal_error}"
                            )
                    raise
                try:
                    operation.commit_deferred()
                except BaseException as commit_error:
                    if record_incident:
                        try:
                            operation.record_incident(commit_error)
                        except Exception as journal_error:
                            commit_error.add_note(
                                f"could not record Virtuoso incident: {journal_error}"
                            )
                    raise
        finally:
            _revoke_workspace_capability(capability)
