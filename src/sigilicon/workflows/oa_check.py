"""Read-only checks for the current source-derived OA workspace."""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
from typing import Any

from sigilicon.project import Project
from sigilicon.virtuoso.locks import discover_oa_locks, inspect_flow_operation_lock
from sigilicon.virtuoso.maestro import active_maestro_sessions
from sigilicon.virtuoso.oa import open_cell_views, virtuoso_pid, virtuoso_workdir
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation
from sigilicon.workflows.oa_library import (
    OALibraryRebuildPlan,
    check_oa_parity,
    plan_oa_library_rebuild,
)


def _exception(error: BaseException) -> dict[str, str]:
    return {"type": type(error).__name__, "message": str(error)}


class UnavailableBridge:
    """Client-shaped read-only failure used when Bridge is unavailable."""

    def __init__(self, error: BaseException | str) -> None:
        self.error = str(error)

    def execute_skill(self, source: str, *, timeout: int = 300) -> Any:
        del source, timeout
        raise RuntimeError(self.error)


def _view_dict(value: Any) -> dict[str, Any]:
    return {
        "library": value.library,
        "cell": value.cell,
        "view": value.view,
        "mode": value.mode,
        "visible": value.visible,
        "identity": value.identity,
    }


def _process_state(client: Any) -> dict[str, Any]:
    try:
        pid = virtuoso_pid(client)
        alive = True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
        command = None
        cmdline = Path(f"/proc/{pid}/cmdline")
        if cmdline.is_file():
            command = cmdline.read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            ).strip()
        return {"pid": pid, "alive": alive, "cmdline": command}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"error": _exception(exc)}


def _bridge_state(client: Any) -> dict[str, Any]:
    state: dict[str, Any] = {}
    try:
        state["active_maestro_sessions"] = list(active_maestro_sessions(client))
    except (OSError, RuntimeError, ValueError) as exc:
        state["active_maestro_sessions_error"] = _exception(exc)
    try:
        state["open_cell_views"] = [_view_dict(view) for view in open_cell_views(client)]
    except (OSError, RuntimeError, ValueError) as exc:
        state["open_cell_views_error"] = _exception(exc)
    try:
        state["workdir"] = str(virtuoso_workdir(client))
    except (OSError, RuntimeError, ValueError) as exc:
        state["workdir_error"] = _exception(exc)
    state["process"] = _process_state(client)
    return state


def _ownership(plan: OALibraryRebuildPlan) -> dict[str, Any]:
    owners: dict[str, list[str]] = {}
    conflicts: list[str] = []
    for source in plan.source.source_roots:
        owners.setdefault(source.owner, []).extend(cell.cell for cell in source.cells)
    for owner, cells in owners.items():
        duplicates = sorted(cell for cell in set(cells) if cells.count(cell) > 1)
        conflicts.extend(f"{owner}/{cell}" for cell in duplicates)
    project = plan.source.project
    unmanaged_consumed = any(
        project.owner_for(path) is None
        for source in plan.source.source_roots
        for path in (source.directory, *source.cell_roots)
    )
    return {
        "active_ip_owners": {
            owner: sorted(set(cells)) for owner, cells in sorted(owners.items())
        },
        "conflicts": sorted(set(conflicts)),
        "target_library": plan.library,
        "unmanaged_consumed": unmanaged_consumed,
    }


def _library_ownership(
    plan: OALibraryRebuildPlan,
    client: Any,
) -> dict[str, Any]:
    """Prove that the live library resolves to the manifest-owned OA path."""

    expected = plan.source.oa_library.resolve()
    project = plan.source.project
    try:
        with workspace_operation(
            client,
            project.workspace_root,
            "check-oa-library-ownership",
            policy=OperationPolicy.READ_ONLY,
            acquire_flow_lock=False,
            record_incident=False,
        ) as operation:
            registered = operation.require_project_library_target(client, plan.library)
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "passed": False,
            "status": "uncertain",
            "expected_path": str(expected),
            "error": _exception(exc),
        }
    if registered != expected:
        return {
            "passed": False,
            "status": "blocked",
            "expected_path": str(expected),
            "registered_path": str(registered),
            "error": {
                "type": "RuntimeError",
                "message": (
                    f"library {plan.library} resolves to {registered}, "
                    f"expected {expected}"
                ),
            },
        }
    return {
        "passed": True,
        "status": "clean",
        "expected_path": str(expected),
        "registered_path": str(registered),
    }


def _locks(plan: OALibraryRebuildPlan) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    root = plan.source.oa_library
    expected = {
        (cell, view)
        for cell, views in plan.expected_views.items()
        for view in views
    }
    directories: list[tuple[str, str, Path]] = []
    try:
        if root.is_symlink():
            raise RuntimeError(f"refusing symbolic-link OA library path: {root}")
        if root.is_dir():
            for cell_dir in sorted(root.iterdir()):
                if cell_dir.is_symlink():
                    errors.append(
                        {
                            "path": str(cell_dir),
                            "error": {
                                "type": "RuntimeError",
                                "message": "refusing symbolic-link OA cell path",
                            },
                        }
                    )
                    continue
                if not cell_dir.is_dir():
                    continue
                for view_dir in sorted(cell_dir.iterdir()):
                    if view_dir.is_symlink():
                        errors.append(
                            {
                                "cell": cell_dir.name,
                                "view": view_dir.name,
                                "path": str(view_dir),
                                "error": {
                                    "type": "RuntimeError",
                                    "message": "refusing symbolic-link OA view path",
                                },
                            }
                        )
                        continue
                    if view_dir.is_dir():
                        directories.append((cell_dir.name, view_dir.name, view_dir))
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append({"path": str(root), "error": _exception(exc)})

    for cell, view, directory in directories:
        try:
            for lock in discover_oa_locks(directory):
                alive = None
                if lock.pid is not None:
                    try:
                        os.kill(lock.pid, 0)
                        alive = True
                    except ProcessLookupError:
                        alive = False
                    except PermissionError:
                        alive = True
                rows.append(
                    {
                        "path": str(lock.path),
                        "cell": cell,
                        "view": view,
                        "declared": (cell, view) in expected,
                        "host": lock.host,
                        "pid": lock.pid,
                        "owner_alive": alive,
                    }
                )
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append({"cell": cell, "view": view, "error": _exception(exc)})
    flow_lock_root = plan.source.workspace_template
    try:
        flow_lock = inspect_flow_operation_lock(flow_lock_root)
    except (OSError, RuntimeError, ValueError) as exc:
        flow_lock = {
            "path": str(flow_lock_root / ".flow-operation.lock"),
            "exists": True,
            "error": _exception(exc),
            "inspected_without_write": True,
        }
    return {
        "edit_locks": rows,
        "errors": errors,
        "flow_operation_lock": flow_lock,
    }


def _recommendation(
    *,
    plan_error: BaseException | None,
    parity: Mapping[str, Any],
    ownership: Mapping[str, Any],
    bridge: Mapping[str, Any],
    locks: Mapping[str, Any],
) -> str:
    if plan_error or ownership.get("conflicts") or ownership.get("unmanaged_consumed"):
        return "blocked"
    library_ownership = ownership.get("library")
    if isinstance(library_ownership, Mapping):
        if library_ownership.get("status") == "blocked":
            return "blocked"
        if library_ownership.get("passed") is not True:
            return "uncertain"
    bridge_errors = [key for key in bridge if key.endswith("_error")]
    process = bridge.get("process")
    if isinstance(process, Mapping):
        if process.get("error"):
            bridge_errors.append("process.error")
        elif process.get("alive") is False:
            bridge_errors.append("process.not-alive")
    if bridge_errors:
        return "uncertain"
    if bridge.get("active_maestro_sessions"):
        return "blocked"
    if bridge.get("open_cell_views"):
        return "blocked"
    if locks.get("edit_locks"):
        return "blocked"
    if locks.get("errors"):
        return "uncertain"
    flow_lock = locks.get("flow_operation_lock") or {}
    if flow_lock.get("held"):
        return "blocked"
    if flow_lock.get("error"):
        return "uncertain"
    if parity.get("error"):
        return "uncertain"
    if parity.get("passed") is False:
        return "stale"
    return "clean"


def check_oa_library(
    manifest_path: Path,
    *,
    project: Project,
    library: str | None,
    client: Any,
    timeout: int = 300,
) -> dict[str, Any]:
    """Check only current source/OA parity and live safety state.

    Native setup semantic attestation is intentionally not part of this
    command.  Use ``sigilicon oa attest --testbench ...`` when one testbench needs
    the Cadence API-level check.
    """

    plan: OALibraryRebuildPlan | None = None
    plan_error: BaseException | None = None
    try:
        plan = plan_oa_library_rebuild(
            manifest_path,
            project=project,
            library=library,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        plan_error = exc

    bridge = _bridge_state(client)
    if plan is None:
        return {
            "passed": False,
            "status": "blocked",
            "source_contract": {"passed": False, "error": _exception(plan_error)},
            "ownership": {},
            "parity": {"passed": False, "error": "plan failed"},
            "live": bridge,
            "locks": {},
        }

    ownership = _ownership(plan)
    bridge_errors = [key for key in bridge if key.endswith("_error")]
    process = bridge.get("process")
    if isinstance(process, Mapping) and process.get("error"):
        bridge_errors.append("process.error")
    ownership["library"] = (
        {
            "passed": False,
            "status": "uncertain",
            "error": {
                "type": "RuntimeError",
                "message": "live library ownership unavailable while Bridge is unavailable",
            },
        }
        if bridge_errors
        else _library_ownership(plan, client)
    )
    thin_contract = all(step.simulation.contract_schema == 3 for step in plan.testbenches)
    source_contract = {
        "passed": thin_contract,
        "manifest": str(manifest_path),
        "library": plan.library,
        "cell_count": len(plan.cells),
        "view_count": len(plan.views),
        "testbench_count": len(plan.testbenches),
        "simulation_schemas": {
            step.cell: step.simulation.contract_schema for step in plan.testbenches
        },
        "native_rdb_contracts": {
            step.cell: step.simulation.native_setup.rdb_contract is not None
            for step in plan.testbenches
        },
        "thin_contract": thin_contract,
        "native_attestation": "explicit-testbench-only",
        "unmanaged_source_roots_consumed": ownership["unmanaged_consumed"],
    }
    try:
        parity = check_oa_parity(
            plan,
            client,
            timeout=timeout,
            acquire_flow_lock=False,
            record_incident=False,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        parity = {"passed": False, "error": _exception(exc)}
    locks = _locks(plan)
    status = _recommendation(
        plan_error=plan_error,
        parity=parity,
        ownership=ownership,
        bridge=bridge,
        locks=locks,
    )
    passed = status == "clean" and source_contract["passed"]
    if not passed and status == "clean":
        status = "stale"
    return {
        "passed": passed,
        "status": status,
        "source_contract": source_contract,
        "ownership": ownership,
        "parity": parity,
        "live": bridge,
        "locks": locks,
    }
