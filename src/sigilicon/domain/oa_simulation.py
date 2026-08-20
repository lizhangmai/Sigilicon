"""Declarative OA config/Maestro source shared by manual and automated runs."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
import tomllib
from typing import Any, Mapping

from sigilicon.domain.code_mapping import IntegerCodeMapping, load_ip_adc_code_mapping
from sigilicon.domain.design import PdkConfig, load_pdk_config
from sigilicon.domain.fingerprints import (
    SourceFingerprintSet,
    oa_source_fingerprints,
)
from sigilicon.paths import ProjectContext


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_TIME_TOKEN = re.compile(
    r"(?P<value>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+\-]?\d+)?)"
    r"(?P<prefix>[afpnum]?)(?:s)?\Z"
)
_TIME_SCALE = {
    "": 1.0,
    "a": 1.0e-18,
    "f": 1.0e-15,
    "p": 1.0e-12,
    "n": 1.0e-9,
    "u": 1.0e-6,
    "m": 1.0e-3,
}


@dataclass(frozen=True)
class OANativeLegacySeries:
    """One legacy MDL array represented by native Calculator samples."""

    export: str
    signals: tuple[str, ...]
    prefix: str

    def output_name(self, sample_index: int, signal_index: int) -> str:
        return f"{self.prefix}_{sample_index:03d}_{signal_index:02d}"

    def expression(self, signal: str, sample_time: str) -> str:
        return f'value(VT("{signal}") {sample_time})'


@dataclass(frozen=True)
class OANativeLegacyMeasurement:
    """Source-owned shape of the former MDL sampled-array result."""

    alias: str
    sample_times: tuple[str, ...]
    series: tuple[OANativeLegacySeries, ...]

    @property
    def scalar_outputs(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (
                series.output_name(index, signal_index),
                series.expression(signal, sample_time),
            )
            for series in self.series
            for index, sample_time in enumerate(self.sample_times)
            for signal_index, signal in enumerate(series.signals)
        )

    @property
    def sample_count(self) -> int:
        return len(self.sample_times)

    @property
    def scalar_count(self) -> int:
        return self.sample_count * sum(len(series.signals) for series in self.series)


@dataclass(frozen=True)
class OANativeDiagnosticContract:
    """Source-owned native Calculator outputs for reviewed observations.

    ``settings`` is normalized to tuples by the loader.  It is deliberately
    data, rather than executable Python measurement code: the native setup
    creates the Calculator expressions and Cadence's RDB supplies their
    values.  Python only reconstructs the reviewed structured result.
    """

    kind: str
    settings: Mapping[str, Any]
    scalar_outputs: tuple[tuple[str, str], ...]
    support_sources: tuple[Path, ...] = ()


@dataclass(frozen=True)
class OANativeRdbContract:
    """Independent identity model used to audit a native Maestro RDB."""

    path: Path
    point_count: int
    corners: tuple[str, ...]
    tests: tuple[str, ...]
    waveform_outputs: tuple[tuple[str, str], ...]
    scalar_outputs: tuple[tuple[str, str], ...]
    setup_model_identities: tuple[tuple[str, str], ...] = ()
    legacy_measurement: OANativeLegacyMeasurement | None = None
    diagnostic_equivalence: OANativeDiagnosticContract | None = None

    @property
    def scalar_names(self) -> tuple[str, ...]:
        return tuple(name for name, _expression in self.scalar_outputs)

    @property
    def legacy_scalar_names(self) -> tuple[str, ...]:
        if self.legacy_measurement is None:
            return ()
        return tuple(
            name for name, _expression in self.legacy_measurement.scalar_outputs
        )

    @property
    def diagnostic_scalar_names(self) -> tuple[str, ...]:
        if self.diagnostic_equivalence is None:
            return ()
        return tuple(
            name for name, _expression in self.diagnostic_equivalence.scalar_outputs
        )

    @property
    def nullable_scalar_names(self) -> tuple[str, ...]:
        """Calculator outputs where ``nil`` is a reviewed diagnostic result."""

        if (
            self.diagnostic_equivalence is None
            or self.diagnostic_equivalence.kind != "bank_calibration_measurement"
        ):
            return ()
        return tuple(
            name
            for name in self.diagnostic_scalar_names
            if "_boundary_" in name
        )

    @property
    def support_sources(self) -> tuple[Path, ...]:
        if self.diagnostic_equivalence is None:
            return ()
        return self.diagnostic_equivalence.support_sources

    @property
    def expected_expression_count(self) -> int:
        return (
            self.point_count
            * len(self.corners)
            * len(self.tests)
            * len(self.scalar_outputs)
        )


@dataclass(frozen=True)
class OANativeSetup:
    """Source-owned native ADE/Maestro setup materialized through SKILL."""

    pdk: PdkConfig
    source: Path
    rdb_contract: OANativeRdbContract | None = None


@dataclass(frozen=True)
class OASimulationSpec:
    """The native-only schema-3 simulation identity consumed by OA workflows."""

    path: Path
    project_root: Path
    library: str
    cell: str
    dut: str
    top_view: str
    simulator: str
    native_setup: OANativeSetup
    contract_schema: int = 3


def oa_simulation_fingerprint(spec: OASimulationSpec, canonical_source: Path) -> str:
    """Return the byte-exact identity of the complete native OA source set."""

    return oa_source_fingerprints(spec, canonical_source).exact


def oa_simulation_fingerprints(
    spec: OASimulationSpec,
    canonical_source: Path,
) -> SourceFingerprintSet:
    """Return exact and semantic identities for native OA materialization."""

    return oa_source_fingerprints(spec, canonical_source)


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} must be an identifier")
    return value


def _table(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a table")
    return value


def _rows(value: object, field: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or not value or not all(
        isinstance(row, Mapping) for row in value
    ):
        raise ValueError(f"{field} must be a non-empty array of tables")
    return tuple(value)


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{field} must be a non-empty string array")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise ValueError(f"{field} contains duplicates")
    return result


def _positive_time_token(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a positive time token")
    match = _TIME_TOKEN.fullmatch(value)
    if match is None:
        raise ValueError(f"{field} must be a positive time token")
    seconds = float(match.group("value")) * _TIME_SCALE[match.group("prefix")]
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{field} must be a positive time token")
    return value


def _finite_number(value: object, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        qualifier = "positive " if positive else "finite "
        raise ValueError(f"{field} must be a {qualifier}number")
    return number


def _integer_list(value: object, field: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty integer array")
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError(f"{field} must contain only integers")
        result.append(item)
    return tuple(result)


def _time_list(value: object, field: str) -> tuple[str, ...]:
    return tuple(
        _positive_time_token(item, field)
        for item in _strings(value, field)
    )


def _format_calculator_number(value: float) -> str:
    return format(value, ".12g")


def _freeze_settings(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _freeze_settings(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return tuple(_freeze_settings(item) for item in value)
    return value


def _legacy_series_lookup(
    legacy: OANativeLegacyMeasurement | None,
    exports: object,
    field: str,
) -> tuple[OANativeLegacySeries, ...]:
    if legacy is None:
        raise ValueError(
            f"{field} requires legacy_equivalence sampled arrays"
        )
    names = _strings(exports, field)
    by_export = {series.export: series for series in legacy.series}
    missing = [name for name in names if name not in by_export]
    if missing:
        raise ValueError(
            f"{field} refers to unknown legacy series: {', '.join(missing)}"
        )
    return tuple(by_export[name] for name in names)


def _load_native_diagnostic_contract(
    raw: object,
    legacy: OANativeLegacyMeasurement | None,
    *,
    contract_path: Path,
    project_root: Path,
) -> OANativeDiagnosticContract:
    table = _table(raw, "native RDB contract diagnostic_equivalence")
    kind = _identifier(
        table.get("kind"),
        "native RDB contract diagnostic_equivalence.kind",
    )
    outputs: list[tuple[str, str]] = []
    support_sources: tuple[Path, ...] = ()

    def load_code_mapping() -> tuple[IntegerCodeMapping, Path]:
        value = table.get("code_mapping_contract")
        if not isinstance(value, str) or not value or Path(value).is_absolute():
            raise ValueError(
                "native diagnostic code_mapping_contract must be a relative path"
            )
        source = (contract_path.parent / value).resolve()
        if not source.is_relative_to(project_root) or not source.is_file():
            raise ValueError(
                "native diagnostic code_mapping_contract is missing or escapes "
                "the project"
            )
        return load_ip_adc_code_mapping(source), source

    if kind == "dac_window_statistics":
        required = {
            "kind", "series", "window_starts", "window_ends",
            "warmup_codes", "transfer_codes", "repeat_codes",
            "vrefh_v", "vrefl_v", "vcm_v", "denominator_units",
            "physical_levels",
        }
        if set(table) != required:
            raise ValueError(
                "DAC diagnostic_equivalence fields must be exactly "
                + ", ".join(sorted(required))
            )
        selected = _legacy_series_lookup(
            legacy, table.get("series"),
            "native DAC diagnostic_equivalence.series",
        )
        starts = _time_list(
            table.get("window_starts"),
            "native DAC diagnostic_equivalence.window_starts",
        )
        ends = _time_list(
            table.get("window_ends"),
            "native DAC diagnostic_equivalence.window_ends",
        )
        if len(starts) != len(ends) or legacy is None or len(starts) != legacy.sample_count:
            raise ValueError(
                "DAC diagnostic window count must match legacy sample count"
            )
        warmup = _integer_list(
            table.get("warmup_codes"),
            "native DAC diagnostic_equivalence.warmup_codes",
        )
        transfer = _integer_list(
            table.get("transfer_codes"),
            "native DAC diagnostic_equivalence.transfer_codes",
        )
        repeat = _integer_list(
            table.get("repeat_codes"),
            "native DAC diagnostic_equivalence.repeat_codes",
        )
        if len((*warmup, *transfer, *repeat)) != len(starts):
            raise ValueError(
                "DAC diagnostic code schedule must match legacy sample count"
            )
        vrefh = _finite_number(table.get("vrefh_v"), "DAC vrefh_v")
        vrefl = _finite_number(table.get("vrefl_v"), "DAC vrefl_v")
        vcm = _finite_number(table.get("vcm_v"), "DAC vcm_v", positive=True)
        denominator = _finite_number(
            table.get("denominator_units"),
            "DAC denominator_units",
            positive=True,
        )
        physical_levels = table.get("physical_levels")
        if (
            isinstance(physical_levels, bool)
            or not isinstance(physical_levels, int)
            or physical_levels <= 0
        ):
            raise ValueError("DAC physical_levels must be a positive integer")
        for index, (start, end) in enumerate(zip(starts, ends, strict=True)):
            for series in selected:
                for signal_index, signal in enumerate(series.signals):
                    base = series.output_name(index, signal_index)
                    window = f'clip(VT("{signal}") {start} {end})'
                    outputs.extend(
                        (
                            (f"diag_mean_{base}", f"average({window})"),
                            (
                                f"diag_pp_{base}",
                                f"ymax({window})-ymin({window})",
                            ),
                        )
                    )
        settings = {
            "kind": kind,
            "series": tuple(series.export for series in selected),
            "window_starts": starts,
            "window_ends": ends,
            "warmup_codes": warmup,
            "transfer_codes": transfer,
            "repeat_codes": repeat,
            "vrefh_v": vrefh,
            "vrefl_v": vrefl,
            "vcm_v": vcm,
            "denominator_units": denominator,
            "physical_levels": physical_levels,
        }
    elif kind == "controller_sequence":
        required = {
            "kind", "patterns", "fall_signals", "fall_window_starts",
            "fall_window_ends", "threshold_v",
        }
        if set(table) != required:
            raise ValueError(
                "controller diagnostic_equivalence fields must be exactly "
                + ", ".join(sorted(required))
            )
        patterns = _integer_list(
            table.get("patterns"),
            "controller diagnostic_equivalence.patterns",
        )
        signals = tuple(
            signal
            for signal in _strings(
                table.get("fall_signals"),
                "controller diagnostic_equivalence.fall_signals",
            )
            if signal.startswith("/")
        )
        if len(signals) != len(table.get("fall_signals", ())):
            raise ValueError("controller fall_signals must be absolute OA net names")
        starts = _time_list(
            table.get("fall_window_starts"),
            "controller diagnostic_equivalence.fall_window_starts",
        )
        ends = _time_list(
            table.get("fall_window_ends"),
            "controller diagnostic_equivalence.fall_window_ends",
        )
        if len(starts) != len(ends) or len(starts) != len(patterns):
            raise ValueError("controller fall window count must match patterns")
        threshold = _finite_number(
            table.get("threshold_v"),
            "controller diagnostic_equivalence.threshold_v",
            positive=True,
        )
        for index in range(len(patterns)):
            for signal in signals:
                label = signal.strip("/").lower()
                expression = (
                    f'cross(clip(VT("{signal}") {starts[index]} '
                    f'{ends[index]}) {_format_calculator_number(threshold)} '
                    '1 "falling" nil "time")'
                )
                outputs.append(
                    (f"diag_fall_{label}_{index:03d}", expression)
                )
        settings = {
            "kind": kind,
            "patterns": patterns,
            "fall_signals": signals,
            "fall_window_starts": starts,
            "fall_window_ends": ends,
            "threshold_v": threshold,
        }
    elif kind == "mx_valid_edges":
        required = {
            "kind", "scenario_names", "active_buffers", "expected_scores",
            "window_starts", "window_ends", "edge_signal", "edge_count",
            "threshold_v", "bounded_campaign", "code_mapping_contract",
        }
        optional = {"overlap_write_b_code_index", "overlap_write_b_code", "activation_masks_hex"}
        if set(table) - required - optional or not required.issubset(table):
            raise ValueError(
                "MX diagnostic_equivalence lacks its required fields"
            )
        names = _strings(
            table.get("scenario_names"),
            "MX diagnostic_equivalence.scenario_names",
        )
        active = _integer_list(
            table.get("active_buffers"),
            "MX diagnostic_equivalence.active_buffers",
        )
        expected_scores = _integer_list(
            table.get("expected_scores"),
            "MX diagnostic_equivalence.expected_scores",
        )
        starts = _time_list(
            table.get("window_starts"),
            "MX diagnostic_equivalence.window_starts",
        )
        ends = _time_list(
            table.get("window_ends"),
            "MX diagnostic_equivalence.window_ends",
        )
        if not names or not active or not expected_scores or not starts or not ends:
            raise ValueError("MX diagnostic scenario arrays must not be empty")
        if not all(
            len(item) == len(names)
            for item in (active, expected_scores, starts, ends)
        ):
            raise ValueError("MX diagnostic scenario arrays must have equal length")
        edge_signal = _strings(
            [table.get("edge_signal")],
            "MX diagnostic_equivalence.edge_signal",
        )[0]
        if not edge_signal.startswith("/"):
            raise ValueError("MX edge_signal must be an absolute OA net name")
        edge_count = table.get("edge_count")
        if isinstance(edge_count, bool) or not isinstance(edge_count, int) or edge_count <= 0:
            raise ValueError("MX edge_count must be a positive integer")
        threshold = _finite_number(
            table.get("threshold_v"),
            "MX diagnostic_equivalence.threshold_v",
            positive=True,
        )
        bounded = table.get("bounded_campaign")
        if not isinstance(bounded, bool):
            raise ValueError("MX bounded_campaign must be boolean")
        code_mapping, code_mapping_source = load_code_mapping()
        support_sources = (code_mapping_source,)
        for index, (start, end) in enumerate(zip(starts, ends, strict=True)):
            for edge in range(1, edge_count + 1):
                expression = (
                    f'cross(clip(VT("{edge_signal}") {start} {end}) '
                    f'{_format_calculator_number(threshold)} {edge} '
                    '"rising" nil "time")'
                )
                outputs.append(
                    (f"diag_valid_edge_{index:03d}_{edge - 1:02d}", expression)
                )
        settings = {
            "kind": kind,
            "scenario_names": names,
            "active_buffers": active,
            "expected_scores": expected_scores,
            "expected_codes": tuple(
                code_mapping.code_for(score) for score in expected_scores
            ),
            "window_starts": starts,
            "window_ends": ends,
            "edge_signal": edge_signal,
            "edge_count": edge_count,
            "threshold_v": threshold,
            "bounded_campaign": bounded,
            "code_mapping": code_mapping.as_dict(),
            "code_mapping_contract": str(
                code_mapping_source.relative_to(project_root)
            ),
            "overlap_write_b_code_index": table.get("overlap_write_b_code_index"),
            "overlap_write_b_code": table.get("overlap_write_b_code"),
            "activation_masks_hex": tuple(table.get("activation_masks_hex", ())),
        }
    elif kind in {"bank_calibration_trajectory", "bank_calibration_measurement"}:
        measurement_fields = {
            "monte_carlo_samples", "supply_v",
            "model_file", "model_sections",
            "transient_stop", "transient_maxstep",
            "raw_half_span_v", "post_half_span_v", "retention_half_span_v",
            "raw_sweep_min_v", "raw_sweep_max_v", "raw_sweep_step_v",
            "raw_ascending_start", "raw_ascending_stop",
            "raw_descending_start", "raw_descending_stop",
            "raw_sample_period",
            "post_sweep_min_v", "post_sweep_max_v", "post_sweep_step_v",
            "post_ascending_start", "post_ascending_stop",
            "post_descending_start", "post_descending_stop",
            "post_sample_period",
            "retention_hold_time_s", "retention_hold_start",
            "retention_hold_sample",
            "retention_sweep_min_v", "retention_sweep_max_v",
            "retention_sweep_step_v",
            "retention_ascending_start", "retention_ascending_stop",
            "retention_descending_start", "retention_descending_stop",
            "retention_sample_period",
        }
        required = {
            "kind", "paths", "decision_sample_times",
            "transfer_sample_times", "threshold_v",
        }
        if kind == "bank_calibration_measurement":
            required |= measurement_fields
        if set(table) != required:
            raise ValueError(
                "Bank calibration diagnostic_equivalence fields must be exactly "
                + ", ".join(sorted(required))
            )
        paths = table.get("paths")
        if isinstance(paths, bool) or not isinstance(paths, int) or paths <= 0:
            raise ValueError("Bank calibration paths must be a positive integer")
        decision_times = _time_list(
            table.get("decision_sample_times"),
            "Bank calibration diagnostic_equivalence.decision_sample_times",
        )
        transfer_times = _time_list(
            table.get("transfer_sample_times"),
            "Bank calibration diagnostic_equivalence.transfer_sample_times",
        )
        if len(decision_times) != 7 or len(transfer_times) != 7:
            raise ValueError(
                "Bank calibration trajectory requires seven decisions and seven transfers"
            )
        threshold = _finite_number(
            table.get("threshold_v"),
            "Bank calibration diagnostic_equivalence.threshold_v",
            positive=True,
        )
        threshold_token = _format_calculator_number(threshold)
        settings = {
            "kind": kind,
            "paths": paths,
            "decision_sample_times": decision_times,
            "transfer_sample_times": transfer_times,
            "threshold_v": threshold,
        }
        if kind == "bank_calibration_measurement":
            monte_carlo_samples = table.get("monte_carlo_samples")
            if (
                isinstance(monte_carlo_samples, bool)
                or not isinstance(monte_carlo_samples, int)
                or monte_carlo_samples <= 0
            ):
                raise ValueError(
                    "Bank calibration monte_carlo_samples must be a positive integer"
                )
            model_file = table.get("model_file")
            if (
                not isinstance(model_file, str)
                or not model_file
                or Path(model_file).name != model_file
            ):
                raise ValueError(
                    "Bank calibration model_file must be a model basename"
                )
            model_sections = _strings(
                table.get("model_sections"),
                "Bank calibration diagnostic_equivalence.model_sections",
            )
            transient_stop = _positive_time_token(
                table.get("transient_stop"),
                "Bank calibration diagnostic_equivalence.transient_stop",
            )
            transient_maxstep = _positive_time_token(
                table.get("transient_maxstep"),
                "Bank calibration diagnostic_equivalence.transient_maxstep",
            )
            def sweep_settings(
                phase: str,
            ) -> tuple[float, float, float, str, str, str, str, str]:
                minimum = _finite_number(
                    table.get(f"{phase}_sweep_min_v"),
                    f"Bank calibration {phase}_sweep_min_v",
                )
                maximum = _finite_number(
                    table.get(f"{phase}_sweep_max_v"),
                    f"Bank calibration {phase}_sweep_max_v",
                )
                step = _finite_number(
                    table.get(f"{phase}_sweep_step_v"),
                    f"Bank calibration {phase}_sweep_step_v",
                    positive=True,
                )
                ascending_start = _positive_time_token(
                    table.get(f"{phase}_ascending_start"),
                    f"Bank calibration {phase}_ascending_start",
                )
                ascending_stop = _positive_time_token(
                    table.get(f"{phase}_ascending_stop"),
                    f"Bank calibration {phase}_ascending_stop",
                )
                descending_start = _positive_time_token(
                    table.get(f"{phase}_descending_start"),
                    f"Bank calibration {phase}_descending_start",
                )
                descending_stop = _positive_time_token(
                    table.get(f"{phase}_descending_stop"),
                    f"Bank calibration {phase}_descending_stop",
                )
                sample_period = _positive_time_token(
                    table.get(f"{phase}_sample_period"),
                    f"Bank calibration {phase}_sample_period",
                )
                intervals = (maximum - minimum) / step
                rounded_intervals = round(intervals)
                time_seconds = {}
                for name, token in (
                    ("ascending_start", ascending_start),
                    ("ascending_stop", ascending_stop),
                    ("descending_start", descending_start),
                    ("descending_stop", descending_stop),
                    ("sample_period", sample_period),
                ):
                    match = _TIME_TOKEN.fullmatch(token)
                    assert match is not None
                    time_seconds[name] = (
                        float(match.group("value"))
                        * _TIME_SCALE[match.group("prefix")]
                    )
                ascending_intervals = (
                    time_seconds["ascending_stop"]
                    - time_seconds["ascending_start"]
                ) / time_seconds["sample_period"]
                descending_intervals = (
                    time_seconds["descending_stop"]
                    - time_seconds["descending_start"]
                ) / time_seconds["sample_period"]
                if (
                    maximum <= minimum
                    or rounded_intervals < 1
                    or not math.isclose(
                        intervals, rounded_intervals, rel_tol=0.0, abs_tol=1.0e-9
                    )
                    or not math.isclose(
                        ascending_intervals,
                        rounded_intervals,
                        rel_tol=0.0,
                        abs_tol=1.0e-6,
                    )
                    or not math.isclose(
                        descending_intervals,
                        rounded_intervals,
                        rel_tol=0.0,
                        abs_tol=1.0e-6,
                    )
                ):
                    raise ValueError(f"Bank calibration {phase} sweep shape is inconsistent")
                return (
                    minimum,
                    maximum,
                    step,
                    ascending_start,
                    ascending_stop,
                    descending_start,
                    descending_stop,
                    sample_period,
                )

            raw = sweep_settings("raw")
            post = sweep_settings("post")
            retention_sweep = sweep_settings("retention")
            retention_hold_time = _finite_number(
                table.get("retention_hold_time_s"),
                "Bank calibration retention_hold_time_s",
                positive=True,
            )
            retention_hold_start = _positive_time_token(
                table.get("retention_hold_start"),
                "Bank calibration retention_hold_start",
            )
            retention_hold_sample = _positive_time_token(
                table.get("retention_hold_sample"),
                "Bank calibration retention_hold_sample",
            )

            def time_seconds(token: str) -> float:
                match = _TIME_TOKEN.fullmatch(token)
                assert match is not None
                return (
                    float(match.group("value"))
                    * _TIME_SCALE[match.group("prefix")]
                )

            if not math.isclose(
                time_seconds(retention_hold_sample)
                - time_seconds(retention_hold_start),
                retention_hold_time,
                rel_tol=0.0,
                abs_tol=max(1.0e-18, retention_hold_time * 1.0e-12),
            ):
                raise ValueError(
                    "Bank calibration retention hold sample does not match hold time"
                )

            def add_boundary_outputs(
                phase: str,
                direction: str,
                start: str,
                stop: str,
                period: str,
            ) -> None:
                cross_type = "rising" if direction == "ascending" else "falling"
                input_wave = (
                    f'sample((VT("/VINP")-VT("/VINN")) {start} {stop} '
                    f'"linear" {period})'
                )
                for path in range(paths):
                    outp = f"/OUTP{path}"
                    outn = f"/OUTN{path}"
                    output_wave = (
                        f'sample((VT("{outp}")-VT("{outn}")) {start} {stop} '
                        f'"linear" {period})'
                    )
                    onehot_wave = (
                        f'sample(((VT("{outp}")-{threshold_token})*'
                        f'(VT("{outn}")-{threshold_token})) {start} {stop} '
                        f'"linear" {period})'
                    )
                    outputs.extend(
                        (
                            (
                                f"diag_bank_{phase}_{direction}_boundary_{path:02d}",
                                f'value({input_wave} cross({output_wave} 0 1 '
                                f'"{cross_type}" nil nil))',
                            ),
                            (
                                f"diag_bank_{phase}_{direction}_onehot_{path:02d}",
                                f"ymax({onehot_wave})",
                            ),
                        )
                    )

            add_boundary_outputs("raw", "ascending", raw[3], raw[4], raw[7])
            add_boundary_outputs("raw", "descending", raw[5], raw[6], raw[7])
            add_boundary_outputs("post", "ascending", post[3], post[4], post[7])
            add_boundary_outputs("post", "descending", post[5], post[6], post[7])
            add_boundary_outputs(
                "retention", "ascending",
                retention_sweep[3], retention_sweep[4], retention_sweep[7],
            )
            add_boundary_outputs(
                "retention", "descending",
                retention_sweep[5], retention_sweep[6], retention_sweep[7],
            )
            for path in range(paths):
                outputs.extend(
                    (
                        (
                            f"bank_retention_vcalp_{path}",
                            f'value(VT("/VCALP{path}") {retention_hold_sample})',
                        ),
                        (
                            f"bank_retention_vcaln_{path}",
                            f'value(VT("/VCALN{path}") {retention_hold_sample})',
                        ),
                    )
                )
            settings.update(
                {
                    "monte_carlo_samples": monte_carlo_samples,
                    "model_file": model_file,
                    "model_sections": model_sections,
                    "transient_stop": transient_stop,
                    "transient_maxstep": transient_maxstep,
                    "boundary_directions": ("ascending", "descending"),
                    "supply_v": _finite_number(
                        table.get("supply_v"),
                        "Bank calibration diagnostic_equivalence.supply_v",
                        positive=True,
                    ),
                    "raw_half_span_v": _finite_number(
                        table.get("raw_half_span_v"),
                        "Bank calibration raw_half_span_v",
                        positive=True,
                    ),
                    "post_half_span_v": _finite_number(
                        table.get("post_half_span_v"),
                        "Bank calibration post_half_span_v",
                        positive=True,
                    ),
                    "raw_sweep_min_v": raw[0],
                    "raw_sweep_max_v": raw[1],
                    "raw_sweep_step_v": raw[2],
                    "raw_ascending_start": raw[3],
                    "raw_ascending_stop": raw[4],
                    "raw_descending_start": raw[5],
                    "raw_descending_stop": raw[6],
                    "raw_sample_period": raw[7],
                    "post_sweep_min_v": post[0],
                    "post_sweep_max_v": post[1],
                    "post_sweep_step_v": post[2],
                    "post_ascending_start": post[3],
                    "post_ascending_stop": post[4],
                    "post_descending_start": post[5],
                    "post_descending_stop": post[6],
                    "post_sample_period": post[7],
                    "retention_hold_time_s": retention_hold_time,
                    "retention_hold_start": retention_hold_start,
                    "retention_hold_sample": retention_hold_sample,
                    "retention_half_span_v": _finite_number(
                        table.get("retention_half_span_v"),
                        "Bank calibration retention_half_span_v",
                        positive=True,
                    ),
                    "retention_sweep_min_v": retention_sweep[0],
                    "retention_sweep_max_v": retention_sweep[1],
                    "retention_sweep_step_v": retention_sweep[2],
                    "retention_ascending_start": retention_sweep[3],
                    "retention_ascending_stop": retention_sweep[4],
                    "retention_descending_start": retention_sweep[5],
                    "retention_descending_stop": retention_sweep[6],
                    "retention_sample_period": retention_sweep[7],
                }
            )
        for step, sample_time in enumerate(decision_times):
            for path in range(paths):
                outp = f'/OUTP{path}'
                outn = f'/OUTN{path}'
                outputs.extend(
                    (
                        (
                            f"diag_bank_decision_diff_{step:03d}_{path:02d}",
                            f'value((VT("{outp}")-VT("{outn}")) {sample_time})',
                        ),
                        (
                            f"diag_bank_decision_onehot_{step:03d}_{path:02d}",
                            f'(value(VT("{outp}") {sample_time})-{threshold_token})*'
                            f'(value(VT("{outn}") {sample_time})-{threshold_token})',
                        ),
                    )
                )
        for step, sample_time in enumerate(transfer_times):
            for path in range(paths):
                outputs.append(
                    (
                        f"diag_bank_vcal_diff_{step:03d}_{path:02d}",
                        f'value((VT("/VCALP{path}")-VT("/VCALN{path}")) '
                        f'{sample_time})',
                    )
                )
    elif kind == "frontend_valid_edges":
        required = {
            "kind", "scores", "warmup_scores", "edge_signal", "edge_count",
            "threshold_v", "window_starts", "window_ends",
            "ready_signal", "result_sample_delay", "result_signals",
            "vcm_dac_by_corner", "vcm_adc_by_corner", "vcm_cal_by_corner",
            "exact_window", "raw_domain", "decisions", "history_directions",
            "diagnostic_equal_drive_values_v", "code_mapping_contract",
        }
        if set(table) != required:
            raise ValueError(
                "frontend diagnostic_equivalence fields must be exactly "
                + ", ".join(sorted(required))
            )
        scores = _integer_list(
            table.get("scores"), "frontend diagnostic_equivalence.scores"
        )
        warmup = _integer_list(
            table.get("warmup_scores"),
            "frontend diagnostic_equivalence.warmup_scores",
        )
        starts = _time_list(
            table.get("window_starts"),
            "frontend diagnostic_equivalence.window_starts",
        )
        ends = _time_list(
            table.get("window_ends"),
            "frontend diagnostic_equivalence.window_ends",
        )
        if len(starts) != len(ends) or len(starts) != len((*warmup, *scores)):
            raise ValueError(
                "frontend edge window count must match warmup plus scores"
            )
        edge_signal = _strings(
            [table.get("edge_signal")],
            "frontend diagnostic_equivalence.edge_signal",
        )[0]
        if not edge_signal.startswith("/"):
            raise ValueError("frontend edge_signal must be an absolute OA net name")
        ready_signal = _strings(
            [table.get("ready_signal")],
            "frontend diagnostic_equivalence.ready_signal",
        )[0]
        if not ready_signal.startswith("/"):
            raise ValueError("frontend ready_signal must be an absolute OA net name")
        result_sample_delay = _positive_time_token(
            table.get("result_sample_delay"),
            "frontend diagnostic_equivalence.result_sample_delay",
        )
        result_signals = tuple(
            signal for signal in _strings(
                table.get("result_signals"),
                "frontend diagnostic_equivalence.result_signals",
            ) if signal.startswith("/")
        )
        if len(result_signals) != len(table.get("result_signals", ())):
            raise ValueError("frontend result_signals must be absolute OA net names")
        required_results = {"/D5", "/D4", "/D3", "/D2", "/D1", "/D0"}
        if not required_results.issubset(result_signals):
            raise ValueError("frontend result_signals must include D[5:0]")
        edge_count = table.get("edge_count")
        if isinstance(edge_count, bool) or not isinstance(edge_count, int) or edge_count <= 0:
            raise ValueError("frontend edge_count must be a positive integer")
        threshold = _finite_number(
            table.get("threshold_v"),
            "frontend diagnostic_equivalence.threshold_v",
            positive=True,
        )
        reference_maps = {
            name: _table(
                table.get(name),
                f"frontend diagnostic_equivalence.{name}",
            )
            for name in (
                "vcm_dac_by_corner", "vcm_adc_by_corner", "vcm_cal_by_corner"
            )
        }
        for name, values in reference_maps.items():
            if not values or any(
                not isinstance(key, str)
                or _finite_number(value, f"frontend {name} value") <= 0
                for key, value in values.items()
            ):
                raise ValueError(f"frontend {name} must contain positive values")
        reference_key_sets = {tuple(sorted(values)) for values in reference_maps.values()}
        if len(reference_key_sets) != 1:
            raise ValueError("frontend reference maps must cover the same corners")
        exact_window = _integer_list(
            table.get("exact_window"),
            "frontend diagnostic_equivalence.exact_window",
        )
        raw_domain = _integer_list(
            table.get("raw_domain"),
            "frontend diagnostic_equivalence.raw_domain",
        )
        if len(exact_window) != 2 or len(raw_domain) != 2:
            raise ValueError("frontend score windows must have two endpoints")
        decisions = table.get("decisions")
        if isinstance(decisions, bool) or not isinstance(decisions, int) or decisions <= 0:
            raise ValueError("frontend decisions must be a positive integer")
        history_directions = _strings(
            table.get("history_directions"),
            "frontend diagnostic_equivalence.history_directions",
        )
        common_modes = tuple(
            _finite_number(value, "frontend diagnostic_equal_drive_values_v", positive=True)
            for value in table.get("diagnostic_equal_drive_values_v", ())
        )
        if not common_modes:
            raise ValueError("frontend diagnostic_equal_drive_values_v must not be empty")
        code_mapping, code_mapping_source = load_code_mapping()
        support_sources = (code_mapping_source,)
        for index, (start, end) in enumerate(zip(starts, ends, strict=True)):
            ready_expression = (
                f'cross(clip(VT("{ready_signal}") {start} {end}) '
                f'{_format_calculator_number(threshold)} 1 '
                '"rising" nil "time")'
            )
            result_time_expression = (
                f"({ready_expression} + {result_sample_delay})"
            )
            outputs.append((f"diag_ready_edge_{index:03d}", ready_expression))
            for signal in result_signals:
                label = signal.strip("/").lower()
                outputs.append(
                    (
                        f"diag_result_{label}_{index:03d}",
                        f'value(VT("{signal}") {result_time_expression})',
                    )
                )
            for edge in range(1, edge_count + 1):
                cross_expression = (
                    f'cross(clip(VT("{edge_signal}") {start} {end}) '
                    f'{_format_calculator_number(threshold)} {edge} '
                    '"rising" nil "time")'
                )
                outputs.append(
                    (f"diag_valid_edge_{index:03d}_{edge - 1:02d}", cross_expression)
                )
        settings = {
            "kind": kind,
            "scores": scores,
            "warmup_scores": warmup,
            "expected_codes": tuple(
                code_mapping.code_for(score) for score in (*warmup, *scores)
            ),
            "edge_signal": edge_signal,
            "edge_count": edge_count,
            "threshold_v": threshold,
            "ready_signal": ready_signal,
            "result_sample_delay": result_sample_delay,
            "result_signals": result_signals,
            "window_starts": starts,
            "window_ends": ends,
            **{
                name: {str(key): float(value) for key, value in values.items()}
                for name, values in reference_maps.items()
            },
            "exact_window": exact_window,
            "raw_domain": raw_domain,
            "decisions": decisions,
            "history_directions": history_directions,
            "diagnostic_equal_drive_values_v": common_modes,
            "code_mapping": code_mapping.as_dict(),
            "code_mapping_contract": str(
                code_mapping_source.relative_to(project_root)
            ),
        }
    else:
        raise ValueError(
            "unsupported native diagnostic_equivalence kind: " + kind
        )

    return OANativeDiagnosticContract(
        kind=kind,
        settings=_freeze_settings(settings),  # type: ignore[arg-type]
        scalar_outputs=tuple(outputs),
        support_sources=support_sources,
    )


def _load_native_rdb_contract(
    path: Path,
    *,
    project_root: Path,
) -> OANativeRdbContract:
    """Load the source-owned native RDB identity audit model.

    This file is deliberately separate from ``simulation.toml``: it does not
    configure ADE/Maestro or define a qualification rule.  It is an
    independently reviewable expectation used only after Cadence's official
    RDB objects have been read.
    """

    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read native RDB contract {path}: {exc}") from exc
    if set(raw) - {
        "schema",
        "point_count",
        "corners",
        "tests",
        "waveforms",
        "scalars",
        "setup_identity",
        "legacy_equivalence",
        "diagnostic_equivalence",
    } or not {
        "schema",
        "point_count",
        "corners",
        "tests",
        "waveforms",
        "scalars",
    }.issubset(raw):
        raise ValueError(
            "native RDB contract fields must be exactly schema, point_count, "
            "corners, tests, waveforms, scalars, with optional setup_identity, "
            "legacy_equivalence and diagnostic_equivalence"
        )
    if raw.get("schema") not in {1, 2}:
        raise ValueError("native RDB contract schema must be 1 or 2")
    point_count = raw.get("point_count")
    if (
        isinstance(point_count, bool)
        or not isinstance(point_count, int)
        or point_count <= 0
    ):
        raise ValueError("native RDB contract point_count must be a positive integer")
    corners = tuple(
        _identifier(value, "native RDB contract corner")
        for value in _strings(raw.get("corners"), "native RDB contract corners")
    )
    tests = tuple(
        _identifier(value, "native RDB contract test")
        for value in _strings(raw.get("tests"), "native RDB contract tests")
    )

    waveform_rows = _rows(raw.get("waveforms"), "native RDB contract waveforms")
    waveform_outputs: list[tuple[str, str]] = []
    for index, row in enumerate(waveform_rows):
        field = f"native RDB contract waveforms[{index}]"
        if set(row) != {"name", "signal"}:
            raise ValueError(f"{field} fields must be exactly name and signal")
        name = _identifier(row.get("name"), f"{field}.name")
        signal = row.get("signal")
        if not isinstance(signal, str) or not signal.startswith("/"):
            raise ValueError(f"{field}.signal must be an absolute OA net name")
        waveform_outputs.append((name, signal))

    scalar_value = raw.get("scalars")
    scalar_rows = (
        ()
        if scalar_value == []
        else _rows(scalar_value, "native RDB contract scalars")
    )
    scalar_outputs: list[tuple[str, str]] = []
    for index, row in enumerate(scalar_rows):
        field = f"native RDB contract scalars[{index}]"
        if set(row) != {"name", "expression"}:
            raise ValueError(
                f"{field} fields must be exactly name and expression"
            )
        name = _identifier(row.get("name"), f"{field}.name")
        expression = row.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError(f"{field}.expression must be non-empty text")
        scalar_outputs.append((name, expression))

    setup_model_identities: tuple[tuple[str, str], ...] = ()
    setup_identity_raw = raw.get("setup_identity")
    if setup_identity_raw is not None:
        setup_identity = _table(
            setup_identity_raw,
            "native RDB contract setup_identity",
        )
        if set(setup_identity) != {"models"}:
            raise ValueError(
                "native RDB contract setup_identity fields must be exactly models"
            )
        model_rows = _rows(
            setup_identity.get("models"),
            "native RDB contract setup_identity.models",
        )
        models: list[tuple[str, str]] = []
        for index, row in enumerate(model_rows):
            field = f"native RDB contract setup_identity.models[{index}]"
            if set(row) != {"file", "section"}:
                raise ValueError(
                    f"{field} fields must be exactly file and section"
                )
            model_file = row.get("file")
            if (
                not isinstance(model_file, str)
                or not model_file
                or Path(model_file).name != model_file
            ):
                raise ValueError(f"{field}.file must be a stable file basename")
            section = _identifier(row.get("section"), f"{field}.section")
            models.append((model_file, section))
        if len(set(models)) != len(models):
            raise ValueError(
                "native RDB contract setup_identity models must be unique"
            )
        setup_model_identities = tuple(models)

    legacy_measurement: OANativeLegacyMeasurement | None = None
    legacy_raw = raw.get("legacy_equivalence")
    if legacy_raw is not None:
        legacy_table = _table(legacy_raw, "native RDB contract legacy_equivalence")
        if set(legacy_table) != {"alias", "sample_times", "series"}:
            raise ValueError(
                "native RDB contract legacy_equivalence fields must be exactly "
                "alias, sample_times, and series"
            )
        alias = _identifier(
            legacy_table.get("alias"),
            "native RDB contract legacy_equivalence.alias",
        )
        sample_times = tuple(
            _positive_time_token(value, "native RDB contract legacy sample time")
            for value in _strings(
                legacy_table.get("sample_times"),
                "native RDB contract legacy_equivalence.sample_times",
            )
        )
        series_rows = _rows(
            legacy_table.get("series"),
            "native RDB contract legacy_equivalence.series",
        )
        series: list[OANativeLegacySeries] = []
        for index, row in enumerate(series_rows):
            field = f"native RDB contract legacy_equivalence.series[{index}]"
            if set(row) != {"export", "signals", "prefix"}:
                raise ValueError(
                    f"{field} fields must be exactly export, signals, and prefix"
                )
            export = _identifier(row.get("export"), f"{field}.export")
            signals = tuple(
                signal
                for signal in _strings(row.get("signals"), f"{field}.signals")
                if signal.startswith("/")
            )
            if len(signals) != len(row.get("signals", ())):
                raise ValueError(f"{field}.signals must be absolute OA net names")
            prefix = _identifier(row.get("prefix"), f"{field}.prefix")
            series.append(
                OANativeLegacySeries(export=export, signals=signals, prefix=prefix)
            )
        if len({item.export for item in series}) != len(series):
            raise ValueError("native legacy series exports must be unique")
        if len({item.prefix for item in series}) != len(series):
            raise ValueError("native legacy series prefixes must be unique")
        legacy_measurement = OANativeLegacyMeasurement(
            alias=alias,
            sample_times=sample_times,
            series=tuple(series),
        )

    diagnostic_equivalence: OANativeDiagnosticContract | None = None
    diagnostic_raw = raw.get("diagnostic_equivalence")
    if diagnostic_raw is not None:
        diagnostic_equivalence = _load_native_diagnostic_contract(
            diagnostic_raw,
            legacy_measurement,
            contract_path=path,
            project_root=project_root,
        )

    waveform_names = [name for name, _signal in waveform_outputs]
    explicit_scalar_names = [name for name, _expression in scalar_outputs]
    legacy_scalar_outputs = (
        ()
        if legacy_measurement is None
        else legacy_measurement.scalar_outputs
    )
    scalar_outputs.extend(legacy_scalar_outputs)
    diagnostic_scalar_outputs = (
        ()
        if diagnostic_equivalence is None
        else diagnostic_equivalence.scalar_outputs
    )
    scalar_outputs.extend(diagnostic_scalar_outputs)
    scalar_names = [name for name, _expression in scalar_outputs]
    if (
        diagnostic_equivalence is not None
        and diagnostic_equivalence.kind == "bank_calibration_measurement"
    ):
        monte_carlo_samples = diagnostic_equivalence.settings[
            "monte_carlo_samples"
        ]
        if point_count != monte_carlo_samples:
            raise ValueError(
                "Bank calibration native RDB point_count must equal the "
                "Maestro Monte Carlo sample count"
            )
    if len(set(waveform_names)) != len(waveform_names):
        raise ValueError("native RDB contract waveform names must be unique")
    if len(set(scalar_names)) != len(scalar_names):
        raise ValueError("native RDB contract scalar names must be unique")
    if set(explicit_scalar_names) & {
        name for name, _expression in legacy_scalar_outputs
    }:
        raise ValueError(
            "native RDB contract explicit and legacy scalar names must be disjoint"
        )
    if (
        set(name for name, _expression in diagnostic_scalar_outputs)
        & set(name for name, _expression in legacy_scalar_outputs)
    ):
        raise ValueError(
            "native RDB contract legacy and diagnostic scalar names must be disjoint"
        )
    if set(waveform_names) & set(scalar_names):
        raise ValueError(
            "native RDB contract waveform and scalar names must be disjoint"
        )
    return OANativeRdbContract(
        path=path.resolve(),
        point_count=point_count,
        corners=corners,
        tests=tests,
        waveform_outputs=tuple(waveform_outputs),
        scalar_outputs=tuple(scalar_outputs),
        setup_model_identities=setup_model_identities,
        legacy_measurement=legacy_measurement,
        diagnostic_equivalence=diagnostic_equivalence,
    )


def _validate_native_rdb_contract_source(
    contract: OANativeRdbContract,
    setup_source: Path,
) -> None:
    """Require the audit model to name identities actually declared by setup.il."""

    setup_text = setup_source.read_text(encoding="utf-8")
    missing: list[str] = []
    legacy_scalar_names = set(contract.legacy_scalar_names)
    diagnostic_scalar_names = set(contract.diagnostic_scalar_names)
    for name, signal in contract.waveform_outputs:
        if f'"{name}"' not in setup_text or f'"{signal}"' not in setup_text:
            missing.append(f"waveform {name}/{signal}")
    for name, expression in contract.scalar_outputs:
        if name in legacy_scalar_names or name in diagnostic_scalar_names:
            continue
        escaped_expression = expression.replace('"', r'\"')
        if (
            f'"{name}"' not in setup_text
            or f'"{escaped_expression}"' not in setup_text
        ):
            missing.append(f"scalar {name}/{expression}")
    for test in contract.tests:
        if f'"{test}"' not in setup_text:
            missing.append(f"test {test}")
    for corner in contract.corners:
        if f'"{corner}"' not in setup_text:
            missing.append(f"corner {corner}")
    for model_file, section in contract.setup_model_identities:
        if model_file not in setup_text or f'"{section}"' not in setup_text:
            missing.append(f"setup model {model_file}/{section}")
    legacy = contract.legacy_measurement
    if legacy is not None:
        if "legacySampleTimes" not in setup_text:
            missing.append("legacySampleTimes setup generator")
        if "legacySeries" not in setup_text:
            missing.append("legacySeries setup generator")
        for sample_time in legacy.sample_times:
            if f'"{sample_time}"' not in setup_text:
                missing.append(f"legacy sample time {sample_time}")
        for series in legacy.series:
            for value, label in ((series.export, "export"), (series.prefix, "prefix")):
                if f'"{value}"' not in setup_text:
                    missing.append(f"legacy {label} {value}")
            for signal in series.signals:
                if f'"{signal}"' not in setup_text:
                    missing.append(f"legacy signal {signal}")
        if '"%s_%03d_%02d"' not in setup_text:
            missing.append("legacy scalar output name generator")
        if '"value(VT(\\"%s\\") %s)"' not in setup_text:
            missing.append("legacy scalar expression generator")
    diagnostic = contract.diagnostic_equivalence
    if diagnostic is not None:
        if diagnostic.kind == "dac_window_statistics":
            markers = (
                "diagnosticWindowStarts",
                "diagnosticWindowEnds",
                "diagnosticSeries",
                '"diag_mean_%s_%03d_%02d"',
                '"diag_pp_%s_%03d_%02d"',
                'average(clip(VT(\\"%s\\") %s %s))',
                'ymax(clip(VT(\\"%s\\") %s %s))-ymin(clip(VT(\\"%s\\") %s %s))',
            )
        elif diagnostic.kind == "controller_sequence":
            markers = (
                "diagnosticFallSignals",
                "diagnosticFallWindowStarts",
                "diagnosticFallWindowEnds",
                '"diag_fall_%s_%03d"',
                'cross(clip(VT(\\"%s\\") %s %s)',
            )
        elif diagnostic.kind == "mx_valid_edges":
            markers = (
                "diagnosticEdgeWindows",
                '"diag_valid_edge_%03d_%02d"',
                'cross(clip(VT(\\"%s\\") %s %s)',
            )
        elif diagnostic.kind in {
            "bank_calibration_trajectory", "bank_calibration_measurement"
        }:
            markers = (
                "bankPathCount",
                "bankDecisionSampleTimes",
                "bankTransferSampleTimes",
                '"diag_bank_decision_diff_%03d_%02d"',
                '"diag_bank_decision_onehot_%03d_%02d"',
                '"diag_bank_vcal_diff_%03d_%02d"',
                'value((VT(\\"/OUTP%d\\")-VT(\\"/OUTN%d\\")) %s)',
                '(value(VT(\\"/OUTP%d\\") %s)-%s)*'
                '(value(VT(\\"/OUTN%d\\") %s)-%s)',
                'value((VT(\\"/VCALP%d\\")-VT(\\"/VCALN%d\\")) %s)',
            )
            if diagnostic.kind == "bank_calibration_measurement":
                markers += (
                    "llmCimNativeAddBankBoundary",
                    '"diag_bank_%s_%s_boundary_%02d"',
                    '"diag_bank_%s_%s_onehot_%02d"',
                    'sample((VT(\\"/VINP\\")-VT(\\"/VINN\\")) %s %s',
                    'cross(%s 0 1 \\"%s\\" nil nil)',
                    "llmCimNativeAddBankRetention",
                    '"bank_retention_vcalp_%d"',
                    '"bank_retention_vcaln_%d"',
                    'value(VT(\\"/VCALP%d\\") %s)',
                    'value(VT(\\"/VCALN%d\\") %s)',
                    "axlSetAllSweepsEnabled(sdb 0)",
                    "maeSetSpec(outputName testName ?lt \"0\"",
                )
        else:
            markers = (
                "diagnosticEdgeWindows",
                '"diag_valid_edge_%03d_%02d"',
                'cross(clip(VT(\\"%s\\") %s %s)',
                "diagnosticReadySignal",
                "diagnosticResultSampleDelay",
                "diagnosticResultSignals",
                '"diag_ready_edge_%03d"',
                '"diag_result_%s_%03d"',
                '"value(VT(\\"%s\\") (%s+%s))"',
            )
        for marker in markers:
            if marker not in setup_text:
                missing.append(f"diagnostic setup generator {marker}")
        for value in diagnostic.settings.get("window_starts", ()):
            if isinstance(value, str) and f'"{value}"' not in setup_text:
                missing.append(f"diagnostic window start {value}")
        for value in diagnostic.settings.get("window_ends", ()):
            if isinstance(value, str) and f'"{value}"' not in setup_text:
                missing.append(f"diagnostic window end {value}")
        for key in ("decision_sample_times", "transfer_sample_times"):
            for value in diagnostic.settings.get(key, ()):
                if isinstance(value, str) and f'"{value}"' not in setup_text:
                    missing.append(f"diagnostic {key} value {value}")
        if diagnostic.kind in {
            "bank_calibration_trajectory", "bank_calibration_measurement"
        }:
            paths = diagnostic.settings.get("paths")
            if f"bankPathCount = {paths}" not in setup_text:
                missing.append(f"diagnostic bank path count {paths}")
        if diagnostic.kind == "bank_calibration_measurement":
            for phase in ("raw", "post", "retention"):
                for key in (
                    f"{phase}_ascending_start",
                    f"{phase}_ascending_stop",
                    f"{phase}_descending_start",
                    f"{phase}_descending_stop",
                    f"{phase}_sample_period",
                ):
                    value = diagnostic.settings[key]
                    if f'"{value}"' not in setup_text:
                        missing.append(f"diagnostic {key} {value}")
            retention_hold_sample = diagnostic.settings["retention_hold_sample"]
            if f'"{retention_hold_sample}"' not in setup_text:
                missing.append(
                    f"diagnostic retention_hold_sample {retention_hold_sample}"
                )
        for key in (
            "series", "fall_signals", "edge_signal",
            "ready_signal", "result_signals",
        ):
            values = (
                (diagnostic.settings.get(key),)
                if key in {"edge_signal", "ready_signal"}
                else diagnostic.settings.get(key, ())
            )
            for value in values:
                if isinstance(value, str) and value and f'"{value}"' not in setup_text:
                    missing.append(f"diagnostic {key} {value}")
    if missing:
        raise ValueError(
            "native RDB contract is not consistent with setup.il: "
            + ", ".join(missing)
        )


def _load_native_oa_simulation_spec(
    spec_path: Path,
    project_root: Path,
    *,
    owner_root: Path,
    raw: Mapping[str, Any],
) -> OASimulationSpec:
    """Load the thin contract used by native ADE/Maestro pilot cells.

    The contract deliberately contains no analysis, corner, output, or
    measurement language.  Those semantics live in the source-owned setup
    program and are executed by the Cadence APIs during materialization.
    """

    allowed_root = {"schema", "testbench", "platform", "setup"}
    unknown_root = set(raw) - allowed_root
    if unknown_root:
        raise ValueError(
            "native OA simulation contract contains unsupported fields: "
            f"{sorted(unknown_root)}"
        )
    testbench = _table(raw.get("testbench"), "testbench")
    if set(testbench) != {"library", "cell", "dut", "source_view", "simulator"}:
        raise ValueError(
            "native testbench fields must be exactly library, cell, dut, "
            "source_view, and simulator"
        )
    platform = _table(raw.get("platform"), "platform")
    if set(platform) != {"pdk"}:
        raise ValueError("native platform fields must be exactly pdk")
    setup = _table(raw.get("setup"), "setup")
    if set(setup) != {"source"}:
        raise ValueError("native setup fields must be exactly source")

    library = _identifier(testbench.get("library"), "testbench.library")
    cell = _identifier(testbench.get("cell"), "testbench.cell")
    dut = _identifier(testbench.get("dut"), "testbench.dut")
    source_view = _identifier(testbench.get("source_view"), "testbench.source_view")
    simulator = _identifier(testbench.get("simulator"), "testbench.simulator")
    if simulator not in {"spectre", "ams"}:
        raise ValueError("testbench.simulator must be spectre or ams")
    pdk = load_pdk_config(
        project_root,
        _identifier(platform.get("pdk"), "platform.pdk"),
    )
    setup_value = setup.get("source")
    if not isinstance(setup_value, str) or not setup_value:
        raise ValueError("setup.source must be a cell-relative path")
    setup_source = (spec_path.parent / setup_value).resolve()
    cell_root = spec_path.parent.resolve()
    if not setup_source.is_relative_to(cell_root) or not setup_source.is_file():
        raise ValueError("setup.source must stay inside its testbench cell")
    if not setup_source.is_relative_to(owner_root.resolve()):
        raise ValueError("setup.source must stay inside its owning active IP")
    rdb_contract_path = (cell_root / "native_rdb.toml").resolve()
    rdb_contract = (
        _load_native_rdb_contract(
            rdb_contract_path,
            project_root=project_root,
        )
        if rdb_contract_path.is_file()
        else None
    )
    if rdb_contract is not None:
        _validate_native_rdb_contract_source(rdb_contract, setup_source)
    return OASimulationSpec(
        path=spec_path,
        project_root=project_root,
        library=library,
        cell=cell,
        dut=dut,
        top_view=source_view,
        simulator=simulator,
        native_setup=OANativeSetup(
            pdk=pdk,
            source=setup_source,
            rdb_contract=rdb_contract,
        ),
    )


def load_oa_simulation_spec(
    path: Path,
    *,
    project_root: Path,
) -> OASimulationSpec:
    """Load only the source-owned schema-3 thin native simulation contract."""

    spec_path = path.resolve()
    context = ProjectContext.from_project_root(project_root)
    root = context.project_root
    if not spec_path.is_file() or not spec_path.is_relative_to(root):
        raise ValueError("OA simulation spec must be a project-owned file")
    ip_root = context.ip_root
    if not spec_path.is_relative_to(ip_root):
        raise ValueError("OA simulation spec must be owned by an active IP")
    relative_to_ip = spec_path.relative_to(ip_root)
    if len(relative_to_ip.parts) < 2 or spec_path.is_relative_to(
        context.legacy_ip_root
    ):
        raise ValueError("OA simulation spec must be owned by an active IP")
    owner_root = ip_root / relative_to_ip.parts[0]
    try:
        with spec_path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read OA simulation spec {spec_path}: {exc}") from exc
    if raw.get("schema") != 3:
        raise ValueError(
            "OA simulation schema must be 3; schema-2 MDL contracts are no longer "
            "accepted by the native OA workflow"
        )
    return _load_native_oa_simulation_spec(
        spec_path,
        root,
        owner_root=owner_root,
        raw=raw,
    )
