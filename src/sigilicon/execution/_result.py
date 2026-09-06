"""Typed execution outcomes and their canonical persisted records."""

from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Any, Mapping
from sigilicon.artifacts import read_nofollow_bytes
from sigilicon.paths import validate_artifact_id
from sigilicon.execution._values import (
    ContractError,
    _RUN_FAILURE_STATUSES,
    _RUN_STATUSES,
    _STEP_STATUSES,
    _identifier,
    adapter_identity,
)


@dataclass(frozen=True)
class Artifact:
    role: str
    kind: str
    path: Path
    size: int | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _identifier(self.role, "artifact role"))
        object.__setattr__(self, "kind", adapter_identity(self.kind))
        object.__setattr__(self, "path", Path(self.path).absolute())
        if (self.size is None) != (self.sha256 is None):
            raise ContractError("artifact size and digest must be provided together")
        if self.size is not None and (
            type(self.size) is not int
            or self.size < 0
            or not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
        ):
            raise ContractError("artifact size or digest is invalid")

    def read_bytes(self) -> bytes:
        """Read and, for a stored artifact, verify its payload on demand."""

        payload = read_nofollow_bytes(self.path)
        if self.size is not None and (
            len(payload) != self.size
            or hashlib.sha256(payload).hexdigest() != self.sha256
        ):
            raise ContractError("stored artifact payload disagrees with its reference")
        return payload

    def read_text(self, *, encoding: str = "utf-8") -> str:
        """Read verified text without making an eager retained copy."""

        return self.read_bytes().decode(encoding)


@dataclass(frozen=True)
class StepResult:
    status: str
    artifacts: tuple[Artifact, ...] = ()
    message: str = ""

    def __post_init__(self) -> None:
        if self.status not in _STEP_STATUSES:
            raise ContractError(f"invalid step result status: {self.status!r}")
        if not isinstance(self.artifacts, tuple) or any(
            not isinstance(artifact, Artifact) for artifact in self.artifacts
        ):
            raise ContractError("step result artifacts must be Artifact values")
        if self.status in {"blocked", "cancelled"} and self.artifacts:
            raise ContractError("blocked or cancelled steps cannot publish artifacts")
        if not isinstance(self.message, str):
            raise ContractError("step result message must be text")
        if self.status != "succeeded" and not self.message:
            raise ContractError("failed or blocked steps require a message")

    @classmethod
    def succeeded(
        cls,
        *,
        artifacts: tuple[Artifact, ...] = (),
    ) -> "StepResult":
        return cls("succeeded", artifacts)

    @classmethod
    def failed(cls, message: str) -> "StepResult":
        return cls("failed", message=message)

    @classmethod
    def partial(
        cls,
        message: str,
    ) -> "StepResult":
        return cls("partial", message=message)

    @classmethod
    def uncertain(
        cls,
        message: str,
    ) -> "StepResult":
        return cls("uncertain", message=message)

    @classmethod
    def cancelled(cls, message: str) -> "StepResult":
        return cls("cancelled", message=message)


@dataclass(frozen=True)
class StepOutcome:
    step: str
    adapter: str
    result: StepResult

    def __post_init__(self) -> None:
        object.__setattr__(self, "step", _identifier(self.step, "outcome step"))
        object.__setattr__(self, "adapter", adapter_identity(self.adapter))
        if not isinstance(self.result, StepResult):
            raise ContractError("step outcome requires a StepResult")


@dataclass(frozen=True)
class RunResult:
    owner: str
    operation: str
    variant: str | None
    run_id: str
    operation_id: str
    plan_identity: str
    status: str
    outcomes: tuple[StepOutcome, ...]
    run_root: Path = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner", _identifier(self.owner, "run owner"))
        object.__setattr__(self, "operation", _identifier(self.operation, "run operation"))
        if self.variant is not None:
            object.__setattr__(self, "variant", _identifier(self.variant, "run variant"))
        if self.status not in _RUN_STATUSES:
            raise ContractError(f"invalid run status: {self.status!r}")
        validate_artifact_id(self.run_id, "run id")
        validate_artifact_id(self.operation_id, "operation id")
        validate_artifact_id(self.plan_identity, "plan identity")
        if not isinstance(self.outcomes, tuple) or not self.outcomes or any(
            not isinstance(outcome, StepOutcome) for outcome in self.outcomes
        ):
            raise ContractError("run outcomes must be a non-empty StepOutcome tuple")
        if len({outcome.step for outcome in self.outcomes}) != len(self.outcomes):
            raise ContractError("run outcomes contain duplicate steps")
        step_statuses = {outcome.result.status for outcome in self.outcomes}
        if step_statuses == {"succeeded"}:
            expected_status = "succeeded"
        elif "uncertain" in step_statuses:
            expected_status = "uncertain"
        elif "partial" in step_statuses:
            expected_status = "partial"
        elif "cancelled" in step_statuses:
            expected_status = "cancelled"
        else:
            expected_status = "failed"
        if self.status != expected_status:
            raise ContractError("run status disagrees with its step outcomes")
        for outcome in self.outcomes:
            for artifact in outcome.result.artifacts:
                _run_artifact_path(self.run_root, outcome.step, artifact.path)

    @property
    def record(self) -> dict[str, Any]:
        return {
            "schema": 3,
            "contract_kind": "run-result",
            "owner": self.owner,
            "operation": self.operation,
            "variant": self.variant,
            "run_id": self.run_id,
            "operation_id": self.operation_id,
            "plan_identity": self.plan_identity,
            "status": self.status,
            "steps": [
                {
                    "id": outcome.step,
                    "uses": outcome.adapter,
                    "status": outcome.result.status,
                    "message": outcome.result.message,
                    "artifacts": [
                        {
                            "role": artifact.role,
                            "kind": artifact.kind,
                            "path": _run_artifact_path(
                                self.run_root, outcome.step,
                                artifact.path,
                            ),
                        }
                        for artifact in outcome.result.artifacts
                    ],
                }
                for outcome in self.outcomes
            ],
        }


def _run_artifact_path(run_root: Path, step: str, path: Path) -> str:
    """Serialize against the actual run root, irrespective of nested filenames."""

    try:
        relative = Path(path).absolute().relative_to(Path(run_root).absolute())
    except ValueError as exc:
        raise ContractError(f"step {step!r} published outside its managed output root") from exc
    if (relative.parts[:2] != ("outputs", step) or len(relative.parts) < 3
            or ".." in relative.parts):
        raise ContractError(f"step {step!r} published outside its managed output root")
    return relative.as_posix()


@dataclass(frozen=True)
class RunFailureProvenance:
    """Structured progress and uncertainty for a terminal run failure."""

    completed_steps: tuple[str, ...] = ()
    uncertain_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.completed_steps, tuple):
            raise ContractError("run failure completed steps must be a tuple")
        steps = tuple(
            _identifier(step, "completed run step")
            for step in self.completed_steps
        )
        if len(steps) != len(set(steps)):
            raise ContractError("run failure completed steps contain duplicates")
        object.__setattr__(self, "completed_steps", steps)
        if self.uncertain_reason is not None and (
            not isinstance(self.uncertain_reason, str)
            or not self.uncertain_reason
        ):
            raise ContractError(
                "run failure uncertainty reason must be non-empty text or None"
            )

    @classmethod
    def from_manifest(
        cls,
        partial_failure: object,
        uncertain_reason: object,
    ) -> "RunFailureProvenance":
        if partial_failure is None:
            completed_steps: object = ()
        elif not isinstance(partial_failure, Mapping):
            raise ContractError(
                "run failure partial provenance must be an object or null"
            )
        else:
            if set(partial_failure) != {"completed_steps"}:
                raise ContractError(
                    "run failure partial provenance fields are invalid"
                )
            completed_steps = partial_failure.get("completed_steps")
        if not isinstance(completed_steps, (list, tuple)):
            raise ContractError("run failure completed_steps must be an array")
        return cls(tuple(completed_steps), uncertain_reason)

    @property
    def record(self) -> dict[str, Any]:
        return {
            "completed_steps": list(self.completed_steps),
            "uncertain_reason": self.uncertain_reason,
        }


@dataclass(frozen=True)
class RunFailure:
    """Typed terminal record for a run that failed before producing RunResult."""

    owner: str
    operation: str
    variant: str | None
    run_id: str
    operation_id: str | None
    plan_identity: str
    status: str
    error_type: str
    message: str
    provenance: RunFailureProvenance = field(
        default_factory=RunFailureProvenance
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner", _identifier(self.owner, "run owner"))
        object.__setattr__(self, "operation", _identifier(self.operation, "run operation"))
        if self.variant is not None:
            object.__setattr__(self, "variant", _identifier(self.variant, "run variant"))
        validate_artifact_id(self.run_id, "run id")
        validate_artifact_id(self.plan_identity, "plan identity")
        if self.operation_id is not None:
            validate_artifact_id(self.operation_id, "operation id")
        if self.status not in _RUN_FAILURE_STATUSES:
            raise ContractError(f"invalid run failure status: {self.status!r}")
        if not isinstance(self.error_type, str) or not self.error_type:
            raise ContractError("run failure error type must be non-empty text")
        if not isinstance(self.message, str):
            raise ContractError("run failure message must be text")
        if not isinstance(self.provenance, RunFailureProvenance):
            raise ContractError("run failure provenance must be RunFailureProvenance")
        if self.status == "failed" and (
            self.provenance.completed_steps
            or self.provenance.uncertain_reason is not None
        ):
            raise ContractError("failed run cannot contain failure provenance")
        if self.status == "partial":
            if not self.provenance.completed_steps:
                raise ContractError("partial run failure requires completed steps")
            if self.provenance.uncertain_reason is not None:
                raise ContractError(
                    "partial run failure cannot contain uncertainty reason"
                )
        elif self.status == "uncertain":
            if self.provenance.uncertain_reason is None:
                raise ContractError(
                    "uncertain run failure requires uncertainty reason"
                )
        elif self.provenance.uncertain_reason is not None:
            raise ContractError(
                "uncertainty reason requires an uncertain run failure"
            )

    @property
    def record(self) -> dict[str, Any]:
        return {
            "schema": 3,
            "contract_kind": "run-failure",
            "owner": self.owner,
            "operation": self.operation,
            "variant": self.variant,
            "run_id": self.run_id,
            "operation_id": self.operation_id,
            "plan_identity": self.plan_identity,
            "status": self.status,
            "error": {"type": self.error_type, "message": self.message},
            "provenance": self.provenance.record,
        }
