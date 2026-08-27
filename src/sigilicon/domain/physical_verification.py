"""Project-owned policy applied to platform physical-verification assets."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re
from typing import Any, Mapping

from sigilicon.canonical import canonical_from_json, canonical_json
from sigilicon.domain.config_contracts import read_toml, require_config_header


_HEADER_FIELDS = {"schema", "contract_kind", "path_scope", "owner"}


@dataclass(frozen=True)
class PhysicalVerificationPolicy:
    """Owner decisions layered over immutable foundry deck identities."""

    path: Path
    drc_disabled_defines: Mapping[str, int]
    drc_configuration_warnings: tuple[str, ...]
    drc_waiver_layers: tuple[str, ...]


class PhysicalVerificationStatus(str, Enum):
    CLEAN = "clean"
    VIOLATED = "violated"
    UNSUPPORTED = "unsupported"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    EXECUTION_FAILED = "execution_failed"


def _sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{label} must be a SHA-256 identity")


@dataclass(frozen=True)
class CheckedLayoutIdentity:
    artifact_sha256: str
    plan_sha256: str
    result_sha256: str | None
    owner: str
    name: str

    def __post_init__(self) -> None:
        _sha256(self.artifact_sha256, "layout artifact identity")
        _sha256(self.plan_sha256, "layout plan identity")
        if self.result_sha256 is not None:
            _sha256(self.result_sha256, "physical-design result identity")
        if (
            not isinstance(self.owner, str)
            or not self.owner
            or not isinstance(self.name, str)
            or not self.name
        ):
            raise ValueError("checked layout owner and name must be non-empty")


@dataclass(frozen=True)
class CheckedSourceIdentity:
    artifact_sha256: str
    owner: str
    name: str

    def __post_init__(self) -> None:
        _sha256(self.artifact_sha256, "source artifact identity")
        if (
            not isinstance(self.owner, str)
            or not self.owner
            or not isinstance(self.name, str)
            or not self.name
        ):
            raise ValueError("checked source owner and name must be non-empty")


@dataclass(frozen=True)
class VerificationCompletion:
    backend: str
    executed: bool
    report_parsed: bool
    exit_code: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.backend, str) or not self.backend:
            raise ValueError("verification backend identity must be non-empty")
        if type(self.executed) is not bool or type(self.report_parsed) is not bool:
            raise ValueError("verification completion flags must be booleans")
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise ValueError("verification exit code must be an integer or null")
        if self.report_parsed and not self.executed:
            raise ValueError("an unexecuted verification cannot have a parsed report")
        if self.exit_code is not None and not self.executed:
            raise ValueError("an unexecuted verification cannot have an exit code")

    @property
    def proven(self) -> bool:
        return self.executed and self.report_parsed and self.exit_code == 0


@dataclass(frozen=True)
class DrcViolation:
    rule: str
    count: int

    def __post_init__(self) -> None:
        if not self.rule or type(self.count) is not int or self.count <= 0:
            raise ValueError("DRC violation needs a rule and positive count")


@dataclass(frozen=True)
class LvsMismatch:
    category: str
    count: int

    def __post_init__(self) -> None:
        if not self.category or type(self.count) is not int or self.count <= 0:
            raise ValueError("LVS mismatch needs a category and positive count")


def _validate_conclusion(
    status: PhysicalVerificationStatus,
    completion: VerificationCompletion,
    finding_count: int,
    label: str,
) -> None:
    if not isinstance(status, PhysicalVerificationStatus):
        raise ValueError(f"{label} status must be PhysicalVerificationStatus")
    if status is PhysicalVerificationStatus.CLEAN:
        if not completion.proven or finding_count:
            raise ValueError(
                f"clean {label} requires parsed zero-finding completion"
            )
    elif status is PhysicalVerificationStatus.VIOLATED:
        if not completion.proven or finding_count <= 0:
            raise ValueError(
                f"violated {label} requires parsed positive findings"
            )
    elif status in {
        PhysicalVerificationStatus.UNSUPPORTED,
        PhysicalVerificationStatus.BACKEND_UNAVAILABLE,
    }:
        if completion.executed or completion.report_parsed or completion.exit_code is not None:
            raise ValueError(f"{status.value} {label} cannot claim execution")
        if finding_count:
            raise ValueError(f"{status.value} {label} cannot claim findings")
    elif status is PhysicalVerificationStatus.EXECUTION_FAILED:
        if not completion.executed or completion.proven:
            raise ValueError(
                f"execution_failed {label} requires an incomplete execution"
            )


@dataclass(frozen=True)
class DrcEvidence:
    status: PhysicalVerificationStatus
    layout: CheckedLayoutIdentity
    completion: VerificationCompletion
    violations: tuple[DrcViolation, ...]
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.layout, CheckedLayoutIdentity):
            raise ValueError("DRC evidence needs a checked layout identity")
        if not isinstance(self.completion, VerificationCompletion):
            raise ValueError("DRC evidence needs verification completion")
        if not isinstance(self.violations, tuple) or any(
            not isinstance(item, DrcViolation) for item in self.violations
        ):
            raise ValueError("DRC evidence violations must be a typed tuple")
        if not isinstance(self.message, str):
            raise ValueError("DRC evidence message must be text")
        _validate_conclusion(
            self.status,
            self.completion,
            sum(item.count for item in self.violations),
            "DRC",
        )

    @property
    def clean(self) -> bool:
        return self.status is PhysicalVerificationStatus.CLEAN

    def canonical_json(self) -> str:
        return canonical_json(self)


@dataclass(frozen=True)
class LvsEvidence:
    status: PhysicalVerificationStatus
    layout: CheckedLayoutIdentity
    source: CheckedSourceIdentity
    completion: VerificationCompletion
    mismatches: tuple[LvsMismatch, ...]
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.layout, CheckedLayoutIdentity):
            raise ValueError("LVS evidence needs a checked layout identity")
        if not isinstance(self.source, CheckedSourceIdentity):
            raise ValueError("LVS evidence needs a checked source identity")
        if not isinstance(self.completion, VerificationCompletion):
            raise ValueError("LVS evidence needs verification completion")
        if not isinstance(self.mismatches, tuple) or any(
            not isinstance(item, LvsMismatch) for item in self.mismatches
        ):
            raise ValueError("LVS evidence mismatches must be a typed tuple")
        if not isinstance(self.message, str):
            raise ValueError("LVS evidence message must be text")
        _validate_conclusion(
            self.status,
            self.completion,
            sum(item.count for item in self.mismatches),
            "LVS",
        )

    @property
    def clean(self) -> bool:
        return self.status is PhysicalVerificationStatus.CLEAN

    def canonical_json(self) -> str:
        return canonical_json(self)


PhysicalVerificationEvidence = DrcEvidence | LvsEvidence


def drc_evidence_from_json(text: str) -> DrcEvidence:
    return canonical_from_json(text, DrcEvidence)


def lvs_evidence_from_json(text: str) -> LvsEvidence:
    return canonical_from_json(text, LvsEvidence)


def _table(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a table")
    return value


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{field} must be a string array")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise ValueError(f"{field} contains duplicates")
    return result


def _integer_map(value: object, field: str) -> Mapping[str, int]:
    raw = _table(value, field)
    result: dict[str, int] = {}
    for name, item in raw.items():
        if (
            not isinstance(name, str)
            or not name
            or isinstance(item, bool)
            or not isinstance(item, int)
        ):
            raise ValueError(f"{field} must map names to integers")
        result[name] = item
    return result


def load_physical_verification_policy(
    path: Path,
    *,
    owner: str,
) -> PhysicalVerificationPolicy:
    """Load one strict owner policy without resolving any EDA installation."""

    resolved = path.resolve()
    raw = read_toml(resolved)
    require_config_header(
        raw,
        resolved,
        contract_kind="physical-verification-policy",
        path_scope="owner",
        owner=owner,
    )
    unknown = set(raw) - (_HEADER_FIELDS | {"drc"})
    if unknown:
        raise ValueError(
            f"physical verification policy contains unsupported fields: {sorted(unknown)}"
        )
    drc = _table(raw.get("drc"), "physical verification policy drc")
    unknown_drc = set(drc) - {
        "disabled_defines",
        "configuration_warnings",
        "waiver_layers",
    }
    if unknown_drc:
        raise ValueError(
            f"physical verification drc policy contains unsupported fields: "
            f"{sorted(unknown_drc)}"
        )
    return PhysicalVerificationPolicy(
        path=resolved,
        drc_disabled_defines=_integer_map(
            drc.get("disabled_defines", {}), "drc.disabled_defines"
        ),
        drc_configuration_warnings=_strings(
            drc.get("configuration_warnings", []), "drc.configuration_warnings"
        ),
        drc_waiver_layers=_strings(
            drc.get("waiver_layers", []), "drc.waiver_layers"
        ),
    )
