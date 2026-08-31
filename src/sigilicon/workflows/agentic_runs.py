"""Durable control records for authorized agentic Flow execution."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Mapping

from sigilicon.artifacts import (
    atomic_write_json,
    read_json_object,
    write_immutable_text,
)
from sigilicon.canonical import canonical_digest, canonical_json
from sigilicon.flow.model import identifier, owner_identity, run_identity
from sigilicon.identifiers import bounded_identity
from sigilicon.paths import ArtifactExecutionPaths, ProjectContext


AGENTIC_RUN_REQUEST_KIND = "agentic-target-run-request"
AGENTIC_RUN_STATE_KIND = "agentic-target-run-state"
AGENTIC_RUN_AUDIT_KIND = "agentic-target-run-audit"
RUNNING_STATUSES = frozenset({"queued", "running"})
TERMINAL_STATUSES = frozenset(
    {"accepted", "failed", "cancelled", "budget-exhausted", "uncertain"}
)
RUN_STATUSES = RUNNING_STATUSES | TERMINAL_STATUSES
_STATE_FIELDS = {
    "schema",
    "contract_kind",
    "project_id",
    "owner",
    "target",
    "operation",
    "plan_identity",
    "plan_record_json",
    "run_id",
    "grant_identity",
    "grant_json",
    "required_capabilities",
    "budget",
    "status",
    "submitted_at",
    "started_at",
    "finished_at",
    "completed_nodes",
    "total_nodes",
    "current_node",
    "error_code",
}
_REQUEST_FIELDS = _STATE_FIELDS - {
    "status",
    "started_at",
    "finished_at",
    "completed_nodes",
    "current_node",
    "error_code",
} | {
    "principal",
    "role",
    "approval",
    "environment_identity",
    "environment_record_json",
}
_AUDIT_FIELDS = {
    "schema",
    "contract_kind",
    "project_id",
    "owner",
    "target",
    "operation",
    "run_id",
    "plan_identity",
    "grant_identity",
    "principal",
    "role",
    "approval",
    "required_capabilities",
    "budget",
    "environment_identity",
    "submitted_at",
    "started_at",
    "finished_at",
    "terminal_status",
    "result_status",
    "error_code",
}


def _exact(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    missing = fields - set(value)
    unknown = set(value) - fields
    if missing or unknown:
        raise ValueError(
            f"{label} fields do not match: missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )


def _timestamp(value: object, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or "\n" in value:
        raise ValueError(f"{label} must be timestamp text")
    return value


def _canonical_record(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be canonical JSON text")
    try:
        record = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be canonical JSON text") from exc
    if not isinstance(record, dict) or canonical_json(record) != value:
        raise ValueError(f"{label} must be an exact canonical JSON object")
    return record


def _capabilities(value: object, label: str) -> None:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) for item in value)
        or value != sorted(set(value))
        or any(item not in {"execute-derived", "mutate-workspace"} for item in value)
    ):
        raise ValueError(f"{label} capabilities are invalid")


def _budget(value: object, label: str) -> None:
    if not isinstance(value, dict) or set(value) != {"maximum_seconds", "maximum_nodes"}:
        raise ValueError(f"{label} budget is invalid")
    if (
        type(value["maximum_seconds"]) is not int
        or not 1 <= value["maximum_seconds"] <= 86_400
        or type(value["maximum_nodes"]) is not int
        or not 1 <= value["maximum_nodes"] <= 10_000
    ):
        raise ValueError(f"{label} budget is outside supported bounds")


def _validate_common(value: Mapping[str, Any], *, request: bool) -> None:
    _exact(value, _REQUEST_FIELDS if request else _STATE_FIELDS, "agentic run record")
    if value.get("schema") != 1:
        raise ValueError("agentic run record must use schema 1")
    expected_kind = AGENTIC_RUN_REQUEST_KIND if request else AGENTIC_RUN_STATE_KIND
    if value.get("contract_kind") != expected_kind:
        raise ValueError(f"agentic run record kind must be {expected_kind!r}")
    bounded_identity(value.get("project_id"), "agentic project")
    owner_identity(value.get("owner"), "agentic run owner")
    identifier(value.get("target"), "agentic run target")
    identifier(value.get("operation"), "agentic run operation")
    plan_identity = value.get("plan_identity")
    bounded_identity(plan_identity, "agentic Target Operation Plan")
    plan_record = _canonical_record(
        value.get("plan_record_json"),
        "agentic Target Operation Plan record",
    )
    if plan_identity != canonical_digest(plan_record):
        raise ValueError("agentic Target Operation Plan identity drift")
    run_identity(value.get("run_id"))
    bounded_identity(value.get("grant_identity"), "agentic execution grant")
    _canonical_record(value.get("grant_json"), "agentic execution grant record")
    _capabilities(value.get("required_capabilities"), "agentic run")
    _budget(value.get("budget"), "agentic run")
    budget = value["budget"]
    _timestamp(value.get("submitted_at"), "agentic run submission")
    total = value.get("total_nodes")
    if type(total) is not int or total <= 0 or total > budget["maximum_nodes"]:
        raise ValueError("agentic run total node count is invalid")
    if request:
        for field in ("principal", "role", "approval", "environment_identity"):
            identifier(value.get(field), f"agentic run {field}")
        _canonical_record(
            value.get("environment_record_json"),
            "agentic execution environment record",
        )
        return
    status = value.get("status")
    if status not in RUN_STATUSES:
        raise ValueError("agentic run status is invalid")
    _timestamp(value.get("started_at"), "agentic run start", nullable=True)
    _timestamp(value.get("finished_at"), "agentic run finish", nullable=True)
    completed = value.get("completed_nodes")
    if type(completed) is not int or not 0 <= completed <= total:
        raise ValueError("agentic run progress is invalid")
    current = value.get("current_node")
    if current is not None:
        identifier(current, "agentic run current node")
    error = value.get("error_code")
    if error is not None:
        identifier(error, "agentic run error code")
    if status in TERMINAL_STATUSES and value.get("finished_at") is None:
        raise ValueError("terminal agentic run requires a finish timestamp")
    if status in RUNNING_STATUSES and value.get("finished_at") is not None:
        raise ValueError("running agentic run cannot have a finish timestamp")


def _validate_audit(value: Mapping[str, Any]) -> None:
    _exact(value, _AUDIT_FIELDS, "agentic run audit")
    if value.get("schema") != 1:
        raise ValueError("agentic run audit must use schema 1")
    if value.get("contract_kind") != AGENTIC_RUN_AUDIT_KIND:
        raise ValueError(f"agentic run audit kind must be {AGENTIC_RUN_AUDIT_KIND!r}")
    bounded_identity(value.get("project_id"), "agentic project")
    owner_identity(value.get("owner"), "agentic run owner")
    identifier(value.get("target"), "agentic run target")
    identifier(value.get("operation"), "agentic run operation")
    run_identity(value.get("run_id"))
    bounded_identity(value.get("plan_identity"), "agentic Target Operation Plan")
    bounded_identity(value.get("grant_identity"), "agentic execution grant")
    for field in ("principal", "role", "approval", "environment_identity"):
        identifier(value.get(field), f"agentic run {field}")
    _capabilities(value.get("required_capabilities"), "agentic run audit")
    _budget(value.get("budget"), "agentic run audit")
    _timestamp(value.get("submitted_at"), "agentic run audit submission")
    _timestamp(value.get("started_at"), "agentic run audit start", nullable=True)
    _timestamp(value.get("finished_at"), "agentic run audit finish")
    if value.get("terminal_status") not in TERMINAL_STATUSES:
        raise ValueError("agentic run audit terminal status is invalid")
    terminal_status = value["terminal_status"]
    result_status = value.get("result_status")
    valid_result_statuses = {
        "accepted": {"accepted"},
        "failed": {None, "failed"},
        "cancelled": {None, "failed"},
        "budget-exhausted": {None, "accepted", "failed"},
        "uncertain": {None},
    }
    if result_status not in valid_result_statuses[terminal_status]:
        raise ValueError("agentic run audit result status is invalid")
    error = value.get("error_code")
    if error is not None:
        identifier(error, "agentic run audit error code")


@dataclass(frozen=True)
class LocatedAgenticRun:
    paths: ArtifactExecutionPaths
    state: dict[str, Any]


@dataclass(frozen=True)
class AgenticRunStore:
    context: ProjectContext
    project_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.context, ProjectContext):
            raise TypeError("AgenticRunStore requires a ProjectContext")
        bounded_identity(self.project_id, "agentic project")

    def paths(
        self,
        *,
        owner: str,
        target: str,
        operation: str,
        run_id: str,
    ) -> ArtifactExecutionPaths:
        return self.context.artifacts.agentic_execution(
            owner=owner,
            flow=operation,
            target=target,
            identity=run_id,
        )

    def _validate_path_identity(
        self,
        paths: ArtifactExecutionPaths,
        record: Mapping[str, Any],
    ) -> None:
        expected = self.paths(
            owner=record["owner"],
            target=record["target"],
            operation=record["operation"],
            run_id=record["run_id"],
        )
        if paths != expected:
            raise ValueError("agentic run path identity drift")

    def create(
        self,
        paths: ArtifactExecutionPaths,
        *,
        request: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> None:
        _validate_common(request, request=True)
        _validate_common(state, request=False)
        if request["project_id"] != self.project_id or state["project_id"] != self.project_id:
            raise ValueError("agentic run project identity drift")
        shared = (_STATE_FIELDS & _REQUEST_FIELDS) - {"contract_kind"}
        if any(request[field] != state[field] for field in shared):
            raise ValueError("agentic run request/state identity drift")
        self._validate_path_identity(paths, request)
        self._validate_path_identity(paths, state)
        paths.complete_partial_create()
        request_path = paths.role("control") / "request.json"
        request_text = canonical_json(dict(request))
        if request_path.exists():
            if self.read_request(paths) != dict(request):
                raise ValueError("agentic run partial request conflict")
        else:
            write_immutable_text(request_path, request_text)
        state_path = paths.role("control") / "state.json"
        if state_path.exists():
            if self.read_state(paths) != dict(state):
                raise ValueError("agentic run partial state conflict")
        else:
            atomic_write_json(state_path, state)

    def read_request(self, paths: ArtifactExecutionPaths) -> dict[str, Any]:
        request = read_json_object(paths.role("control") / "request.json", "Agentic Run Request")
        _validate_common(request, request=True)
        if request["project_id"] != self.project_id:
            raise ValueError("agentic run request project identity drift")
        self._validate_path_identity(paths, request)
        return request

    def read_request_if_present(
        self,
        paths: ArtifactExecutionPaths,
    ) -> dict[str, Any] | None:
        target = paths.role("control") / "request.json"
        if not target.exists():
            return None
        return self.read_request(paths)

    def read_state(self, paths: ArtifactExecutionPaths) -> dict[str, Any]:
        state = read_json_object(paths.role("control") / "state.json", "Agentic Run State")
        _validate_common(state, request=False)
        if state["project_id"] != self.project_id:
            raise ValueError("agentic run state project identity drift")
        self._validate_path_identity(paths, state)
        return state

    def write_state(self, paths: ArtifactExecutionPaths, state: Mapping[str, Any]) -> None:
        _validate_common(state, request=False)
        previous = self.read_state(paths)
        identity_fields = _STATE_FIELDS - {
            "status",
            "started_at",
            "finished_at",
            "completed_nodes",
            "current_node",
            "error_code",
        }
        if any(previous[field] != state[field] for field in identity_fields):
            raise ValueError("agentic run state identity drift")
        if previous["status"] in TERMINAL_STATUSES and dict(previous) != dict(state):
            raise ValueError("terminal agentic run state is immutable")
        if state["completed_nodes"] < previous["completed_nodes"]:
            raise ValueError("agentic run progress cannot move backward")
        atomic_write_json(paths.role("control") / "state.json", state)

    def write_audit(self, paths: ArtifactExecutionPaths, audit: Mapping[str, Any]) -> None:
        _validate_audit(audit)
        if audit["project_id"] != self.project_id:
            raise ValueError("agentic run audit project identity drift")
        self._validate_path_identity(paths, audit)
        request = self.read_request(paths)
        state = self.read_state(paths)
        request_fields = (_AUDIT_FIELDS & _REQUEST_FIELDS) - {"contract_kind"}
        if any(audit[field] != request[field] for field in request_fields):
            raise ValueError("agentic run audit request identity drift")
        if state["status"] not in TERMINAL_STATUSES:
            raise ValueError("agentic run audit requires terminal state")
        for audit_field, state_field in (
            ("started_at", "started_at"),
            ("finished_at", "finished_at"),
            ("terminal_status", "status"),
            ("error_code", "error_code"),
        ):
            if audit[audit_field] != state[state_field]:
                raise ValueError("agentic run audit terminal state drift")
        target = paths.role("audit") / "audit.json"
        if target.exists():
            raise ValueError("agentic run audit is immutable")
        write_immutable_text(target, canonical_json(dict(audit)))

    def locate(self, run_id: str) -> LocatedAgenticRun:
        identity = run_identity(run_id)
        root = self.context.artifact_root / "system" / "agentic-target-runs"
        if not root.is_dir() or root.is_symlink():
            raise ValueError(f"unknown managed Target Run: {identity}")

        def child_directories(parent: Path) -> tuple[Path, ...]:
            try:
                with os.scandir(parent) as entries:
                    return tuple(
                        Path(entry.path)
                        for entry in entries
                        if entry.is_dir(follow_symlinks=False)
                    )
            except OSError:
                return ()

        matches: list[Path] = []
        for owner in child_directories(root):
            for target in child_directories(owner):
                for operation in child_directories(target):
                    for candidate in child_directories(operation):
                        if candidate.name == identity:
                            matches.append(candidate)
        if len(matches) != 1:
            raise ValueError(f"unknown or ambiguous managed Target Run: {identity}")
        relative = matches[0].relative_to(root)
        owner, target, operation, _identity = relative.parts
        paths = self.paths(
            owner=owner,
            target=target,
            operation=operation,
            run_id=identity,
        )
        request = self.read_request(paths)
        state = self.read_state(paths)
        shared = (_STATE_FIELDS & _REQUEST_FIELDS) - {"contract_kind"}
        if any(request[field] != state[field] for field in shared):
            raise ValueError("agentic run request/state identity drift")
        return LocatedAgenticRun(paths, state)


__all__ = [
    "AGENTIC_RUN_AUDIT_KIND",
    "AGENTIC_RUN_REQUEST_KIND",
    "AGENTIC_RUN_STATE_KIND",
    "AgenticRunStore",
    "LocatedAgenticRun",
    "RUNNING_STATUSES",
    "TERMINAL_STATUSES",
]
