"""Durable nofollow storage for resumable agentic Design Campaigns."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import json
from typing import Any, Mapping

from sigilicon.artifacts import (
    atomic_write_json,
    read_json_object,
    read_nofollow_text,
    write_immutable_text,
)
from sigilicon.canonical import canonical_json
from sigilicon.flow.model import run_identity
from sigilicon.paths import ArtifactExecutionPaths, ArtifactLayout
from sigilicon.workflows.design_campaign import (
    DesignCampaignState,
    design_campaign_state_from_json,
)
from sigilicon.workflows.design_repair import design_repair_proposal_from_json


CAMPAIGN_REQUEST_KIND = "agentic-design-campaign-request"
CAMPAIGN_EVENT_KIND = "agentic-design-campaign-event"
_EVENT = re.compile(r"event-(?P<sequence>[0-9]{4})\.json\Z")
_REQUEST_FIELDS = {
    "schema",
    "contract_kind",
    "project_id",
    "owner",
    "campaign_id",
    "campaign_identity",
    "run_id",
    "grant_identity",
    "grant_json",
    "principal",
    "role",
    "approval",
    "required_capabilities",
    "environment_identity",
    "environment_record_json",
    "campaign_plan_record_json",
    "submitted_at",
}
_EVENT_FIELDS = {
    "schema",
    "contract_kind",
    "project_id",
    "campaign_identity",
    "run_id",
    "sequence",
    "operation",
    "proposal_identity",
    "proposal_json",
    "recorded_at",
    "principal",
    "approval",
    "state_json",
}


def _exact(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    if set(value) != fields:
        raise ValueError(
            f"{label} fields do not match: missing={sorted(fields - set(value))}, "
            f"unknown={sorted(set(value) - fields)}"
        )


def _canonical_record(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be canonical JSON text")
    try:
        record = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be canonical JSON text") from exc
    if not isinstance(record, dict) or canonical_json(record) != value:
        raise ValueError(f"{label} must be an exact canonical JSON object")


@dataclass(frozen=True)
class LocatedDesignCampaign:
    paths: ArtifactExecutionPaths
    request: dict[str, Any]
    campaign_json: str
    state: DesignCampaignState | None
    last_proposal_json: str | None


@dataclass(frozen=True)
class DesignCampaignStore:
    artifact_root: Path
    project_id: str

    def __post_init__(self) -> None:
        root = Path(self.artifact_root).resolve()
        if root == Path(root.anchor):
            raise ValueError("Design Campaign store cannot use a filesystem root")
        object.__setattr__(self, "artifact_root", root)
        if not isinstance(self.project_id, str) or not self.project_id:
            raise ValueError("Design Campaign store needs a project identity")

    def paths(self, *, owner: str, campaign_id: str, run_id: str) -> ArtifactExecutionPaths:
        return ArtifactLayout(self.artifact_root).agentic_campaign(
            owner=owner,
            campaign=campaign_id,
            identity=run_id,
        )

    def create(
        self,
        paths: ArtifactExecutionPaths,
        *,
        request: Mapping[str, Any],
        campaign_json: str,
    ) -> None:
        _exact(request, _REQUEST_FIELDS, "Design Campaign request")
        if (
            request["schema"] != 1
            or request["contract_kind"] != CAMPAIGN_REQUEST_KIND
            or request["project_id"] != self.project_id
            or request["run_id"] != paths.identity
        ):
            raise ValueError("Design Campaign request identity drift")
        for field in (
            "grant_json",
            "environment_record_json",
            "campaign_plan_record_json",
        ):
            _canonical_record(request[field], f"Design Campaign {field}")
        if not paths.root.exists():
            try:
                paths.create()
            except FileExistsError:
                pass
        if (
            not paths.root.is_dir()
            or paths.root.is_symlink()
            or not paths.root.resolve().is_relative_to(self.artifact_root)
        ):
            raise ValueError("Design Campaign partial create path is unsafe")
        known_roles = set(paths.roles)
        entries = {item.name: item for item in os.scandir(paths.root)}
        if set(entries) - known_roles:
            raise ValueError("Design Campaign partial create inventory conflicts")
        for role in paths.roles:
            target = paths.role(role)
            if not target.exists():
                target.mkdir()
            if not target.is_dir() or target.is_symlink():
                raise ValueError("Design Campaign partial create role conflicts")
        events = paths.role("control") / "events"
        if not events.exists():
            events.mkdir()
        if not events.is_dir() or events.is_symlink():
            raise ValueError("Design Campaign event store conflicts")
        for target, text, label in (
            (
                paths.role("inputs") / "request.json",
                canonical_json(dict(request)),
                "request",
            ),
            (paths.role("inputs") / "campaign.json", campaign_json, "Campaign"),
        ):
            if target.exists():
                if read_nofollow_text(target) != text:
                    raise ValueError(f"Design Campaign partial {label} conflict")
            else:
                write_immutable_text(target, text)

    @contextmanager
    def exclusive(self, paths: ArtifactExecutionPaths):
        """Serialize one Campaign state transition, including its Flow attempt."""

        lock = paths.role("control") / "transition.lock"
        descriptor = os.open(
            lock,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("Design Campaign transition is already in progress") from exc
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _request(self, paths: ArtifactExecutionPaths) -> dict[str, Any]:
        request = read_json_object(
            paths.role("inputs") / "request.json",
            "Design Campaign Request",
        )
        _exact(request, _REQUEST_FIELDS, "Design Campaign request")
        if request["project_id"] != self.project_id or request["run_id"] != paths.identity:
            raise ValueError("Design Campaign request identity drift")
        for field in (
            "grant_json",
            "environment_record_json",
            "campaign_plan_record_json",
        ):
            _canonical_record(request[field], f"Design Campaign {field}")
        return request

    def read_request_if_present(
        self,
        paths: ArtifactExecutionPaths,
    ) -> dict[str, Any] | None:
        target = paths.role("inputs") / "request.json"
        if not target.exists():
            return None
        return self._request(paths)

    def _events(
        self,
        paths: ArtifactExecutionPaths,
        request: Mapping[str, Any],
    ) -> tuple[tuple[dict[str, Any], DesignCampaignState], ...]:
        root = paths.role("control") / "events"
        if not root.is_dir() or root.is_symlink():
            raise ValueError("Design Campaign event store is unsafe")
        entries: list[tuple[int, Path]] = []
        with os.scandir(root) as scanned:
            for entry in scanned:
                match = _EVENT.fullmatch(entry.name)
                if match is None or not entry.is_file(follow_symlinks=False):
                    raise ValueError("Design Campaign event inventory is invalid")
                entries.append((int(match["sequence"]), Path(entry.path)))
        entries.sort()
        states: list[tuple[dict[str, Any], DesignCampaignState]] = []
        for expected_sequence, (sequence, path) in enumerate(entries, 1):
            if sequence != expected_sequence:
                raise ValueError("Design Campaign event sequence is incomplete")
            text = read_nofollow_text(path)
            event = read_json_object(path, "Design Campaign Event")
            _exact(event, _EVENT_FIELDS, "Design Campaign event")
            if canonical_json(event) != text:
                raise ValueError("Design Campaign event serialization drift")
            if (
                event["schema"] != 1
                or event["contract_kind"] != CAMPAIGN_EVENT_KIND
                or event["project_id"] != self.project_id
                or event["campaign_identity"] != request["campaign_identity"]
                or event["run_id"] != paths.identity
                or event["sequence"] != sequence
                or event["principal"] != request["principal"]
                or event["approval"] != request["approval"]
            ):
                raise ValueError("Design Campaign event lineage drift")
            if event["operation"] == "start":
                if sequence != 1 or event["proposal_identity"] is not None or event["proposal_json"] is not None:
                    raise ValueError("Design Campaign start event conflicts")
            elif event["operation"] == "resume":
                if sequence == 1 or event["proposal_identity"] is None or event["proposal_json"] is None:
                    raise ValueError("Design Campaign resume event conflicts")
                proposal = design_repair_proposal_from_json(event["proposal_json"])
                if proposal.identity != event["proposal_identity"]:
                    raise ValueError("Design Campaign event proposal record drift")
            else:
                raise ValueError("Design Campaign event operation is invalid")
            state = design_campaign_state_from_json(event["state_json"])
            if (
                state.campaign_identity != request["campaign_identity"]
            ):
                raise ValueError("Design Campaign event state drift")
            if states:
                previous = states[-1][1]
                if previous.phase.value == "completed":
                    raise ValueError("Design Campaign terminal event was rewritten")
                if len(state.iterations) < len(previous.iterations):
                    raise ValueError("Design Campaign event iterations moved backward")
            states.append((event, state))
        return tuple(states)

    def append(
        self,
        paths: ArtifactExecutionPaths,
        state: DesignCampaignState,
        *,
        operation: str,
        recorded_at: str,
        proposal_identity: str | None,
        proposal_json: str | None = None,
    ) -> None:
        request = self._request(paths)
        events = self._events(paths, request)
        if operation not in {"start", "resume"}:
            raise ValueError("Design Campaign event operation is invalid")
        if (not events and operation != "start") or (events and operation != "resume"):
            raise ValueError("Design Campaign event operation sequence conflicts")
        if events and events[-1][1].phase.value == "completed":
            raise ValueError("terminal Design Campaign state is immutable")
        if operation == "start" and (proposal_identity is not None or proposal_json is not None):
            raise ValueError("Design Campaign start cannot carry a proposal")
        if operation == "resume":
            if proposal_identity is None or proposal_json is None:
                raise ValueError("Design Campaign resume needs an exact proposal record")
            proposal = design_repair_proposal_from_json(proposal_json)
            if proposal.identity != proposal_identity:
                raise ValueError("Design Campaign proposal identity/record drift")
        if (
            state.campaign_identity != request["campaign_identity"]
            or state.owner != request["owner"]
            or state.campaign_id != request["campaign_id"]
        ):
            raise ValueError("Design Campaign appended state identity drift")
        sequence = len(events) + 1
        state_json = state.canonical_json()
        event = {
            "schema": 1,
            "contract_kind": CAMPAIGN_EVENT_KIND,
            "project_id": self.project_id,
            "campaign_identity": request["campaign_identity"],
            "run_id": paths.identity,
            "sequence": sequence,
            "operation": operation,
            "proposal_identity": proposal_identity,
            "proposal_json": proposal_json,
            "recorded_at": recorded_at,
            "principal": request["principal"],
            "approval": request["approval"],
            "state_json": state_json,
        }
        text = canonical_json(event)
        target = paths.role("control") / "events" / f"event-{sequence:04d}.json"
        write_immutable_text(target, text)
        atomic_write_json(
            paths.role("control") / "state.json",
            {
                "schema": 1,
                "contract_kind": "agentic-design-campaign-state-pointer",
                "run_id": paths.identity,
                "sequence": sequence,
                "event_sequence": sequence,
            },
        )

    def read(self, paths: ArtifactExecutionPaths) -> LocatedDesignCampaign:
        request = self._request(paths)
        campaign_json = read_nofollow_text(paths.role("inputs") / "campaign.json")
        events = self._events(paths, request)
        state = None if not events else events[-1][1]
        if events:
            _latest_event, latest_state = events[-1]
            pointer = {
                "schema": 1,
                "contract_kind": "agentic-design-campaign-state-pointer",
                "run_id": paths.identity,
                "sequence": len(events),
                "event_sequence": len(events),
            }
            try:
                current = read_json_object(
                    paths.role("control") / "state.json",
                    "Design Campaign State Pointer",
                )
            except (OSError, RuntimeError):
                current = None
            if current != pointer:
                atomic_write_json(paths.role("control") / "state.json", pointer)
        last_proposal_json = None if not events else events[-1][0]["proposal_json"]
        return LocatedDesignCampaign(
            paths,
            request,
            campaign_json,
            state,
            last_proposal_json,
        )

    def locate(self, run_id: str) -> LocatedDesignCampaign:
        identity = run_identity(run_id)
        root = self.artifact_root / "system" / "agentic-design-campaigns"
        if not root.is_dir() or root.is_symlink():
            raise ValueError(f"unknown Design Campaign Run: {identity}")

        def directories(parent: Path) -> tuple[Path, ...]:
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
        for owner in directories(root):
            for campaign in directories(owner):
                for candidate in directories(campaign):
                    if candidate.name == identity:
                        matches.append(candidate)
        if len(matches) != 1:
            raise ValueError(f"unknown or ambiguous Design Campaign Run: {identity}")
        relative = matches[0].relative_to(root)
        owner, campaign_id, _run_id = relative.parts
        return self.read(
            self.paths(owner=owner, campaign_id=campaign_id, run_id=identity)
        )

    def finalize(self, paths: ArtifactExecutionPaths, state: DesignCampaignState) -> None:
        """Write one immutable terminal audit projection; events remain authority."""

        request = self._request(paths)
        payload = {
            **request,
            "contract_kind": "agentic-design-campaign-audit",
            "terminal_status": state.termination.value,
            "iteration_count": len(state.iterations),
        }
        target = paths.role("audit") / "audit.json"
        text = canonical_json(payload)
        if target.exists():
            if read_nofollow_text(target) != text:
                raise ValueError("Design Campaign terminal audit identity drift")
            return
        write_immutable_text(target, text)


__all__ = [
    "CAMPAIGN_EVENT_KIND",
    "CAMPAIGN_REQUEST_KIND",
    "DesignCampaignStore",
    "LocatedDesignCampaign",
]
