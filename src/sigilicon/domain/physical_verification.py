"""Project-owned policy applied to platform physical-verification assets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from sigilicon.domain.config_contracts import read_toml, require_config_header


_HEADER_FIELDS = {"schema", "contract_kind", "path_scope", "owner"}


@dataclass(frozen=True)
class PhysicalVerificationPolicy:
    """Owner decisions layered over immutable foundry deck identities."""

    path: Path
    drc_disabled_defines: Mapping[str, int]
    drc_configuration_warnings: tuple[str, ...]
    drc_waiver_layers: tuple[str, ...]


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
