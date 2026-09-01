from __future__ import annotations

import copy

import pytest

from sigilicon.flow import (
    FactContractError,
    FactKind,
    FactSchema,
    FactSet,
    FactSource,
    FactSpec,
    FactValueError,
    PolicyCheck,
    PolicySpec,
    bind_policy,
)


def schema() -> FactSchema:
    return FactSchema(
        "test.observe",
        (
            FactSpec("passed", FactKind.BOOLEAN),
            FactSpec("count", FactKind.INTEGER),
            FactSpec("slack", FactKind.REAL, unit="ns"),
            FactSpec("status", FactKind.TEXT, enum_values=("clean", "failed")),
            FactSpec("notes", FactKind.TEXT_LIST, required=False),
        ),
    )


def source() -> FactSource:
    return FactSource("test.observe", "node")


def test_fact_set_projects_and_round_trips_strictly() -> None:
    facts = FactSet(
        schema(),
        {
            "passed": True,
            "count": 2,
            "slack": 0.25,
            "status": "clean",
            "notes": ["report", "parsed"],
        },
        source(),
    )

    assert facts["passed"] is True
    assert facts["notes"] == ("report", "parsed")
    assert FactSet.from_json(facts.to_json(), schema()) == facts


def test_fact_set_rejects_wrong_types() -> None:
    with pytest.raises(FactValueError, match="integer"):
        FactSet(
            schema(),
            {"passed": True, "count": True, "slack": 0.25, "status": "clean"},
            source(),
        )


def test_fact_set_rejects_missing_fields_and_source_drift() -> None:
    values: dict[str, object] = {
        "passed": True,
        "count": 2,
        "slack": 0.25,
        "status": "clean",
    }
    with pytest.raises(FactValueError, match="required"):
        FactSet(schema(), {"passed": True}, source())
    with pytest.raises(FactValueError, match="source action"):
        FactSet(schema(), values, FactSource("test.other", "node"))


def test_fact_set_json_tampering_is_rejected() -> None:
    facts = FactSet(
        schema(),
        {"passed": True, "count": 2, "slack": 0.25, "status": "clean"},
        source(),
    )
    payload = facts.to_json()

    tampered_source = copy.deepcopy(payload)
    tampered_source["source"]["node_id"] = "other"  # type: ignore[index]
    with pytest.raises(FactValueError, match="source drift"):
        FactSet.from_json(tampered_source, schema(), source=source())

def test_fact_schema_rejects_invalid_units_and_enums() -> None:
    with pytest.raises(FactContractError, match="numeric"):
        FactSpec("status", FactKind.TEXT, unit="mode")


def test_policy_binds_fact_and_expected_types_before_execution() -> None:
    policy = PolicySpec(
        "quality",
        (
            PolicyCheck("passed", "passed", "equals", True),
            PolicyCheck("minimum", "count", "at_least", 1),
        ),
    )
    bound = bind_policy(policy, schema())
    assert bound.schema == schema()
    assert bound.checks[0].fact.name == "passed"

    with pytest.raises(FactContractError, match="unknown fact"):
        bind_policy(
            PolicySpec("bad", (PolicyCheck("x", "missing", "exists"),)),
            schema(),
        )
