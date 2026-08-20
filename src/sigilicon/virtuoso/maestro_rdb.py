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


def _context_rows(
    result: dict[str, Any],
    *,
    point: int,
    corner: str,
    test: str,
) -> dict[str, Any]:
    rows = {
        str(row["name"]): row["value"]
        for row in result["outputs"]
        if int(row["point"]) == point
        and str(row["corner"]) == corner
        and str(row["test"]) == test
    }
    if not rows:
        raise ValueError(
            f"native RDB is missing diagnostic context {point}/{corner}/{test}"
        )
    return rows


def _time_seconds(token: str) -> float:
    match = re.fullmatch(
        r"(?P<value>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+\-]?\d+)?)"
        r"(?P<prefix>[afpnum]?)(?:s)?",
        token,
    )
    if match is None:
        raise ValueError(f"invalid native diagnostic time token: {token}")
    return float(match.group("value")) * {
        "": 1.0,
        "a": 1.0e-18,
        "f": 1.0e-15,
        "p": 1.0e-12,
        "n": 1.0e-9,
        "u": 1.0e-6,
        "m": 1.0e-3,
    }[match.group("prefix")]


def _logic(value: Any, threshold: float) -> int:
    return 0 if float(value) < threshold else 1


def _legacy_values(
    context: dict[str, Any],
    legacy: Any,
    series: Any,
    sample_index: int,
) -> tuple[Any, ...]:
    values = context["exports"].get(series.export)
    if not isinstance(values, list):
        raise ValueError(f"native legacy export is missing: {series.export}")
    start = sample_index * len(series.signals)
    end = start + len(series.signals)
    if end > len(values):
        raise ValueError(f"native legacy export is short: {series.export}")
    return tuple(values[start:end])


def _diagnostic_name_value(
    rows: dict[str, Any], name: str, *, context: str
) -> Any:
    if name not in rows:
        raise ValueError(f"native RDB is missing diagnostic output {context}/{name}")
    value = rows[name]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"native diagnostic output {context}/{name} is not numeric")
    if not math.isfinite(float(value)):
        raise ValueError(f"native diagnostic output {context}/{name} is not finite")
    return value


def _diagnostic_settings(contract: Any) -> dict[str, Any]:
    return dict(contract.settings)


def _native_dac_diagnostics(
    result: dict[str, Any],
    contract: Any,
    diagnostic: Any,
    legacy_result: dict[str, Any],
) -> dict[str, Any]:
    settings = _diagnostic_settings(diagnostic)
    legacy = contract.legacy_measurement
    if legacy is None:
        raise ValueError("DAC diagnostics require legacy sampled arrays")
    codes = tuple(
        (*settings["warmup_codes"], *settings["transfer_codes"], *settings["repeat_codes"])
    )
    series_by_export = {series.export: series for series in legacy.series}
    selected = [series_by_export[name] for name in settings["series"]]
    contexts: list[dict[str, Any]] = []
    ideal_lsb = 2.0 * (float(settings["vrefh_v"]) - float(settings["vrefl_v"])) / float(
        settings["denominator_units"]
    )
    for point in range(1, contract.point_count + 1):
        for corner in contract.corners:
            for test in contract.tests:
                context_name = f"{point}/{corner}/{test}"
                rows_by_name = _context_rows(
                    result, point=point, corner=corner, test=test
                )
                legacy_context = next(
                    item
                    for item in legacy_result["contexts"]
                    if item["point"] == point
                    and item["corner"] == corner
                    and item["test"] == test
                )
                rows: list[dict[str, Any]] = []
                for index, code in enumerate(codes):
                    means: dict[str, Any] = {}
                    pps: list[Any] = []
                    for series in selected:
                        for signal_index, signal in enumerate(series.signals):
                            label = signal.strip("/").lower()
                            base = series.output_name(index, signal_index)
                            means[label] = _diagnostic_name_value(
                                rows_by_name,
                                f"diag_mean_{base}",
                                context=context_name,
                            )
                            pps.append(
                                _diagnostic_name_value(
                                    rows_by_name,
                                    f"diag_pp_{base}",
                                    context=context_name,
                                )
                            )
                    vdiff = float(means["vmac_n"]) - float(means["vmac_p"])
                    rows.append(
                        {
                            "campaign_index": index,
                            "code_even": code,
                            "code_pair": [code, code + 1],
                            "vdiff_v": vdiff,
                            "ideal_vdiff_v": (31.5 - code) * ideal_lsb,
                            "common_modes_v": {
                                "vmac": (float(means["vmac_n"]) + float(means["vmac_p"])) / 2.0,
                                "vmag2": (float(means["vmag2_n"]) + float(means["vmag2_p"])) / 2.0,
                                "vmag1": (float(means["vmag1_n"]) + float(means["vmag1_p"])) / 2.0,
                                "vmag0": (float(means["vmag0_n"]) + float(means["vmag0_p"])) / 2.0,
                            },
                            "sample_window_pp_v": max(float(value) for value in pps),
                        }
                    )
                transfer_start = len(settings["warmup_codes"])
                transfer = rows[transfer_start : transfer_start + len(settings["transfer_codes"])]
                xs = [float(row["code_even"]) for row in transfer]
                ys = [float(row["vdiff_v"]) for row in transfer]
                xm, ym = sum(xs) / len(xs), sum(ys) / len(ys)
                denominator = sum((x - xm) ** 2 for x in xs)
                slope = sum(
                    (x - xm) * (y - ym) for x, y in zip(xs, ys, strict=True)
                ) / denominator
                intercept = ym - slope * xm
                fitted_lsb = -slope
                center = intercept + 31.5 * slope
                two_code_steps = [
                    left - right for left, right in zip(ys, ys[1:])
                ]
                dnl = [
                    step / (2.0 * fitted_lsb) - 1.0 for step in two_code_steps
                ]
                gain_error = abs(fitted_lsb / ideal_lsb - 1.0)
                max_dnl = max(abs(value) for value in dnl)
                max_cm = max(
                    abs(float(cm) - float(settings["vcm_v"]))
                    for row in rows
                    for cm in row["common_modes_v"].values()
                )
                max_pp = max(float(row["sample_window_pp_v"]) for row in rows)
                repeat: dict[str, float] = {}
                for repeat_index, code in enumerate(settings["repeat_codes"]):
                    repeat_row = rows[transfer_start + len(settings["transfer_codes"]) + repeat_index]
                    baseline_row = rows[transfer_start + code // 2]
                    repeat[str(code)] = abs(
                        float(repeat_row["vdiff_v"]) - float(baseline_row["vdiff_v"])
                    )
                max_repeat = max(repeat.values())
                checks = [
                    {
                        "name": "all_31_physical_steps_monotonic",
                        "role": "functional_contract",
                        "passed": all(step > 0 for step in two_code_steps),
                        "value": min(two_code_steps),
                        "rule": "every adjacent physical level is strictly ordered",
                    },
                    {
                        "name": "fitted_score_lsb_gain",
                        "role": "diagnostic_measurement",
                        "passed": None,
                        "value": gain_error,
                        "fixed_limit": None,
                    },
                    {
                        "name": "fixed_half_lsb_center",
                        "role": "diagnostic_measurement",
                        "passed": None,
                        "value": center,
                        "fixed_limit": None,
                    },
                    {
                        "name": "physical_step_dnl",
                        "role": "diagnostic_measurement",
                        "passed": None,
                        "value": max_dnl,
                        "fixed_limit": None,
                    },
                    {
                        "name": "differential_common_mode",
                        "role": "diagnostic_measurement",
                        "passed": None,
                        "value": max_cm,
                        "fixed_limit": None,
                    },
                    {
                        "name": "sample_window_settled",
                        "role": "diagnostic_measurement",
                        "passed": None,
                        "value": max_pp,
                        "fixed_limit": None,
                    },
                    {
                        "name": "selected_state_repeatability",
                        "role": "diagnostic_measurement",
                        "passed": None,
                        "value": max_repeat,
                        "fixed_limit": None,
                    },
                ]
                failed = [
                    str(check["name"])
                    for check in checks
                    if check["role"] == "functional_contract"
                    and check["passed"] is not True
                ]
                contexts.append(
                    {
                        "point": point,
                        "corner": corner,
                        "test": test,
                        "rows": rows,
                        "checks": checks,
                        "failed_checks": failed,
                        "passed": not failed,
                        "legacy_sample_arrays": legacy_context["exports"],
                    }
                )
    return {
        "contract_version": 1,
        "format": "native_rdb_dac_phase_transfer",
        "source": "Cadence maeReadResDB/point->outputs",
        "diagnostic_kind": diagnostic.kind,
        "warmup_codes": list(settings["warmup_codes"]),
        "codes_even": list(codes),
        "physical_levels": settings["physical_levels"],
        "digital_output_codes": 64,
        "ideal_score_lsb_v": ideal_lsb,
        "contexts": contexts,
        "product_qualification_conclusion": False,
    }


def _native_controller_diagnostics(
    result: dict[str, Any],
    contract: Any,
    diagnostic: Any,
    legacy_result: dict[str, Any],
) -> dict[str, Any]:
    settings = _diagnostic_settings(diagnostic)
    legacy = contract.legacy_measurement
    if legacy is None:
        raise ValueError("controller diagnostics require legacy sampled arrays")
    series_by_export = {series.export: series for series in legacy.series}
    phase = series_by_export["phase_samples"]
    bits = series_by_export["bit_state_samples"]
    contexts: list[dict[str, Any]] = []
    threshold = float(settings["threshold_v"])
    fall_signals = tuple(settings["fall_signals"])
    for point in range(1, contract.point_count + 1):
        for corner in contract.corners:
            for test in contract.tests:
                context_name = f"{point}/{corner}/{test}"
                rows_by_name = _context_rows(
                    result, point=point, corner=corner, test=test
                )
                legacy_context = next(
                    item for item in legacy_result["contexts"]
                    if item["point"] == point
                    and item["corner"] == corner
                    and item["test"] == test
                )
                rows: list[dict[str, Any]] = []
                checks: list[dict[str, Any]] = []
                for index, pattern in enumerate(settings["patterns"]):
                    reset_index = 2 * index
                    result_index = reset_index + 1
                    phase_reset = _legacy_values(
                        legacy_context, legacy, phase, reset_index
                    )
                    phase_result = _legacy_values(
                        legacy_context, legacy, phase, result_index
                    )
                    bit_reset = _legacy_values(
                        legacy_context, legacy, bits, reset_index
                    )
                    bit_result = _legacy_values(
                        legacy_context, legacy, bits, result_index
                    )
                    reset_states = [
                        _logic(bit_reset[signal_index * 6 + 4], threshold)
                        for signal_index in range(6)
                    ]
                    reset_d = [
                        _logic(bit_reset[signal_index * 6], threshold)
                        for signal_index in range(6)
                    ]
                    reset_z = [
                        _logic(bit_reset[signal_index * 6 + 2], threshold)
                        for signal_index in range(6)
                    ]
                    code_bits = [
                        _logic(bit_result[signal_index * 6], threshold)
                        for signal_index in range(6)
                    ]
                    zero_bits = [
                        _logic(bit_result[signal_index * 6 + 2], threshold)
                        for signal_index in range(6)
                    ]
                    states = [
                        _logic(bit_result[signal_index * 6 + 4], threshold)
                        for signal_index in range(6)
                    ]
                    decoded = (
                        None
                        if any(bit is None for bit in code_bits)
                        else sum(
                            int(value) << bit
                            for value, bit in zip(code_bits, range(5, -1, -1), strict=True)
                        )
                    )
                    falls = [
                        _diagnostic_name_value(
                            rows_by_name,
                            f"diag_fall_{signal.strip('/').lower()}_{index:03d}",
                            context=context_name,
                        )
                        for signal in fall_signals
                    ]
                    ordered = all(
                        float(left) < float(right)
                        for left, right in zip(falls, falls[1:])
                    )
                    reset_ok = (
                        reset_states == [1] * 6
                        and reset_d == [0] * 6
                        and reset_z == [0] * 6
                    )
                    result_ok = (
                        decoded == pattern
                        and states == [0] * 6
                        and all(
                            z is not None and d is not None and z == 1 - d
                            for d, z in zip(code_bits, zero_bits, strict=True)
                        )
                        and _logic(phase_result[4], threshold) == 1
                    )
                    checks.extend(
                        (
                            {
                                "name": f"pattern_{pattern:02d}_reset_state",
                                "passed": reset_ok,
                            },
                            {
                                "name": f"pattern_{pattern:02d}_result",
                                "passed": result_ok,
                                "value": decoded,
                                "expected": pattern,
                            },
                            {
                                "name": f"pattern_{pattern:02d}_decision_order",
                                "passed": ordered,
                                "fall_times_s": [float(value) for value in falls],
                            },
                        )
                    )
                    rows.append(
                        {
                            "pattern": pattern,
                            "decoded": decoded,
                            "code_bits_msb_first": code_bits,
                            "zero_bits_msb_first": zero_bits,
                            "state_fall_times_s": [float(value) for value in falls],
                        }
                    )
                failed = [
                    str(check["name"])
                    for check in checks
                    if not bool(check["passed"])
                ]
                contexts.append(
                    {
                        "point": point,
                        "corner": corner,
                        "test": test,
                        "patterns": list(settings["patterns"]),
                        "rows": rows,
                        "checks": checks,
                        "failed_checks": failed,
                        "passed": not failed,
                    }
                )
    return {
        "contract_version": 1,
        "format": "native_rdb_controller_sequence",
        "source": "Cadence maeReadResDB/point->outputs",
        "diagnostic_kind": diagnostic.kind,
        "patterns": list(settings["patterns"]),
        "logic_decode_reference": "VDD/2",
        "logic_decode_reference_v": threshold,
        "fixed_logic_swing_margin_v": None,
        "contexts": contexts,
        "product_qualification_conclusion": False,
    }


def _native_mx_diagnostics(
    result: dict[str, Any],
    contract: Any,
    diagnostic: Any,
    legacy_result: dict[str, Any],
) -> dict[str, Any]:
    settings = _diagnostic_settings(diagnostic)
    legacy = contract.legacy_measurement
    if legacy is None:
        raise ValueError("MX diagnostics require legacy sampled arrays")
    series_by_export = {series.export: series for series in legacy.series}
    protocol = series_by_export["protocol_samples"]
    decisions = series_by_export["decision_samples"]
    mac = series_by_export["mac_samples"]
    threshold = float(settings["threshold_v"])
    contexts: list[dict[str, Any]] = []
    names = tuple(settings["scenario_names"])
    expected_scores = tuple(settings["expected_scores"])
    active_buffers = tuple(settings["active_buffers"])
    edge_count = int(settings["edge_count"])
    overlap_index = settings.get("overlap_write_b_code_index")
    overlap_code = settings.get("overlap_write_b_code")
    expected_codes = tuple(settings["expected_codes"])
    for point in range(1, contract.point_count + 1):
        for corner in contract.corners:
            for test in contract.tests:
                context_name = f"{point}/{corner}/{test}"
                rows_by_name = _context_rows(
                    result, point=point, corner=corner, test=test
                )
                legacy_context = next(
                    item for item in legacy_result["contexts"]
                    if item["point"] == point
                    and item["corner"] == corner
                    and item["test"] == test
                )
                rows: list[dict[str, Any]] = []
                checks: list[dict[str, Any]] = []
                for index, (name, expected_score, expected, active_buffer) in enumerate(
                    zip(
                        names,
                        expected_scores,
                        expected_codes,
                        active_buffers,
                        strict=True,
                    )
                ):
                    protocol_values = _legacy_values(
                        legacy_context, legacy, protocol, index
                    )
                    decision_values = _legacy_values(
                        legacy_context, legacy, decisions, index
                    )
                    mac_values = _legacy_values(legacy_context, legacy, mac, index)
                    code_bits = [
                        _logic(value, threshold) for value in decision_values
                    ]
                    code = (
                        None
                        if any(bit is None for bit in code_bits)
                        else sum(
                            int(value) << bit
                            for value, bit in zip(code_bits, range(5, -1, -1), strict=True)
                        )
                    )
                    rdy = _logic(protocol_values[0], threshold)
                    edge_values = [
                        _diagnostic_name_value(
                            rows_by_name,
                            f"diag_valid_edge_{index:03d}_{edge:02d}",
                            context=context_name,
                        )
                        for edge in range(edge_count)
                    ]
                    checks.extend(
                        (
                            {
                                "name": f"{name}_code",
                                "passed": code == expected and rdy == 1,
                                "value": code,
                                "expected": expected,
                            },
                            {
                                "name": f"{name}_six_decisions",
                                "passed": len(edge_values) == edge_count,
                                "value": len(edge_values),
                                "expected": edge_count,
                            },
                        )
                    )
                    row = {
                        "name": name,
                        "active_buffer": active_buffer,
                        "expected_score": expected_score,
                        "expected_code": expected,
                        "code": code,
                        "code_bits_msb_first": code_bits,
                        "rdy": rdy,
                        "valid_rising_edges_s": [float(value) for value in edge_values],
                        "vmac_p_v": mac_values[0],
                        "vmac_n_v": mac_values[1],
                    }
                    if overlap_index is not None and index == int(overlap_index):
                        row["overlap_write_b_code"] = overlap_code
                    rows.append(row)
                by_name = {row["name"]: row for row in rows}
                if diagnostic.kind == "mx_valid_edges" and len(names) == 4:
                    expected_by_name = {
                        name: expected
                        for name, expected in zip(
                            names, expected_codes, strict=True
                        )
                    }
                    checks.extend(
                        (
                            {
                                "name": "inactive_b_write_did_not_corrupt_active_a",
                                "passed": by_name["buffer_a_plus1"]["code"]
                                == by_name["buffer_a_plus1_while_rewriting_b"]["code"]
                                == expected_by_name["buffer_a_plus1"],
                            },
                            {
                                "name": "inactive_b_write_visible_after_idle_swap",
                                "passed": by_name["buffer_b_minus1"]["code"]
                                == expected_by_name["buffer_b_minus1"]
                                and by_name["buffer_b_rewritten_plus1"]["code"]
                                == expected_by_name["buffer_b_rewritten_plus1"],
                            },
                        )
                    )
                failed = [
                    str(check["name"])
                    for check in checks
                    if not bool(check["passed"])
                ]
                contexts.append(
                    {
                        "point": point,
                        "corner": corner,
                        "test": test,
                        "rows": rows,
                        "checks": checks,
                        "failed_checks": failed,
                        "passed": not failed,
                    }
                )
    base = {
        "contract_version": 1,
        "format": (
            "native_rdb_mx_bounded_full_array"
            if settings["bounded_campaign"]
            else "native_rdb_mx_dual_buffer"
        ),
        "source": "Cadence maeReadResDB/point->outputs",
        "diagnostic_kind": diagnostic.kind,
        "contexts": contexts,
        "logic_decode_reference": "VDD/2",
        "logic_decode_reference_v": threshold,
        "fixed_logic_swing_margin_v": None,
        "mapping": dict(settings["code_mapping"]),
        "mapping_source": settings["code_mapping_contract"],
        "comparator_mismatch_qualified": False,
        "pvt_qualified": False,
        "product_qualification_conclusion": False,
    }
    if settings["bounded_campaign"]:
        base["campaign"] = {
            "passed": True,
            "bounded_canonical_setup": True,
            "scenario_names": list(names),
            "expected_scores": list(expected_scores),
        }
    else:
        base["programming"] = {"buffer_a_row0": "0x1", "buffer_b_row0": "0xe"}
        base["scenarios"] = contexts[0]["rows"] if len(contexts) == 1 else [
            row for context in contexts for row in context["rows"]
        ]
    return base


def _native_frontend_diagnostics(
    result: dict[str, Any],
    contract: Any,
    diagnostic: Any,
    legacy_result: dict[str, Any] | None,
) -> dict[str, Any]:
    settings = _diagnostic_settings(diagnostic)
    legacy = contract.legacy_measurement
    if legacy is not None and legacy_result is None:
        raise ValueError("frontend legacy contract requires legacy RDB result")
    series_by_export = (
        {} if legacy is None else {series.export: series for series in legacy.series}
    )
    threshold = float(settings["threshold_v"])
    warmup = tuple(settings["warmup_scores"])
    scores = tuple(settings["scores"])
    expanded_scores = (*warmup, *scores)
    edge_count = int(settings["edge_count"])
    expected_codes = tuple(settings["expected_codes"])
    contexts: list[dict[str, Any]] = []
    reference_maps = {
        name: dict(settings[f"{name}_by_corner"])
        for name in ("vcm_dac", "vcm_adc", "vcm_cal")
    }
    for point in range(1, contract.point_count + 1):
        for corner in contract.corners:
            for test in contract.tests:
                context_name = f"{point}/{corner}/{test}"
                rows_by_name = _context_rows(
                    result, point=point, corner=corner, test=test
                )
                legacy_context = None
                if legacy is not None and legacy_result is not None:
                    legacy_context = next(
                        item for item in legacy_result["contexts"]
                        if item["point"] == point
                        and item["corner"] == corner
                        and item["test"] == test
                    )
                rows: list[dict[str, Any]] = []
                checks: list[dict[str, Any]] = []
                for index, (score, expected) in enumerate(
                    zip(expanded_scores, expected_codes, strict=True)
                ):
                    legacy_fixed_sample = None
                    if legacy is not None and legacy_context is not None:
                        protocol = series_by_export["protocol_samples"]
                        comparator = series_by_export["comparator_samples"]
                        mac = series_by_export["mac_samples"]
                        decision = series_by_export["decision_samples"]
                        protocol_values = _legacy_values(
                            legacy_context, legacy, protocol, index
                        )
                        comparator_values = _legacy_values(
                            legacy_context, legacy, comparator, index
                        )
                        mac_values = _legacy_values(
                            legacy_context, legacy, mac, index
                        )
                        decision_values = _legacy_values(
                            legacy_context, legacy, decision, index
                        )
                        legacy_bits = [
                            _logic(value, threshold) for value in decision_values
                        ]
                        legacy_code = (
                            None
                            if any(bit is None for bit in legacy_bits)
                            else sum(
                                int(value) << bit
                                for value, bit in zip(
                                    legacy_bits, range(5, -1, -1), strict=True
                                )
                            )
                        )
                        legacy_fixed_sample = {
                            "sample_time": legacy.sample_times[index],
                            "rdy": _logic(protocol_values[0], threshold),
                            "code": legacy_code,
                            "code_bits_msb_first": legacy_bits,
                            "vmac_p_v": mac_values[0],
                            "vmac_n_v": mac_values[1],
                            "comparator_sample_v": list(comparator_values),
                            "primary_result": False,
                        }
                    ready_edge = _diagnostic_name_value(
                        rows_by_name,
                        f"diag_ready_edge_{index:03d}",
                        context=context_name,
                    )
                    result_values = {
                        signal: _diagnostic_name_value(
                            rows_by_name,
                            f"diag_result_{signal.strip('/').lower()}_{index:03d}",
                            context=context_name,
                        )
                        for signal in settings["result_signals"]
                    }
                    bits = [
                        _logic(result_values[f"/D{bit}"], threshold)
                        for bit in range(5, -1, -1)
                    ]
                    code = (
                        None
                        if any(bit is None for bit in bits)
                        else sum(
                            int(value) << bit
                            for value, bit in zip(bits, range(5, -1, -1), strict=True)
                        )
                    )
                    rdy = 1
                    edges = [
                        _diagnostic_name_value(
                            rows_by_name,
                            f"diag_valid_edge_{index:03d}_{edge:02d}",
                            context=context_name,
                        )
                        for edge in range(edge_count)
                    ]
                    code_bits_decodable = code is not None
                    code_mapping_ok = code == expected
                    result_ready = rdy == 1
                    pulse_ok = len(edges) == int(settings["decisions"])
                    if index >= len(warmup):
                        checks.extend(
                            (
                                {
                                    "name": f"score_{score}_code_bits_decodable",
                                    "passed": code_bits_decodable,
                                    "value": code,
                                },
                                {
                                    "name": f"score_{score}_code_mapping",
                                    "passed": code_mapping_ok,
                                    "value": code,
                                    "expected": expected,
                                },
                                {
                                    "name": f"score_{score}_result_ready",
                                    "passed": result_ready,
                                    "value": rdy,
                                    "expected": 1,
                                },
                                {
                                    "name": f"score_{score}_six_decisions",
                                    "passed": pulse_ok,
                                    "value": len(edges),
                                    "expected": int(settings["decisions"]),
                                },
                            )
                        )
                    rows.append(
                        {
                            "warmup": index < len(warmup),
                            "score": score,
                            "expected_code": expected,
                            "code": code,
                            "code_error": None if code is None else code - expected,
                            "code_bits_msb_first": bits,
                            "rdy": rdy,
                            "rdy_rising_edge_s": float(ready_edge),
                            "result_sample_s": (
                                float(ready_edge)
                                + _time_seconds(
                                    str(settings["result_sample_delay"])
                                )
                            ),
                            "valid_rising_edges_s": [float(value) for value in edges],
                            "conversion_time_s": float(ready_edge)
                            - _time_seconds(str(settings["window_starts"][index])),
                            "completion_margin_to_window_end_s": (
                                _time_seconds(str(settings["window_ends"][index]))
                                - float(ready_edge)
                            ),
                            "decision_samples": [
                                {"valid_edge_s": float(edge_time)}
                                for edge_time in edges
                            ],
                            "legacy_fixed_sample": legacy_fixed_sample,
                        }
                    )
                failed = [
                    str(check["name"])
                    for check in checks
                    if not bool(check["passed"])
                ]
                reference_values = {
                    name: float(values[corner])
                    for name, values in reference_maps.items()
                }
                contexts.append(
                    {
                        "point": point,
                        "corner": corner,
                        "test": test,
                        "campaign": f"representative_{corner}",
                        "reference_values_v": reference_values,
                        "scores": list(scores),
                        "rows": rows,
                        "checks": checks,
                        "failed_checks": failed,
                        "passed": not failed,
                    }
                )
    return {
        "contract_version": 1,
        "format": "native_rdb_frontend_autonomous",
        "source": "Cadence maeReadResDB/point->outputs",
        "diagnostic_kind": diagnostic.kind,
        "warmup_scores": list(warmup),
        "scores": list(scores),
        "diagnostic_equal_drive_values_v": list(
            settings["diagnostic_equal_drive_values_v"]
        ),
        "common_modes_are_product_operating_range_endpoints": False,
        "history_directions": list(settings["history_directions"]),
        "history_directions_have_numeric_hysteresis_threshold": False,
        "mapping": dict(settings["code_mapping"]),
        "mapping_source": settings["code_mapping_contract"],
        "exact_score_window": list(settings["exact_window"]),
        "raw_score_domain": list(settings["raw_domain"]),
        "required_decisions_per_conversion": int(settings["decisions"]),
        "decision_count_is_required_functional_contract": True,
        "result_sample_is_fixed_time": False,
        "result_sample_rule": "first RDY rising edge plus configured settle",
        "result_sample_delay_s": _time_seconds(
            str(settings["result_sample_delay"])
        ),
        "legacy_fixed_samples_retained_for_equivalence": legacy is not None,
        "legacy_fixed_samples_are_primary_results": False,
        "analog_diagnostics_are_scalarized": False,
        "analog_diagnostics_source": "saved native waveforms",
        "logic_decode_reference_v": threshold,
        "logic_decode_reference_is_derived_from_vdd": True,
        "contexts": contexts,
        "product_qualification_conclusion": False,
    }


def _native_bank_calibration_trajectory(
    result: dict[str, Any],
    contract: Any,
    diagnostic: Any,
) -> dict[str, Any]:
    """Group native Bank decision and transfer scalars by MC point/path."""

    settings = diagnostic.settings
    paths = int(settings["paths"])
    decision_times = tuple(settings["decision_sample_times"])
    transfer_times = tuple(settings["transfer_sample_times"])
    contexts: list[dict[str, Any]] = []
    for point in range(1, contract.point_count + 1):
        for corner in contract.corners:
            for test in contract.tests:
                values = _context_rows(
                    result, point=point, corner=corner, test=test
                )
                path_rows: list[dict[str, Any]] = []
                for path in range(paths):
                    decisions: list[dict[str, Any]] = []
                    for step, sample_time in enumerate(decision_times):
                        differential = float(
                            values[
                                f"diag_bank_decision_diff_{step:03d}_{path:02d}"
                            ]
                        )
                        onehot_product = float(
                            values[
                                f"diag_bank_decision_onehot_{step:03d}_{path:02d}"
                            ]
                        )
                        decisions.append(
                            {
                                "step": step,
                                "sample_time": sample_time,
                                "output_differential_v": differential,
                                "decision": (
                                    1 if differential > 0.0
                                    else 0 if differential < 0.0
                                    else None
                                ),
                                "one_hot": onehot_product < 0.0,
                                "one_hot_product_v2": onehot_product,
                            }
                        )
                    transfers = [
                        {
                            "step": step,
                            "sample_time": sample_time,
                            "vcal_differential_v": float(
                                values[
                                    f"diag_bank_vcal_diff_{step:03d}_{path:02d}"
                                ]
                            ),
                        }
                        for step, sample_time in enumerate(transfer_times)
                    ]
                    path_rows.append(
                        {
                            "path": path,
                            "decisions": decisions,
                            "transfers": transfers,
                            "all_decisions_one_hot": all(
                                bool(row["one_hot"]) for row in decisions
                            ),
                        }
                    )
                contexts.append(
                    {
                        "point": point,
                        "corner": corner,
                        "test": test,
                        "paths": path_rows,
                    }
                )
    return {
        "contract_version": 1,
        "format": "native_rdb_bank_calibration_trajectory",
        "source": "Cadence maeReadResDB/point->outputs",
        "diagnostic_kind": diagnostic.kind,
        "paths_per_bank": paths,
        "decision_sample_times": list(decision_times),
        "transfer_sample_times": list(transfer_times),
        "decision_decode_threshold_v": float(settings["threshold_v"]),
        "contexts": contexts,
        "raw_offset_measured": False,
        "post_calibration_residual_measured": False,
        "product_qualification_conclusion": False,
    }


def _native_bank_calibration_measurement(
    result: dict[str, Any],
    contract: Any,
    diagnostic: Any,
) -> dict[str, Any]:
    """Reconstruct in-point bidirectional Bank boundaries from official RDB."""

    settings = diagnostic.settings
    paths = int(settings["paths"])
    directions = tuple(str(value) for value in settings["boundary_directions"])
    supply = float(settings["supply_v"])
    decision_times = tuple(settings["decision_sample_times"])
    transfer_times = tuple(settings["transfer_sample_times"])
    expected_model_file = str(settings["model_file"])
    expected_model_sections = tuple(
        str(section) for section in settings["model_sections"]
    )

    parameter_rows = result.get("point_parameters")
    if not isinstance(parameter_rows, list):
        raise ValueError("native Bank measurement requires official RDB point parameters")
    parameters: dict[int, dict[str, Any]] = {}
    for row in parameter_rows:
        if not isinstance(row, dict):
            raise ValueError("native RDB point parameter row must be an object")
        point = row.get("point")
        name = row.get("name")
        if not isinstance(point, int) or not isinstance(name, str):
            raise ValueError("native RDB point parameter identity is invalid")
        parameters.setdefault(point, {})[name] = row.get("value")

    def numeric_parameter(point: int, names: tuple[str, ...]) -> float:
        point_parameters = parameters.get(point, {})
        for name in names:
            if name not in point_parameters:
                continue
            try:
                value = float(point_parameters[name])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"native RDB point {point} parameter {name} is not numeric"
                ) from exc
            if not math.isfinite(value):
                raise ValueError(
                    f"native RDB point {point} parameter {name} is not finite"
                )
            return value
        raise ValueError(
            f"native RDB point {point} lacks parameter identity {names}"
        )

    def phase_result(
        values: dict[str, Any],
        *,
        phase: str,
        direction: str,
        path: int,
    ) -> dict[str, Any]:
        boundary_value = values[
            f"diag_bank_{phase}_{direction}_boundary_{path:02d}"
        ]
        boundary = None if boundary_value is None else float(boundary_value)
        onehot_product = float(
            values[f"diag_bank_{phase}_{direction}_onehot_{path:02d}"]
        )
        minimum = float(settings[f"{phase}_sweep_min_v"])
        maximum = float(settings[f"{phase}_sweep_max_v"])
        boundary_in_range = (
            boundary is not None and minimum <= boundary <= maximum
        )
        one_hot = onehot_product < 0.0
        return {
            "phase": phase,
            "direction": direction,
            "window_start": settings[f"{phase}_{direction}_start"],
            "window_stop": settings[f"{phase}_{direction}_stop"],
            "sample_period": settings[f"{phase}_sample_period"],
            "calculator_cross_type": (
                "rising" if direction == "ascending" else "falling"
            ),
            "boundary_v": boundary,
            "offset_or_residual_v": (
                None if boundary is None else -boundary
            ),
            "boundary_in_range": boundary_in_range,
            "decision_onehot_maximum_product_v2": onehot_product,
            "all_sampled_decisions_one_hot": one_hot,
            "directed_transition_found": boundary is not None,
            "complete": boundary_in_range and one_hot,
        }

    point_contexts: dict[int, dict[str, Any]] = {}
    point_scalar_signatures: dict[int, tuple[tuple[str, Any], ...]] = {}
    for point in range(1, contract.point_count + 1):
        sequence_value = numeric_parameter(
            point,
            ("monteCarlo::param::sequence",),
        )
        if not sequence_value.is_integer() or sequence_value <= 0:
            raise ValueError(
                f"native RDB point {point} has invalid Monte Carlo sequence"
            )
        sequence = int(sequence_value)
        if sequence in point_contexts:
            raise ValueError(
                f"native RDB Monte Carlo sequence {sequence} is duplicated"
            )
        model_spec = str(parameters.get(point, {}).get("corModelSpec", ""))
        missing_model_tokens = [
            token
            for token in (expected_model_file, *expected_model_sections)
            if token not in model_spec
        ]
        if missing_model_tokens:
            raise ValueError(
                f"native RDB point {point} model identity is incomplete: "
                f"missing={missing_model_tokens}"
            )
        values = _context_rows(
            result,
            point=point,
            corner=contract.corners[0],
            test=contract.tests[0],
        )
        point_scalar_signatures[sequence] = tuple(sorted(values.items()))
        shared_vgc_weight_trajectory = [
            {
                "step": step,
                "sample_time": sample_time,
                "vgc_minus_vcm_cal_v": float(
                    values[f"bank_vgc_weight_{step:03d}"]
                ),
            }
            for step, sample_time in enumerate(transfer_times)
            if f"bank_vgc_weight_{step:03d}" in values
        ]
        path_rows: list[dict[str, Any]] = []
        for path in range(paths):
            decisions = []
            for step, sample_time in enumerate(decision_times):
                differential = float(
                    values[f"diag_bank_decision_diff_{step:03d}_{path:02d}"]
                )
                onehot_product = float(
                    values[f"diag_bank_decision_onehot_{step:03d}_{path:02d}"]
                )
                decisions.append(
                    {
                        "step": step,
                        "sample_time": sample_time,
                        "output_differential_v": differential,
                        "decision": (
                            1 if differential > 0.0
                            else 0 if differential < 0.0
                            else None
                        ),
                        "one_hot": onehot_product < 0.0,
                        "one_hot_product_v2": onehot_product,
                    }
                )
            transfers = [
                {
                    "step": step,
                    "sample_time": sample_time,
                    "vcal_differential_v": float(
                        values[f"diag_bank_vcal_diff_{step:03d}_{path:02d}"]
                    ),
                }
                for step, sample_time in enumerate(transfer_times)
            ]
            hold = (
                float(values[f"bank_hold_vcalp_{path}"]),
                float(values[f"bank_hold_vcaln_{path}"]),
            )
            retention_hold = (
                float(values[f"bank_retention_vcalp_{path}"]),
                float(values[f"bank_retention_vcaln_{path}"]),
            )
            positive_polarity = (
                float(values[f"bank_normal_positive_outp_{path}"])
                > float(settings["threshold_v"])
                > float(values[f"bank_normal_positive_outn_{path}"])
            )
            negative_polarity = (
                float(values[f"bank_normal_negative_outn_{path}"])
                > float(settings["threshold_v"])
                > float(values[f"bank_normal_negative_outp_{path}"])
            )
            phases: dict[str, dict[str, Any]] = {}
            for phase in ("raw", "post", "retention"):
                direction_results = {
                    direction: phase_result(
                        values, phase=phase, direction=direction, path=path
                    )
                    for direction in directions
                }
                boundaries = tuple(
                    direction_results[direction]["boundary_v"]
                    for direction in directions
                )
                complete = all(
                    bool(direction_results[direction]["complete"])
                    for direction in directions
                )
                mean_boundary = (
                    statistics.fmean(float(value) for value in boundaries)
                    if all(value is not None for value in boundaries)
                    else None
                )
                phases[phase] = {
                    **direction_results,
                    "complete": complete,
                    "mean_boundary_v": mean_boundary,
                    "offset_or_residual_v": (
                        None if mean_boundary is None else -mean_boundary
                    ),
                    "direction_boundary_difference_v": (
                        float(boundaries[0]) - float(boundaries[1])
                        if all(value is not None for value in boundaries)
                        else None
                    ),
                }
            raw_offset = phases["raw"]["offset_or_residual_v"]
            post_residual = phases["post"]["offset_or_residual_v"]
            retention_residual = phases["retention"]["offset_or_residual_v"]
            convergence_value = (
                float(raw_offset) - float(post_residual)
                if raw_offset is not None and post_residual is not None
                else None
            )
            path_rows.append(
                {
                    "path": path,
                    "raw": phases["raw"],
                    "post": phases["post"],
                    "retention": phases["retention"],
                    "raw_offset_v": raw_offset,
                    "post_calibration_residual_v": post_residual,
                    "retention_residual_v": retention_residual,
                    "signed_calibration_correction_v": convergence_value,
                    "absolute_residual_reduction_v": (
                        abs(float(raw_offset)) - abs(float(post_residual))
                        if raw_offset is not None and post_residual is not None
                        else None
                    ),
                    "calibration_decisions": decisions,
                    "vcal_transfer_trajectory": transfers,
                    "all_calibration_decisions_one_hot": all(
                        bool(row["one_hot"]) for row in decisions
                    ),
                    "positive_polarity": positive_polarity,
                    "negative_polarity": negative_polarity,
                    "hold_vcalp_v": hold[0],
                    "hold_vcaln_v": hold[1],
                    "hold_rail_legal": all(0.0 <= value <= supply for value in hold),
                    "hold_rail_saturated": any(
                        value <= 0.0 or value >= supply for value in hold
                    ),
                    "retention_hold_time_s": float(
                        settings["retention_hold_time_s"]
                    ),
                    "retention_hold_vcalp_v": retention_hold[0],
                    "retention_hold_vcaln_v": retention_hold[1],
                    "retention_hold_rail_legal": all(
                        0.0 <= value <= supply for value in retention_hold
                    ),
                    "retention_hold_rail_saturated": any(
                        value <= 0.0 or value >= supply
                        for value in retention_hold
                    ),
                }
            )
        point_contexts[sequence] = {
            "point": point,
            "monte_carlo_sequence": sequence,
            "parameters": dict(sorted(parameters.get(point, {}).items())),
            "shared_vgc_weight_trajectory": shared_vgc_weight_trajectory,
            "paths": path_rows,
        }

    expected_sequences = set(range(1, int(settings["monte_carlo_samples"]) + 1))
    if set(point_contexts) != expected_sequences:
        raise ValueError(
            "native RDB Monte Carlo sequence identity is incomplete: "
            f"expected={sorted(expected_sequences)}, got={sorted(point_contexts)}"
        )
    raw_offsets: list[float] = []
    post_residuals: list[float] = []
    retention_residuals: list[float] = []
    convergence: list[float] = []
    contexts: list[dict[str, Any]] = []
    for sequence in sorted(point_contexts):
        context = point_contexts[sequence]
        for path in context["paths"]:
            if path["raw_offset_v"] is not None:
                raw_offsets.append(float(path["raw_offset_v"]))
            if path["post_calibration_residual_v"] is not None:
                post_residuals.append(float(path["post_calibration_residual_v"]))
            if path["retention_residual_v"] is not None:
                retention_residuals.append(float(path["retention_residual_v"]))
            if path["signed_calibration_correction_v"] is not None:
                convergence.append(float(path["signed_calibration_correction_v"]))
        contexts.append(context)

    def sample_statistics(values: list[float]) -> dict[str, Any]:
        return {
            "count": len(values),
            "minimum_v": min(values) if values else None,
            "maximum_v": max(values) if values else None,
            "mean_v": statistics.fmean(values) if values else None,
            "sample_stddev_v": statistics.stdev(values) if len(values) > 1 else None,
        }

    path_rows = [path for context in contexts for path in context["paths"]]

    phase_rows: dict[str, list[tuple[float, float, float]]] = {
        "phi1": [],
        "phi2": [],
    }
    transfer_direction_rows: dict[int, list[bool]] = {
        step: [] for step in range(len(transfer_times))
    }
    for context in contexts:
        weights = {
            int(row["step"]): float(row["vgc_minus_vcm_cal_v"])
            for row in context["shared_vgc_weight_trajectory"]
        }
        for path in context["paths"]:
            decisions = {
                int(row["step"]): row["decision"]
                for row in path["calibration_decisions"]
            }
            transfers = {
                int(row["step"]): float(row["vcal_differential_v"])
                for row in path["vcal_transfer_trajectory"]
            }
            for step in range(len(transfer_times)):
                decision = decisions.get(step)
                weight = weights.get(step)
                next_state = transfers.get(step)
                previous_state = 0.0 if step == 0 else transfers.get(step - 1)
                if (
                    decision not in (0, 1)
                    or weight is None
                    or next_state is None
                    or previous_state is None
                ):
                    continue
                signed_weight = (1.0 if decision == 0 else -1.0) * weight
                phase_rows["phi1" if step % 2 == 0 else "phi2"].append(
                    (previous_state, signed_weight, next_state)
                )
                increment = next_state - previous_state
                transfer_direction_rows[step].append(
                    increment > 0.0 if decision == 0 else increment < 0.0
                )

    def affine_phase_fit(
        rows: list[tuple[float, float, float]], *, steps: tuple[int, ...]
    ) -> dict[str, Any]:
        """Fit x_next=alpha*x_prev+beta*s*a+epsilon without NumPy."""

        base = {
            "phase_steps": list(steps),
            "sample_count": len(rows),
            "role": "pooled diagnostic attribution, not a qualification threshold",
        }
        if len(rows) < 3:
            return {**base, "fit_available": False, "reason": "insufficient_samples"}
        previous = [row[0] for row in rows]
        inputs = [row[1] for row in rows]
        outputs = [row[2] for row in rows]
        mean_previous = statistics.fmean(previous)
        mean_input = statistics.fmean(inputs)
        mean_output = statistics.fmean(outputs)
        centered = [
            (x - mean_previous, u - mean_input, y - mean_output)
            for x, u, y in rows
        ]
        sxx = math.fsum(x * x for x, _, _ in centered)
        suu = math.fsum(u * u for _, u, _ in centered)
        sxu = math.fsum(x * u for x, u, _ in centered)
        sxy = math.fsum(x * y for x, _, y in centered)
        suy = math.fsum(u * y for _, u, y in centered)
        determinant = sxx * suu - sxu * sxu
        scale = max(sxx * suu, 1.0)
        if abs(determinant) <= math.ulp(scale) * 16.0:
            return {**base, "fit_available": False, "reason": "singular_inputs"}
        alpha = (sxy * suu - suy * sxu) / determinant
        beta = (suy * sxx - sxy * sxu) / determinant
        epsilon = mean_output - alpha * mean_previous - beta * mean_input
        residuals = [
            y - (alpha * x + beta * u + epsilon) for x, u, y in rows
        ]
        return {
            **base,
            "fit_available": True,
            "alpha": alpha,
            "beta": beta,
            "epsilon_v": epsilon,
            "rmse_v": math.sqrt(
                math.fsum(value * value for value in residuals) / len(residuals)
            ),
        }

    finite_gain_model = {
        "equation": "x_next = alpha*x_previous + beta*s*a + epsilon",
        "sign_convention": "s=+1 for OUTP=0; s=-1 for OUTP=1",
        "ideal_reference": {"alpha": 1.0, "beta": 1.0, "epsilon_v": 0.0},
        "phi1": affine_phase_fit(phase_rows["phi1"], steps=(0, 2, 4, 6)),
        "phi2": affine_phase_fit(phase_rows["phi2"], steps=(1, 3, 5)),
    }
    transfer_direction_diagnostics = [
        {
            "step": step,
            "phase": "phi1" if step % 2 == 0 else "phi2",
            "net_increment_direction_matches_update": sum(rows),
            "sample_count": len(rows),
            "role": (
                "diagnostic only; alpha state decay can dominate the net increment"
            ),
        }
        for step, rows in transfer_direction_rows.items()
    ]

    distinct_scalar_point_count = len(set(point_scalar_signatures.values()))
    mismatch_variation_observed = (
        distinct_scalar_point_count > 1
        if int(settings["monte_carlo_samples"]) > 1
        else None
    )
    return {
        "contract_version": 1,
        "format": "native_rdb_bank_calibration_measurement",
        "source": "Cadence maeReadResDB/point->params+point->outputs",
        "diagnostic_kind": diagnostic.kind,
        "test": contract.tests[0],
        "corner": contract.corners[0],
        "run_mode": "Monte Carlo Sampling",
        "variation": "mismatch",
        "monte_carlo_samples": int(settings["monte_carlo_samples"]),
        "model_file": expected_model_file,
        "model_sections": list(expected_model_sections),
        "distinct_scalar_point_count": distinct_scalar_point_count,
        "mismatch_variation_observed": mismatch_variation_observed,
        "boundary_directions": list(directions),
        "rdb_point_count": contract.point_count,
        "paths_per_bank": paths,
        "raw_sweep": {
            "electrical_source": "canonical testbench.scs PWL",
            "half_span_v": float(settings["raw_half_span_v"]),
            "minimum_v": float(settings["raw_sweep_min_v"]),
            "maximum_v": float(settings["raw_sweep_max_v"]),
            "step_v": float(settings["raw_sweep_step_v"]),
            "ascending_window": [
                settings["raw_ascending_start"],
                settings["raw_ascending_stop"],
            ],
            "descending_window": [
                settings["raw_descending_start"],
                settings["raw_descending_stop"],
            ],
            "sample_period": settings["raw_sample_period"],
            "role": "diagnostic configuration, not a qualification threshold",
        },
        "post_sweep": {
            "electrical_source": "canonical testbench.scs PWL",
            "half_span_v": float(settings["post_half_span_v"]),
            "minimum_v": float(settings["post_sweep_min_v"]),
            "maximum_v": float(settings["post_sweep_max_v"]),
            "step_v": float(settings["post_sweep_step_v"]),
            "ascending_window": [
                settings["post_ascending_start"],
                settings["post_ascending_stop"],
            ],
            "descending_window": [
                settings["post_descending_start"],
                settings["post_descending_stop"],
            ],
            "sample_period": settings["post_sample_period"],
            "role": "diagnostic configuration, not a qualification threshold",
        },
        "retention_sweep": {
            "electrical_source": "canonical testbench.scs PWL after held C1 state",
            "hold_time_s": float(settings["retention_hold_time_s"]),
            "hold_start": settings["retention_hold_start"],
            "hold_sample": settings["retention_hold_sample"],
            "half_span_v": float(settings["retention_half_span_v"]),
            "minimum_v": float(settings["retention_sweep_min_v"]),
            "maximum_v": float(settings["retention_sweep_max_v"]),
            "step_v": float(settings["retention_sweep_step_v"]),
            "ascending_window": [
                settings["retention_ascending_start"],
                settings["retention_ascending_stop"],
            ],
            "descending_window": [
                settings["retention_descending_start"],
                settings["retention_descending_stop"],
            ],
            "sample_period": settings["retention_sample_period"],
            "role": "native retention evidence; pass limits come from qualification.toml",
        },
        "decision_sample_times": list(decision_times),
        "transfer_sample_times": list(transfer_times),
        "raw_offset_statistics": sample_statistics(raw_offsets),
        "post_residual_statistics": sample_statistics(post_residuals),
        "retention_residual_statistics": sample_statistics(retention_residuals),
        "signed_correction_statistics": sample_statistics(convergence),
        "finite_gain_model": finite_gain_model,
        "transfer_direction_diagnostics": transfer_direction_diagnostics,
        "complete_raw_post_paths": sum(
            bool(path["raw"]["complete"] and path["post"]["complete"])
            for path in path_rows
        ),
        "total_paths": len(path_rows),
        "all_boundary_measurements_complete": all(
            bool(path["raw"]["complete"] and path["post"]["complete"])
            for path in path_rows
        ),
        "all_calibration_decisions_one_hot": all(
            bool(path["all_calibration_decisions_one_hot"])
            for path in path_rows
        ),
        "all_polarity_checks_passed": all(
            bool(path["positive_polarity"] and path["negative_polarity"])
            for path in path_rows
        ),
        "all_hold_nodes_rail_legal": all(
            bool(path["hold_rail_legal"]) for path in path_rows
        ),
        "all_retention_measurements_complete": all(
            bool(path["retention"]["complete"]) for path in path_rows
        ),
        "all_retention_hold_nodes_rail_legal": all(
            bool(path["retention_hold_rail_legal"]) for path in path_rows
        ),
        "retention_rail_saturation_observed": any(
            bool(path["retention_hold_rail_saturated"])
            for path in path_rows
        ),
        "rail_saturation_observed": any(
            bool(path["hold_rail_saturated"]) for path in path_rows
        ),
        "contexts": contexts,
        "qualification_thresholds_applied": False,
        "product_qualification_conclusion": False,
    }


def reconstruct_native_diagnostic(
    result: dict[str, Any],
    contract: Any,
    legacy_result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Reconstruct reviewed diagnostics using only native RDB scalars."""

    diagnostic = contract.diagnostic_equivalence
    if diagnostic is None:
        return None
    if diagnostic.kind == "frontend_valid_edges":
        return _native_frontend_diagnostics(
            result, contract, diagnostic, legacy_result
        )
    if diagnostic.kind == "bank_calibration_trajectory":
        return _native_bank_calibration_trajectory(
            result, contract, diagnostic
        )
    if diagnostic.kind == "bank_calibration_measurement":
        return _native_bank_calibration_measurement(
            result, contract, diagnostic
        )
    if legacy_result is None:
        raise ValueError("native diagnostic reconstruction requires legacy RDB result")
    if diagnostic.kind == "dac_window_statistics":
        return _native_dac_diagnostics(result, contract, diagnostic, legacy_result)
    if diagnostic.kind == "controller_sequence":
        return _native_controller_diagnostics(result, contract, diagnostic, legacy_result)
    if diagnostic.kind == "mx_valid_edges":
        return _native_mx_diagnostics(result, contract, diagnostic, legacy_result)
    raise ValueError(f"unsupported native diagnostic kind: {diagnostic.kind}")
