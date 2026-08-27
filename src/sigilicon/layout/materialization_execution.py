"""Receipt contract for executing a database-neutral Materialization Plan.

The physical-design kernel and plan compiler remain free of database writes.  A
tool Adapter writes one managed layout at the Flow seam, then uses this Module
to validate the request and issue an immutable, content-bound receipt.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import os
from pathlib import Path
import re
import stat
from collections.abc import Mapping

from sigilicon.canonical import canonical_from_json
from sigilicon.layout.materialization import (
    MaterializationIssue,
    MaterializationPlan,
    MaterializationValidation,
    validate_materialization_plan,
)
from sigilicon.layout.pnr.model import PhysicalDesignJob, PhysicalDesignResult
from sigilicon.layout.pnr.serialization import canonical_json, canonical_sha256


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_LAYOUT_KIND_BY_FORMAT = {"gdsii": "layout.gds"}
_CANONICAL_GDS_DATE = (2000, 1, 1, 0, 0, 0) * 2


class MaterializationExecutionError(ValueError):
    """A materialization request or receipt is not trustworthy."""


class LayoutArtifactFormat(str, Enum):
    GDSII = "gdsii"


class MaterializationExecutionStatus(str, Enum):
    MATERIALIZED = "materialized"
    UNSUPPORTED = "unsupported"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    EXECUTION_FAILED = "execution_failed"
    INVALID_PLAN_IDENTITY = "invalid_plan_identity"


def _text(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\n" in value
        or "\r" in value
    ):
        raise MaterializationExecutionError(f"{label} must be non-empty text")
    return value


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise MaterializationExecutionError(f"{label} must be a SHA-256 identity")
    return value


@dataclass(frozen=True)
class MaterializationExecutionTarget:
    owner: str
    name: str
    format: LayoutArtifactFormat

    def __post_init__(self) -> None:
        _text(self.owner, "materialization execution target owner")
        _text(self.name, "materialization execution target name")
        if not isinstance(self.format, LayoutArtifactFormat):
            raise MaterializationExecutionError(
                "materialization execution target format must be typed"
            )


@dataclass(frozen=True)
class MaterializationCompletion:
    backend: str
    executed: bool
    completed: bool
    content_validated: bool
    exit_code: int | None

    def __post_init__(self) -> None:
        _text(self.backend, "materialization backend identity")
        if any(
            type(value) is not bool
            for value in (self.executed, self.completed, self.content_validated)
        ):
            raise MaterializationExecutionError(
                "materialization completion flags must be booleans"
            )
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise MaterializationExecutionError(
                "materialization exit code must be an integer or null"
            )
        if self.completed and not self.executed:
            raise MaterializationExecutionError(
                "an unexecuted materialization cannot be complete"
            )
        if self.content_validated and not self.completed:
            raise MaterializationExecutionError(
                "an incomplete materialization cannot have validated content"
            )
        if self.exit_code is not None and not self.executed:
            raise MaterializationExecutionError(
                "an unexecuted materialization cannot have an exit code"
            )

    @property
    def proven(self) -> bool:
        return (
            self.executed
            and self.completed
            and self.content_validated
            and self.exit_code == 0
        )


@dataclass(frozen=True)
class ManagedLayoutArtifact:
    kind: str
    format: LayoutArtifactFormat
    content_sha256: str
    size_bytes: int
    run_id: str
    producer: str
    role: str
    relative_path: str

    def __post_init__(self) -> None:
        if not isinstance(self.format, LayoutArtifactFormat):
            raise MaterializationExecutionError(
                "managed layout format must be typed"
            )
        expected_kind = _LAYOUT_KIND_BY_FORMAT[self.format.value]
        if self.kind != expected_kind:
            raise MaterializationExecutionError(
                f"{self.format.value} layout kind must be {expected_kind!r}"
            )
        _sha256(self.content_sha256, "managed layout content identity")
        if type(self.size_bytes) is not int or self.size_bytes <= 0:
            raise MaterializationExecutionError(
                "managed layout size must be a positive integer"
            )
        if not isinstance(self.run_id, str) or _RUN_ID.fullmatch(self.run_id) is None:
            raise MaterializationExecutionError(
                "managed layout run identity must be 32 lowercase hexadecimal digits"
            )
        _text(self.producer, "managed layout producer")
        _text(self.role, "managed layout role")
        relative = Path(self.relative_path)
        if (
            not self.relative_path
            or relative.is_absolute()
            or "\\" in self.relative_path
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise MaterializationExecutionError(
                "managed layout path must be relative to its Flow Run"
            )


@dataclass(frozen=True)
class MaterializationExecutionProvenance:
    job_sha256: str
    result_sha256: str
    plan_sha256: str
    layout_sha256: str | None

    def __post_init__(self) -> None:
        _sha256(self.job_sha256, "materialization job identity")
        _sha256(self.result_sha256, "materialization result identity")
        _sha256(self.plan_sha256, "materialization plan identity")
        if self.layout_sha256 is not None:
            _sha256(self.layout_sha256, "materialized layout identity")


@dataclass(frozen=True)
class MaterializationReceipt:
    status: MaterializationExecutionStatus
    target: MaterializationExecutionTarget
    provenance: MaterializationExecutionProvenance
    completion: MaterializationCompletion
    layout: ManagedLayoutArtifact | None
    issues: tuple[MaterializationIssue, ...]
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, MaterializationExecutionStatus):
            raise MaterializationExecutionError(
                "materialization receipt status must be typed"
            )
        if not isinstance(self.target, MaterializationExecutionTarget):
            raise MaterializationExecutionError(
                "materialization receipt target must be typed"
            )
        if not isinstance(self.provenance, MaterializationExecutionProvenance):
            raise MaterializationExecutionError(
                "materialization receipt provenance must be typed"
            )
        if not isinstance(self.completion, MaterializationCompletion):
            raise MaterializationExecutionError(
                "materialization receipt completion must be typed"
            )
        if not isinstance(self.issues, tuple) or any(
            not isinstance(issue, MaterializationIssue) for issue in self.issues
        ):
            raise MaterializationExecutionError(
                "materialization receipt issues must be a typed tuple"
            )
        _text(self.message, "materialization receipt message")

        if self.status is MaterializationExecutionStatus.MATERIALIZED:
            if not self.completion.proven or self.layout is None:
                raise MaterializationExecutionError(
                    "materialized status requires proven backend completion and layout"
                )
            if self.issues:
                raise MaterializationExecutionError(
                    "materialized status cannot carry request issues"
                )
        elif self.status in {
            MaterializationExecutionStatus.UNSUPPORTED,
            MaterializationExecutionStatus.BACKEND_UNAVAILABLE,
            MaterializationExecutionStatus.INVALID_PLAN_IDENTITY,
        }:
            if (
                self.completion.executed
                or self.completion.completed
                or self.completion.content_validated
                or self.completion.exit_code is not None
            ):
                raise MaterializationExecutionError(
                    f"{self.status.value} cannot claim backend execution"
                )
            if self.layout is not None:
                raise MaterializationExecutionError(
                    f"{self.status.value} cannot expose a checked layout"
                )
        elif self.status is MaterializationExecutionStatus.EXECUTION_FAILED:
            if not self.completion.executed or self.completion.proven:
                raise MaterializationExecutionError(
                    "execution_failed requires attempted but unproven execution"
                )
            if self.layout is not None:
                raise MaterializationExecutionError(
                    "failed execution cannot expose a checked layout"
                )

        if self.status is MaterializationExecutionStatus.INVALID_PLAN_IDENTITY:
            if not self.issues:
                raise MaterializationExecutionError(
                    "invalid plan/identity status requires typed issues"
                )
        elif self.issues:
            raise MaterializationExecutionError(
                "only invalid plan/identity status can carry request issues"
            )

        layout_sha256 = None if self.layout is None else self.layout.content_sha256
        if self.provenance.layout_sha256 != layout_sha256:
            raise MaterializationExecutionError(
                "receipt layout identity disagrees with managed artifact"
            )
        if self.layout is not None and self.layout.format is not self.target.format:
            raise MaterializationExecutionError(
                "managed layout format disagrees with materialization target"
            )

    @property
    def materialized(self) -> bool:
        return self.status is MaterializationExecutionStatus.MATERIALIZED

    def canonical_json(self) -> str:
        return canonical_json(self)


def materialization_execution_target_from_mapping(
    value: object,
) -> MaterializationExecutionTarget:
    if not isinstance(value, Mapping):
        raise MaterializationExecutionError(
            "materialization execution target must be an object"
        )
    if set(value) != {"owner", "name", "format"}:
        raise MaterializationExecutionError(
            "materialization execution target fields must be exactly "
            "['format', 'name', 'owner']"
        )
    try:
        layout_format = LayoutArtifactFormat(value["format"])
    except (TypeError, ValueError) as exc:
        raise MaterializationExecutionError(
            f"unsupported materialization layout format: {value['format']!r}"
        ) from exc
    return MaterializationExecutionTarget(
        owner=value["owner"],
        name=value["name"],
        format=layout_format,
    )


def materialization_receipt_from_json(text: str) -> MaterializationReceipt:
    return canonical_from_json(text, MaterializationReceipt)


def validate_materialization_request(
    job: PhysicalDesignJob,
    result: PhysicalDesignResult,
    plan: MaterializationPlan,
    target: MaterializationExecutionTarget,
) -> MaterializationValidation:
    """Validate all identities and executability before backend execution."""

    issues = list(validate_materialization_plan(job, result, plan).issues)
    if not plan.executable:
        issues.append(
            MaterializationIssue(
                "plan_not_executable",
                "diagnostic or rejected Materialization Plan cannot be executed",
            )
        )
    if (plan.target.owner, plan.target.name) != (target.owner, target.name):
        issues.append(
            MaterializationIssue(
                "target_mismatch",
                "execution target does not match Materialization Plan target",
                (
                    plan.target.owner,
                    plan.target.name,
                    target.owner,
                    target.name,
                ),
            )
        )
    return MaterializationValidation(valid=not issues, issues=tuple(issues))


def _gdsii_records(payload: bytes) -> tuple[tuple[int, int, int], ...]:
    records: list[tuple[int, int, int]] = []
    offset = 0
    while offset < len(payload):
        if len(payload) - offset < 4:
            raise MaterializationExecutionError(
                "GDSII layout ends inside a record header"
            )
        length = int.from_bytes(payload[offset : offset + 2], "big")
        if length < 4 or length % 2 or offset + length > len(payload):
            raise MaterializationExecutionError(
                "GDSII layout contains an invalid record length"
            )
        record_type = payload[offset + 2]
        records.append((offset, length, record_type))
        offset += length
        if record_type == 0x04:
            if any(payload[offset:]):
                raise MaterializationExecutionError(
                    "GDSII layout contains nonzero data after ENDLIB"
                )
            break
    record_types = tuple(record_type for _, _, record_type in records)
    required = {0x00, 0x01, 0x02, 0x03, 0x05, 0x06, 0x07, 0x04}
    if (
        not record_types
        or record_types[0] != 0x00
        or record_types[-1] != 0x04
        or not required.issubset(record_types)
    ):
        raise MaterializationExecutionError(
            "GDSII layout lacks the required library and structure records"
        )
    element_records = {0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x15, 0x2D}
    if element_records.isdisjoint(record_types):
        raise MaterializationExecutionError(
            "GDSII layout contains no materialized geometry elements"
        )
    return tuple(records)


def validate_layout_content(
    payload: bytes,
    layout_format: LayoutArtifactFormat,
) -> None:
    """Reject empty, JSON, and arbitrary bytes before they can become layout."""

    if not isinstance(payload, bytes) or not payload:
        raise MaterializationExecutionError(
            "materialized layout must contain non-empty binary content"
        )
    if layout_format is LayoutArtifactFormat.GDSII:
        _gdsii_records(payload)
        return
    raise MaterializationExecutionError(
        f"unsupported materialization layout format: {layout_format!r}"
    )


def canonicalize_gdsii_timestamps(payload: bytes) -> bytes:
    """Remove backend wall-clock variance without changing GDSII geometry."""

    validate_layout_content(payload, LayoutArtifactFormat.GDSII)
    canonical_date = b"".join(
        value.to_bytes(2, "big", signed=False) for value in _CANONICAL_GDS_DATE
    )
    records = _gdsii_records(payload)
    result = bytearray(payload)
    for offset, length, record_type in records:
        data_type = result[offset + 3]
        if record_type in {0x01, 0x05}:
            if length != 28 or data_type != 0x02:
                raise MaterializationExecutionError(
                    "GDSII timestamp record has an invalid shape"
                )
            result[offset + 4 : offset + length] = canonical_date
    canonical = bytes(result)
    validate_layout_content(canonical, LayoutArtifactFormat.GDSII)
    return canonical


def identify_managed_layout(
    path: Path,
    *,
    target: MaterializationExecutionTarget,
    run_root: Path,
    run_id: str,
    producer: str,
    role: str = "layout",
) -> ManagedLayoutArtifact:
    """Validate and identify one regular layout inside a managed Flow Run."""

    candidate = Path(path)
    root = Path(run_root).resolve()
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise MaterializationExecutionError(
            f"cannot inspect materialized layout: {exc}"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode) or candidate.is_symlink():
        raise MaterializationExecutionError(
            "materialized layout must be a managed regular file"
        )
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise MaterializationExecutionError(
            "materialized layout escaped its managed Flow Run"
        )
    if root.name != run_id:
        raise MaterializationExecutionError(
            "managed layout run identity disagrees with its Flow Run root"
        )
    relative = resolved.relative_to(root)
    if (
        len(relative.parts) != 4
        or relative.parts[:3] != ("outputs", producer, role)
    ):
        raise MaterializationExecutionError(
            "managed layout producer or role disagrees with its output path"
        )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            candidate,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise MaterializationExecutionError(
                "materialized layout identity changed before it was read"
            )
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        visible = candidate.lstat()
        identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        )
        if identity != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or (visible.st_dev, visible.st_ino) != identity[:2]:
            raise MaterializationExecutionError(
                "materialized layout identity changed while it was read"
            )
        payload = b"".join(chunks)
    except MaterializationExecutionError:
        raise
    except OSError as exc:
        raise MaterializationExecutionError(
            f"cannot read materialized layout: {exc}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    validate_layout_content(payload, target.format)
    return ManagedLayoutArtifact(
        kind=_LAYOUT_KIND_BY_FORMAT[target.format.value],
        format=target.format,
        content_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        run_id=run_id,
        producer=producer,
        role=role,
        relative_path=relative.as_posix(),
    )


def issue_materialization_receipt(
    job: PhysicalDesignJob,
    result: PhysicalDesignResult,
    plan: MaterializationPlan,
    target: MaterializationExecutionTarget,
    *,
    status: MaterializationExecutionStatus,
    completion: MaterializationCompletion,
    layout: ManagedLayoutArtifact | None,
    message: str,
) -> MaterializationReceipt:
    """Issue the only result contract accepted at the execution seam."""

    request = validate_materialization_request(job, result, plan, target)
    if request.valid and status is MaterializationExecutionStatus.INVALID_PLAN_IDENTITY:
        raise MaterializationExecutionError(
            "valid materialization request cannot claim invalid plan/identity"
        )
    if not request.valid and status is not MaterializationExecutionStatus.INVALID_PLAN_IDENTITY:
        raise MaterializationExecutionError(
            "invalid materialization request must be rejected before backend execution"
        )
    return MaterializationReceipt(
        status=status,
        target=target,
        provenance=MaterializationExecutionProvenance(
            job_sha256=canonical_sha256(job),
            result_sha256=canonical_sha256(result),
            plan_sha256=canonical_sha256(plan),
            layout_sha256=None if layout is None else layout.content_sha256,
        ),
        completion=completion,
        layout=layout,
        issues=() if request.valid else request.issues,
        message=message,
    )


def validate_materialization_receipt(
    job: PhysicalDesignJob,
    result: PhysicalDesignResult,
    plan: MaterializationPlan,
    target: MaterializationExecutionTarget,
    receipt: MaterializationReceipt,
    *,
    layout_path: Path | None = None,
    run_root: Path | None = None,
) -> MaterializationValidation:
    """Revalidate a receipt and, when present, its exact managed content."""

    issues: list[MaterializationIssue] = []
    request = validate_materialization_request(job, result, plan, target)
    invalid_status = (
        receipt.status is MaterializationExecutionStatus.INVALID_PLAN_IDENTITY
    )
    if request.valid == invalid_status:
        issues.append(
            MaterializationIssue(
                "request_status_mismatch",
                "receipt status disagrees with materialization request validity",
            )
        )
    if not request.valid and receipt.issues != request.issues:
        issues.append(
            MaterializationIssue(
                "request_issue_mismatch",
                "receipt issues disagree with pre-execution validation",
            )
        )
    if receipt.target != target:
        issues.append(
            MaterializationIssue(
                "receipt_target_mismatch",
                "receipt target disagrees with explicit execution target",
            )
        )
    expected = (
        canonical_sha256(job),
        canonical_sha256(result),
        canonical_sha256(plan),
    )
    observed = (
        receipt.provenance.job_sha256,
        receipt.provenance.result_sha256,
        receipt.provenance.plan_sha256,
    )
    if observed != expected:
        issues.append(
            MaterializationIssue(
                "receipt_provenance_mismatch",
                "receipt job/result/plan identities do not close",
            )
        )

    if receipt.layout is None:
        if layout_path is not None:
            issues.append(
                MaterializationIssue(
                    "unexpected_layout",
                    "non-materialized receipt cannot bind a layout artifact",
                )
            )
    elif layout_path is None or run_root is None:
        issues.append(
            MaterializationIssue(
                "layout_not_supplied",
                "materialized receipt requires its managed layout for validation",
            )
        )
    else:
        try:
            actual = identify_managed_layout(
                layout_path,
                target=target,
                run_root=run_root,
                run_id=receipt.layout.run_id,
                producer=receipt.layout.producer,
                role=receipt.layout.role,
            )
        except MaterializationExecutionError as exc:
            issues.append(MaterializationIssue("invalid_layout_content", str(exc)))
        else:
            if actual != receipt.layout:
                issues.append(
                    MaterializationIssue(
                        "layout_identity_mismatch",
                        "managed layout content or provenance disagrees with receipt",
                    )
                )
    return MaterializationValidation(valid=not issues, issues=tuple(issues))


def validate_receipt_bound_layout(
    receipt: MaterializationReceipt,
    layout_path: Path,
    *,
    run_root: Path,
) -> MaterializationValidation:
    """Validate the checked bytes and managed provenance bound by a receipt."""

    issues: list[MaterializationIssue] = []
    if not receipt.materialized or receipt.layout is None:
        issues.append(
            MaterializationIssue(
                "receipt_not_materialized",
                "physical verification requires a materialized receipt",
            )
        )
        return MaterializationValidation(False, tuple(issues))
    try:
        actual = identify_managed_layout(
            layout_path,
            target=receipt.target,
            run_root=run_root,
            run_id=receipt.layout.run_id,
            producer=receipt.layout.producer,
            role=receipt.layout.role,
        )
    except MaterializationExecutionError as exc:
        issues.append(MaterializationIssue("invalid_layout_content", str(exc)))
    else:
        if actual != receipt.layout:
            issues.append(
                MaterializationIssue(
                    "layout_identity_mismatch",
                    "checked layout content or managed provenance disagrees with receipt",
                )
            )
    return MaterializationValidation(valid=not issues, issues=tuple(issues))


__all__ = [
    "LayoutArtifactFormat",
    "ManagedLayoutArtifact",
    "MaterializationCompletion",
    "MaterializationExecutionError",
    "MaterializationExecutionProvenance",
    "MaterializationExecutionStatus",
    "MaterializationExecutionTarget",
    "MaterializationReceipt",
    "canonicalize_gdsii_timestamps",
    "identify_managed_layout",
    "issue_materialization_receipt",
    "materialization_execution_target_from_mapping",
    "materialization_receipt_from_json",
    "validate_layout_content",
    "validate_materialization_receipt",
    "validate_materialization_request",
    "validate_receipt_bound_layout",
]
