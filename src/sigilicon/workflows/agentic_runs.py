"""Durable control records for authorized agentic Flow execution."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Mapping

from sigilicon.artifacts import atomic_write_json, read_json_object
from sigilicon.flow.model import identifier, owner_identity, run_identity
from sigilicon.paths import ArtifactExecutionPaths, ArtifactLayout


AGENTIC_RUN_REQUEST_KIND = "agentic-flow-run-request"
AGENTIC_RUN_STATE_KIND = "agentic-flow-run-state"
AGENTIC_RUN_AUDIT_KIND = "agentic-flow-run-audit"
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
    "flow",
    "target",
    "profile",
    "plan_identity",
    "run_id",
    "grant_identity",
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
}


def _exact(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    missing = fields - set(value)
    unknown = set(value) - fields
    if missing or unknown:
        raise ValueError(
            f"{label} fields do not match: missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )


def _sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a SHA-256 identity")
    return value


def _timestamp(value: object, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or "\n" in value:
        raise ValueError(f"{label} must be timestamp text")
    return value


def _validate_common(value: Mapping[str, Any], *, request: bool) -> None:
    _exact(value, _REQUEST_FIELDS if request else _STATE_FIELDS, "agentic run record")
    if value.get("schema") != 1:
        raise ValueError("agentic run record must use schema 1")
    expected_kind = AGENTIC_RUN_REQUEST_KIND if request else AGENTIC_RUN_STATE_KIND
    if value.get("contract_kind") != expected_kind:
        raise ValueError(f"agentic run record kind must be {expected_kind!r}")
    _sha256(value.get("project_id"), "agentic project")
    owner_identity(value.get("owner"), "agentic run owner")
    identifier(value.get("flow"), "agentic run Flow")
    identifier(value.get("target"), "agentic run target")
    identifier(value.get("profile"), "agentic run profile")
    _sha256(value.get("plan_identity"), "agentic Flow Plan")
    run_identity(value.get("run_id"))
    _sha256(value.get("grant_identity"), "agentic execution grant")
    capabilities = value.get("required_capabilities")
    if (
        not isinstance(capabilities, list)
        or not capabilities
        or capabilities != sorted(set(capabilities))
        or any(item not in {"execute-derived", "mutate-workspace"} for item in capabilities)
    ):
        raise ValueError("agentic run capabilities are invalid")
    budget = value.get("budget")
    if not isinstance(budget, dict) or set(budget) != {"maximum_seconds", "maximum_nodes"}:
        raise ValueError("agentic run budget is invalid")
    if (
        type(budget["maximum_seconds"]) is not int
        or not 1 <= budget["maximum_seconds"] <= 86_400
        or type(budget["maximum_nodes"]) is not int
        or not 1 <= budget["maximum_nodes"] <= 10_000
    ):
        raise ValueError("agentic run budget is outside supported bounds")
    _timestamp(value.get("submitted_at"), "agentic run submission")
    total = value.get("total_nodes")
    if type(total) is not int or total <= 0 or total > budget["maximum_nodes"]:
        raise ValueError("agentic run total node count is invalid")
    if request:
        for field in ("principal", "role", "approval", "environment_identity"):
            identifier(value.get(field), f"agentic run {field}")
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


@dataclass(frozen=True)
class LocatedAgenticRun:
    paths: ArtifactExecutionPaths
    state: dict[str, Any]


@dataclass(frozen=True)
class AgenticRunStore:
    artifact_root: Path
    project_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_root", Path(self.artifact_root).resolve())
        _sha256(self.project_id, "agentic project")

    def paths(
        self,
        *,
        owner: str,
        flow: str,
        target: str,
        run_id: str,
    ) -> ArtifactExecutionPaths:
        return ArtifactLayout(self.artifact_root).agentic_execution(
            owner=owner,
            flow=flow,
            target=target,
            identity=run_id,
        )

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
        paths.create()
        atomic_write_json(paths.role("control") / "request.json", request)
        atomic_write_json(paths.role("control") / "state.json", state)

    def read_request(self, paths: ArtifactExecutionPaths) -> dict[str, Any]:
        request = read_json_object(paths.role("control") / "request.json", "Agentic Run Request")
        _validate_common(request, request=True)
        if request["project_id"] != self.project_id:
            raise ValueError("agentic run request project identity drift")
        return request

    def read_state(self, paths: ArtifactExecutionPaths) -> dict[str, Any]:
        state = read_json_object(paths.role("control") / "state.json", "Agentic Run State")
        _validate_common(state, request=False)
        if state["project_id"] != self.project_id:
            raise ValueError("agentic run state project identity drift")
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
        target = paths.role("audit") / "audit.json"
        if target.exists():
            raise ValueError("agentic run audit is immutable")
        atomic_write_json(target, audit)

    def locate(self, run_id: str) -> LocatedAgenticRun:
        identity = run_identity(run_id)
        root = self.artifact_root / "system" / "agentic-flow-runs"
        if not root.is_dir() or root.is_symlink():
            raise ValueError(f"unknown managed Flow Run: {identity}")

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
                for flow in child_directories(target):
                    for candidate in child_directories(flow):
                        if candidate.name == identity:
                            matches.append(candidate)
        if len(matches) != 1:
            raise ValueError(f"unknown or ambiguous managed Flow Run: {identity}")
        relative = matches[0].relative_to(root)
        owner, target, flow, _identity = relative.parts
        paths = self.paths(owner=owner, target=target, flow=flow, run_id=identity)
        return LocatedAgenticRun(paths, self.read_state(paths))


__all__ = [
    "AGENTIC_RUN_AUDIT_KIND",
    "AGENTIC_RUN_REQUEST_KIND",
    "AGENTIC_RUN_STATE_KIND",
    "AgenticRunStore",
    "LocatedAgenticRun",
    "RUNNING_STATUSES",
    "TERMINAL_STATUSES",
]
