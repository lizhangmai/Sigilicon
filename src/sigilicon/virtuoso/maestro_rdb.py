"""Parse the small, explicit export emitted by Cadence's native RDB API."""

from __future__ import annotations

import math
from pathlib import Path
import re
import statistics
from typing import Any, Collection

def _number(value: str, *, field: str) -> int | float:
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"native Maestro RDB {field} is not numeric: {value!r}") from exc
    if number.is_integer() and "." not in value and "e" not in value.lower():
        return int(number)
    return number


def _value(value: str, *, field: str) -> Any:
    token = value.strip()
    if not token:
        raise ValueError(f"native Maestro RDB {field} is empty")
    if token == "nil":
        return None
    try:
        return _number(token, field=field)
    except ValueError:
        return token.strip('"')


def _expected_identity(
    result: dict[str, Any],
    *,
    expected_point_count: int | None,
    expected_corners: Collection[str] | None,
    expected_tests: Collection[str] | None,
    expected_outputs: Collection[str] | None,
    expected_expression_count: int | None,
) -> None:
    """Check an optional source-owned identity model without inferring one.

    The native setup remains the authority for the actual output model.  This
    hook lets a caller that already has an independently validated model
    compare it with the official RDB identities; the parser never derives a
    model from simulator result paths, aggregate run metadata, Detail CSV, or
    legacy MDL.
    """

    checks = (
        ("point count", expected_point_count, result["point_count"]),
        ("corner identity", expected_corners, result["identity"]["corners"]),
        ("test identity", expected_tests, result["identity"]["tests"]),
        ("output identity", expected_outputs, result["identity"]["outputs"]),
        (
            "expression count",
            expected_expression_count,
            result["expression_count"],
        ),
    )
    for label, expected, actual in checks:
        if expected is None:
            continue
        if label in {"point count", "expression count"}:
            if expected <= 0 or actual != expected:
                raise ValueError(
                    f"native Maestro RDB {label} differs from the expected model: "
                    f"expected {expected}, got {actual}"
                )
            continue
        expected_set = {str(item) for item in expected}
        actual_set = {str(item) for item in actual}
        if expected_set != actual_set:
            raise ValueError(
                f"native Maestro RDB {label} differs from the expected model: "
                f"expected {sorted(expected_set)}, got {sorted(actual_set)}"
            )
    if expected_point_count is None:
        return
    expected_point_ids = set(range(1, expected_point_count + 1))
    actual_point_ids = {
        int(point["point"]) for point in result["identity"]["points"]
    }
    if actual_point_ids != expected_point_ids:
        raise ValueError(
            "native Maestro RDB point identity differs from the expected model: "
            f"expected {sorted(expected_point_ids)}, got {sorted(actual_point_ids)}"
        )
    if (
        expected_corners is None
        or expected_tests is None
        or expected_outputs is None
    ):
        return
    expected_rows = {
        (point, str(corner), str(test), str(output))
        for point in expected_point_ids
        for corner in expected_corners
        for test in expected_tests
        for output in expected_outputs
    }
    actual_rows = {
        (int(row["point"]), row["corner"], row["test"], row["name"])
        for row in result["outputs"]
    }
    if actual_rows != expected_rows:
        raise ValueError(
            "native Maestro RDB Cartesian point/corner/test/output identity "
            "differs from the expected model: "
            f"missing={sorted(expected_rows - actual_rows)}, "
            f"extra={sorted(actual_rows - expected_rows)}"
        )


def read_native_maestro_rdb_export(
    path: Path,
    *,
    expected_point_count: int | None = None,
    expected_corners: Collection[str] | None = None,
    expected_tests: Collection[str] | None = None,
    expected_outputs: Collection[str] | None = None,
    expected_expression_count: int | None = None,
    nullable_outputs: Collection[str] | None = None,
) -> dict[str, Any]:
    """Validate and normalize a worker export made from ``maeReadResDB``.

    The worker writes this tabular envelope with SKILL ``fprintf``.  The
    values and identities are queried from the official read-only result
    objects; this parser does not inspect simulator paths, aggregate run
    metadata, or Detail CSV.
    """

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read native Maestro RDB export: {path}") from exc
    if not lines or lines[0].split("\t") != ["RDB_SCHEMA", "1"]:
        raise ValueError("native Maestro RDB export has an invalid schema header")
    nullable_output_names = {
        str(name) for name in (() if nullable_outputs is None else nullable_outputs)
    }
    outputs: list[dict[str, Any]] = []
    parameters: list[dict[str, Any]] = []
    summary: tuple[int, int] | None = None
    overall_status: Any = None
    for line_number, line in enumerate(lines[1:], start=2):
        fields = line.split("\t")
        if fields[0] == "PARAM":
            if len(fields) != 4:
                raise ValueError(
                    f"native Maestro RDB line {line_number}: malformed PARAM row"
                )
            point = _number(fields[1], field="parameter point")
            if not isinstance(point, int) or point <= 0:
                raise ValueError("native Maestro RDB parameter point must be positive")
            name = fields[2].strip()
            if not name:
                raise ValueError("native Maestro RDB parameter name is empty")
            parameters.append(
                {
                    "point": point,
                    "name": name,
                    "value": _value(fields[3], field=f"parameter {name}"),
                }
            )
        elif fields[0] == "OUTPUT":
            if len(fields) != 7:
                raise ValueError(
                    f"native Maestro RDB line {line_number}: malformed OUTPUT row"
                )
            point = _number(fields[1], field="point")
            if not isinstance(point, int) or point <= 0:
                raise ValueError("native Maestro RDB output point must be positive")
            corner, test, name = fields[2:5]
            if not corner or not test or not name:
                raise ValueError(
                    f"native Maestro RDB line {line_number}: empty output identity"
                )
            value = _value(fields[5], field="value")
            if value is None and name not in nullable_output_names:
                raise ValueError(
                    f"native Maestro RDB output {corner}/{test}/{name} has no scalar value"
                )
            if value is not None and (
                not isinstance(value, (int, float)) or isinstance(value, bool)
            ):
                raise ValueError(
                    f"native Maestro RDB output {corner}/{test}/{name} has no scalar value"
                )
            if value is not None and not math.isfinite(float(value)):
                raise ValueError(
                    f"native Maestro RDB output {corner}/{test}/{name} is not finite"
                )
            status = fields[6].strip()
            if not status:
                raise ValueError("native Maestro RDB output status is empty")
            outputs.append(
                {
                    "point": point,
                    "corner": corner,
                    "test": test,
                    "name": name,
                    "value": value,
                    "spec_status": status.strip('"'),
                }
            )
        elif fields[0] == "SUMMARY":
            if len(fields) != 3 or summary is not None:
                raise ValueError(
                    f"native Maestro RDB line {line_number}: malformed SUMMARY row"
                )
            points = _number(fields[1], field="point count")
            expressions = _number(fields[2], field="expression count")
            if not isinstance(points, int) or not isinstance(expressions, int):
                raise ValueError("native Maestro RDB summary counts must be integers")
            summary = (points, expressions)
        elif fields[0] == "OVERALL_SPEC":
            if len(fields) != 2 or overall_status is not None:
                raise ValueError(
                    f"native Maestro RDB line {line_number}: malformed overall status"
                )
            overall_status = fields[1].strip('"')
        elif line.strip():
            raise ValueError(
                f"native Maestro RDB line {line_number}: unknown record {fields[0]!r}"
            )
    if summary is None or overall_status is None:
        raise ValueError("native Maestro RDB export lacks summary or spec status")
    point_count, expression_count = summary
    if point_count <= 0 or expression_count <= 0:
        raise ValueError("native Maestro RDB export contains no scalar expression outputs")
    if expression_count != len(outputs):
        raise ValueError(
            "native Maestro RDB expression count does not match OUTPUT rows"
        )
    identities = [(row["point"], row["corner"], row["test"], row["name"]) for row in outputs]
    if len(set(identities)) != len(identities):
        raise ValueError("native Maestro RDB export contains duplicate output identities")
    if any(row["point"] > point_count for row in outputs):
        raise ValueError("native Maestro RDB output refers to an unknown point")
    parameter_identities = [
        (row["point"], row["name"]) for row in parameters
    ]
    if len(set(parameter_identities)) != len(parameter_identities):
        raise ValueError("native Maestro RDB export contains duplicate point parameters")
    if any(row["point"] > point_count for row in parameters):
        raise ValueError("native Maestro RDB parameter refers to an unknown point")
    parameters_by_point: dict[int, dict[str, Any]] = {}
    for row in parameters:
        parameters_by_point.setdefault(int(row["point"]), {})[str(row["name"])] = (
            row["value"]
        )
    point_identities: dict[int, dict[str, Any]] = {}
    for row in outputs:
        point = int(row["point"])
        identity = point_identities.setdefault(
            point,
            {"point": point, "corners": set(), "tests": set(), "outputs": set()},
        )
        identity["corners"].add(str(row["corner"]))
        identity["tests"].add(str(row["test"]))
        identity["outputs"].add(str(row["name"]))
    normalized_point_identities = [
        {
            "point": point,
            "corners": sorted(identity["corners"]),
            "tests": sorted(identity["tests"]),
            "outputs": sorted(identity["outputs"]),
            "parameters": dict(sorted(parameters_by_point.get(point, {}).items())),
        }
        for point, identity in sorted(point_identities.items())
    ]
    if len(normalized_point_identities) != point_count:
        raise ValueError(
            "native Maestro RDB point count does not match scalar point identities"
        )
    result = {
        "schema": 1,
        "source": "Cadence maeReadResDB/point->params+point->outputs",
        "point_count": point_count,
        "expression_count": expression_count,
        "scalar_output_count": expression_count,
        "scalar_values_finite": all(row["value"] is not None for row in outputs),
        "nullable_output_count": sum(
            row["value"] is None for row in outputs
        ),
        "overall_spec_status": overall_status,
        "identity": {
            "points": normalized_point_identities,
            "corners": sorted({row["corner"] for row in outputs}),
            "tests": sorted({row["test"] for row in outputs}),
            "outputs": sorted({row["name"] for row in outputs}),
        },
        "outputs": outputs,
        "point_parameters": parameters,
    }
    _expected_identity(
        result,
        expected_point_count=expected_point_count,
        expected_corners=expected_corners,
        expected_tests=expected_tests,
        expected_outputs=expected_outputs,
        expected_expression_count=expected_expression_count,
    )
    return result


def reconstruct_native_legacy_measurement(
    result: dict[str, Any],
    contract: OANativeRdbContract,
) -> dict[str, Any] | None:
    """Rebuild the former MDL sampled-array shape from official RDB rows.

    This is a structured-result transformation only.  It does not inspect a
    simulator result path, infer a corner from a run object, or execute MDL.  The native
    setup owns the sample expressions and the RDB remains the sole numerical
    source.
    """

    legacy = contract.legacy_measurement
    if legacy is None:
        return None
    rows = {
        (
            int(row["point"]),
            str(row["corner"]),
            str(row["test"]),
            str(row["name"]),
        ): row["value"]
        for row in result["outputs"]
    }
    contexts: list[dict[str, Any]] = []
    for point in range(1, contract.point_count + 1):
        for corner in contract.corners:
            for test in contract.tests:
                exports: dict[str, list[Any]] = {
                    "sample_times": list(legacy.sample_times)
                }
                for series in legacy.series:
                    values: list[Any] = []
                    for index in range(legacy.sample_count):
                        for signal_index in range(len(series.signals)):
                            name = series.output_name(index, signal_index)
                            key = (point, corner, test, name)
                            if key not in rows:
                                raise ValueError(
                                    "native RDB is missing legacy sample output "
                                    f"{point}/{corner}/{test}/{name}"
                                )
                            values.append(rows[key])
                    exports[series.export] = values
                contexts.append(
                    {
                        "point": point,
                        "corner": corner,
                        "test": test,
                        "exports": exports,
                    }
                )
    return {
        "schema": 1,
        "format": "native_rdb_legacy_sample_arrays",
        "source": "Cadence maeReadResDB/point->outputs",
        "alias": legacy.alias,
        "sample_times": list(legacy.sample_times),
        "sample_count": legacy.sample_count,
        "series": [
            {
                "export": series.export,
                "signals": list(series.signals),
                "prefix": series.prefix,
            }
            for series in legacy.series
        ],
        "contexts": contexts,
        "product_qualification_conclusion": False,
    }


def reconstruct_native_diagnostic(
    result: dict[str, Any],
    contract: Any,
    legacy_result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Delegate product-owned diagnostic reconstruction to ProjectContext."""

    diagnostic = contract.diagnostic_equivalence
    if diagnostic is None:
        return None
    adapter = getattr(contract, "diagnostic_adapter", None)
    if adapter is None:
        raise RuntimeError("native diagnostic contract has no project adapter")
    return adapter.reconstruct(result, contract, legacy_result)
