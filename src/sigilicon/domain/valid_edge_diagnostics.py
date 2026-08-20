"""Generic valid-edge scalar generation and RDB reconstruction.

Callers own the source schema, scenario meaning, code mapping, and report policy.
This module only handles validated edge windows and decodes scalar/legacy values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Any, Mapping

from sigilicon.domain.oa_simulation import OANativeDiagnosticContract


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
    window_starts: tuple[str, ...]
    window_ends: tuple[str, ...]
    edge_signal: str
    edge_count: int
    threshold_v: float
    owner_metadata: Mapping[str, Any] = field(default_factory=dict)
    support_sources: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ValidEdgeResultBinding:
    """Names that bind a generic decoder to caller-declared legacy exports."""

    protocol_export: str
    decision_export: str
    analog_export: str
    analog_output_keys: tuple[str, ...]
    ready_signal_index: int = 0


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
        for values in (definition.window_starts, definition.window_ends)
    ):
        raise ValueError("valid-edge windows must match the scenario count")
    if not definition.edge_signal.startswith("/"):
        raise ValueError("valid-edge signal must be an absolute OA net name")
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
) -> OANativeDiagnosticContract:
    """Create native Calculator expressions from caller-validated inputs."""

    _validate_definition(definition)
    outputs: list[tuple[str, str]] = []
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
        "window_starts": definition.window_starts,
        "window_ends": definition.window_ends,
        "edge_signal": definition.edge_signal,
        "edge_count": definition.edge_count,
        "threshold_v": float(definition.threshold_v),
        "owner_metadata": dict(definition.owner_metadata),
    }
    return OANativeDiagnosticContract(
        kind=definition.kind,
        settings=settings,
        scalar_outputs=tuple(outputs),
        support_sources=definition.support_sources,
    )


def validate_valid_edge_contract(
    diagnostic: OANativeDiagnosticContract,
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
        window_starts=tuple(settings.get("window_starts", ())),
        window_ends=tuple(settings.get("window_ends", ())),
        edge_signal=str(settings.get("edge_signal", "")),
        edge_count=settings.get("edge_count", 0),
        threshold_v=settings.get("threshold_v", 0.0),
    )
    _validate_definition(definition)


def validate_valid_edge_source(
    diagnostic: OANativeDiagnosticContract,
    setup_text: str,
) -> tuple[str, ...]:
    """Report missing generator markers without interpreting caller policy."""

    missing: list[str] = []
    markers = (
        "diagnosticEdgeWindows",
        '"diag_valid_edge_%03d_%02d"',
        'cross(clip(VT(\\"%s\\") %s %s)',
    )
    for marker in markers:
        if marker not in setup_text:
            missing.append(f"diagnostic setup generator {marker}")
    for field in ("window_starts", "window_ends"):
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


def _legacy_values(
    context: Mapping[str, Any], series: Any, sample_index: int
) -> tuple[Any, ...]:
    values = context["exports"].get(series.export)
    if not isinstance(values, list):
        raise ValueError(f"native legacy export is missing: {series.export}")
    start = sample_index * len(series.signals)
    end = start + len(series.signals)
    if end > len(values):
        raise ValueError(f"native legacy export is short: {series.export}")
    return tuple(values[start:end])


def _logic(value: Any, threshold: float) -> int:
    return 0 if float(value) < threshold else 1


def evaluate_valid_edge_diagnostic(
    result: Mapping[str, Any],
    contract: Any,
    legacy_result: Mapping[str, Any],
    *,
    binding: ValidEdgeResultBinding,
) -> dict[str, Any]:
    """Decode generic valid-edge observations; callers assemble final reports."""

    diagnostic = contract.diagnostic_equivalence
    if diagnostic is None:
        raise ValueError("valid-edge diagnostic contract is missing")
    legacy = contract.legacy_measurement
    if legacy is None:
        raise ValueError("valid-edge diagnostics require legacy sampled arrays")
    series_by_export = {series.export: series for series in legacy.series}
    required_exports = (
        binding.protocol_export,
        binding.decision_export,
        binding.analog_export,
    )
    missing_exports = [name for name in required_exports if name not in series_by_export]
    if missing_exports:
        raise ValueError(
            "valid-edge legacy exports are missing: " + ", ".join(missing_exports)
        )
    protocol = series_by_export[binding.protocol_export]
    decisions = series_by_export[binding.decision_export]
    analog = series_by_export[binding.analog_export]
    if binding.ready_signal_index >= len(protocol.signals):
        raise ValueError("valid-edge ready signal index is outside protocol series")
    if len(binding.analog_output_keys) != len(analog.signals):
        raise ValueError("valid-edge analog output keys do not match analog signals")

    settings = dict(diagnostic.settings)
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
                try:
                    legacy_context = next(
                        item
                        for item in legacy_result["contexts"]
                        if item["point"] == point
                        and item["corner"] == corner
                        and item["test"] == test
                    )
                except StopIteration as exc:
                    raise ValueError(
                        f"native legacy RDB is missing context {context_name}"
                    ) from exc
                rows: list[dict[str, Any]] = []
                checks: list[dict[str, Any]] = []
                for index, scenario in enumerate(scenarios):
                    protocol_values = _legacy_values(legacy_context, protocol, index)
                    decision_values = _legacy_values(legacy_context, decisions, index)
                    analog_values = _legacy_values(legacy_context, analog, index)
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
                        protocol_values[binding.ready_signal_index], threshold
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
