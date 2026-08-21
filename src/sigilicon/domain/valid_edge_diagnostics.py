"""Generic valid-edge scalar generation and RDB reconstruction.

Callers own the source schema, scenario meaning, code mapping, and report policy.
This module only handles validated edge windows and decodes Calculator scalars.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Any, Mapping

from sigilicon.domain.native_diagnostics import NativeDiagnosticContract


@dataclass(frozen=True)
class ValidEdgeScenario:
    """One caller-defined observation and its already-resolved expected code."""

    name: str
    expected_code: int
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidEdgeContractDefinition:
    """Backend-neutral inputs needed to create Calculator edge expressions."""

    kind: str
    scenarios: tuple[ValidEdgeScenario, ...]
    sample_times: tuple[str, ...]
    ready_signal: str
    decision_signals: tuple[str, ...]
    analog_signals: tuple[str, ...]
    window_starts: tuple[str, ...]
    window_ends: tuple[str, ...]
    edge_signal: str
    edge_count: int
    threshold_v: float
    owner_metadata: Mapping[str, Any] = field(default_factory=dict)
    support_sources: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ValidEdgeResultBinding:
    """Caller-owned names for analog values in the reconstructed report."""

    analog_output_keys: tuple[str, ...]


def _calculator_number(value: float) -> str:
    return format(value, ".12g")


def _validate_definition(definition: ValidEdgeContractDefinition) -> None:
    if not definition.kind:
        raise ValueError("valid-edge diagnostic kind must not be empty")
    if not definition.scenarios:
        raise ValueError("valid-edge diagnostic scenarios must not be empty")
    names = tuple(scenario.name for scenario in definition.scenarios)
    if any(not name for name in names) or len(set(names)) != len(names):
        raise ValueError("valid-edge scenario names must be non-empty and unique")
    if not all(
        len(values) == len(definition.scenarios)
        for values in (
            definition.sample_times,
            definition.window_starts,
            definition.window_ends,
        )
    ):
        raise ValueError("valid-edge samples and windows must match the scenario count")
    signals = (
        definition.ready_signal,
        *definition.decision_signals,
        *definition.analog_signals,
        definition.edge_signal,
    )
    if not definition.decision_signals or not definition.analog_signals:
        raise ValueError("valid-edge decision and analog signals must not be empty")
    if any(not signal.startswith("/") for signal in signals):
        raise ValueError("valid-edge signals must be absolute OA net names")
    if isinstance(definition.edge_count, bool) or definition.edge_count <= 0:
        raise ValueError("valid-edge count must be a positive integer")
    if (
        isinstance(definition.threshold_v, bool)
        or not math.isfinite(float(definition.threshold_v))
        or definition.threshold_v <= 0
    ):
        raise ValueError("valid-edge threshold must be a positive finite number")


def build_valid_edge_contract(
    definition: ValidEdgeContractDefinition,
) -> NativeDiagnosticContract:
    """Create native Calculator expressions from caller-validated inputs."""

    _validate_definition(definition)
    outputs: list[tuple[str, str]] = []
    for index, sample_time in enumerate(definition.sample_times):
        outputs.append(
            (
                f"diag_ready_{index:03d}_00",
                f'value(VT("{definition.ready_signal}") {sample_time})',
            )
        )
        for signal_index, signal in enumerate(definition.decision_signals):
            outputs.append(
                (
                    f"diag_decision_{index:03d}_{signal_index:02d}",
                    f'value(VT("{signal}") {sample_time})',
                )
            )
        for signal_index, signal in enumerate(definition.analog_signals):
            outputs.append(
                (
                    f"diag_analog_{index:03d}_{signal_index:02d}",
                    f'value(VT("{signal}") {sample_time})',
                )
            )
    for index, (start, end) in enumerate(
        zip(definition.window_starts, definition.window_ends, strict=True)
    ):
        for edge in range(1, definition.edge_count + 1):
            expression = (
                f'cross(clip(VT("{definition.edge_signal}") {start} {end}) '
                f'{_calculator_number(definition.threshold_v)} {edge} '
                '"rising" nil "time")'
            )
            outputs.append(
                (f"diag_valid_edge_{index:03d}_{edge - 1:02d}", expression)
            )
    settings = {
        "kind": definition.kind,
        "scenarios": tuple(
            {
                "name": scenario.name,
                "expected_code": scenario.expected_code,
                "metadata": dict(scenario.metadata),
            }
            for scenario in definition.scenarios
        ),
        "sample_times": definition.sample_times,
        "ready_signal": definition.ready_signal,
        "decision_signals": definition.decision_signals,
        "analog_signals": definition.analog_signals,
        "window_starts": definition.window_starts,
        "window_ends": definition.window_ends,
        "edge_signal": definition.edge_signal,
        "edge_count": definition.edge_count,
        "threshold_v": float(definition.threshold_v),
        "owner_metadata": dict(definition.owner_metadata),
    }
    return NativeDiagnosticContract(
        kind=definition.kind,
        settings=settings,
        scalar_outputs=tuple(outputs),
        support_sources=definition.support_sources,
    )


def validate_valid_edge_contract(
    diagnostic: NativeDiagnosticContract,
    *,
    point_count: int,
    expected_kind: str,
) -> None:
    """Validate invariants needed by the shared RDB algorithm."""

    if diagnostic.kind != expected_kind:
        raise ValueError(f"unsupported valid-edge diagnostic kind: {diagnostic.kind}")
    if isinstance(point_count, bool) or point_count <= 0:
        raise ValueError("valid-edge point_count must be a positive integer")
    settings = dict(diagnostic.settings)
    scenarios = tuple(settings.get("scenarios", ()))
    definition = ValidEdgeContractDefinition(
        kind=diagnostic.kind,
        scenarios=tuple(
            ValidEdgeScenario(
                name=str(scenario["name"]),
                expected_code=int(scenario["expected_code"]),
                metadata=dict(scenario.get("metadata", {})),
            )
            for scenario in scenarios
        ),
        sample_times=tuple(settings.get("sample_times", ())),
        ready_signal=str(settings.get("ready_signal", "")),
        decision_signals=tuple(settings.get("decision_signals", ())),
        analog_signals=tuple(settings.get("analog_signals", ())),
        window_starts=tuple(settings.get("window_starts", ())),
        window_ends=tuple(settings.get("window_ends", ())),
        edge_signal=str(settings.get("edge_signal", "")),
        edge_count=settings.get("edge_count", 0),
        threshold_v=settings.get("threshold_v", 0.0),
    )
    _validate_definition(definition)


def validate_valid_edge_source(
    diagnostic: NativeDiagnosticContract,
    setup_text: str,
) -> tuple[str, ...]:
    """Report missing generator markers without interpreting caller policy."""

    missing: list[str] = []
    markers = (
        "diagnosticSampleTimes",
        "diagnosticSampleGroups",
        '"diag_%s_%03d_%02d"',
        r'value(VT(\"%s\") %s)',
        "diagnosticEdgeWindows",
        '"diag_valid_edge_%03d_%02d"',
        'cross(clip(VT(\\"%s\\") %s %s)',
    )
    for marker in markers:
        if marker not in setup_text:
            missing.append(f"diagnostic setup generator {marker}")
    for field in ("sample_times", "window_starts", "window_ends"):
        for value in diagnostic.settings.get(field, ()):
            if isinstance(value, str) and f'"{value}"' not in setup_text:
                missing.append(f"diagnostic {field} value {value}")
    return tuple(missing)


def _context_rows(
    result: Mapping[str, Any], *, point: int, corner: str, test: str
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


def _diagnostic_value(rows: Mapping[str, Any], name: str, *, context: str) -> float:
    if name not in rows:
        raise ValueError(f"native RDB is missing diagnostic output {context}/{name}")
    value = rows[name]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"native diagnostic output {context}/{name} is not finite")
    return float(value)


def _logic(value: Any, threshold: float) -> int:
    return 0 if float(value) < threshold else 1


def evaluate_valid_edge_diagnostic(
    result: Mapping[str, Any],
    contract: Any,
    *,
    binding: ValidEdgeResultBinding,
) -> dict[str, Any]:
    """Decode generic valid-edge observations; callers assemble final reports."""

    diagnostic = contract.diagnostic_equivalence
    if diagnostic is None:
        raise ValueError("valid-edge diagnostic contract is missing")
    settings = dict(diagnostic.settings)
    analog_signals = tuple(settings["analog_signals"])
    decision_signals = tuple(settings["decision_signals"])
    if len(binding.analog_output_keys) != len(analog_signals):
        raise ValueError("valid-edge analog output keys do not match analog signals")

    scenarios = tuple(settings["scenarios"])
    threshold = float(settings["threshold_v"])
    edge_count = int(settings["edge_count"])
    contexts: list[dict[str, Any]] = []
    for point in range(1, contract.point_count + 1):
        for corner in contract.corners:
            for test in contract.tests:
                context_name = f"{point}/{corner}/{test}"
                rows_by_name = _context_rows(
                    result, point=point, corner=corner, test=test
                )
                rows: list[dict[str, Any]] = []
                checks: list[dict[str, Any]] = []
                for index, scenario in enumerate(scenarios):
                    decision_values = tuple(
                        _diagnostic_value(
                            rows_by_name,
                            f"diag_decision_{index:03d}_{signal_index:02d}",
                            context=context_name,
                        )
                        for signal_index in range(len(decision_signals))
                    )
                    analog_values = tuple(
                        _diagnostic_value(
                            rows_by_name,
                            f"diag_analog_{index:03d}_{signal_index:02d}",
                            context=context_name,
                        )
                        for signal_index in range(len(analog_signals))
                    )
                    code_bits = [_logic(value, threshold) for value in decision_values]
                    code = sum(
                        value << bit
                        for value, bit in zip(
                            code_bits,
                            range(len(code_bits) - 1, -1, -1),
                            strict=True,
                        )
                    )
                    ready = _logic(
                        _diagnostic_value(
                            rows_by_name,
                            f"diag_ready_{index:03d}_00",
                            context=context_name,
                        ),
                        threshold,
                    )
                    edges = [
                        _diagnostic_value(
                            rows_by_name,
                            f"diag_valid_edge_{index:03d}_{edge:02d}",
                            context=context_name,
                        )
                        for edge in range(edge_count)
                    ]
                    name = str(scenario["name"])
                    expected = int(scenario["expected_code"])
                    checks.extend(
                        (
                            {
                                "name": f"{name}_code",
                                "passed": code == expected and ready == 1,
                                "value": code,
                                "expected": expected,
                            },
                            {
                                "name": f"{name}_valid_edges",
                                "passed": len(edges) == edge_count,
                                "value": len(edges),
                                "expected": edge_count,
                            },
                        )
                    )
                    row = dict(scenario.get("metadata", {}))
                    row.update(
                        {
                            "name": name,
                            "expected_code": expected,
                            "code": code,
                            "code_bits_msb_first": code_bits,
                            "rdy": ready,
                            "valid_rising_edges_s": edges,
                        }
                    )
                    row.update(zip(binding.analog_output_keys, analog_values, strict=True))
                    rows.append(row)
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
    return {"threshold_v": threshold, "contexts": contexts}
