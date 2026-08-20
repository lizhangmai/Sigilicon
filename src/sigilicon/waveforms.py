"""Simulator-independent sampled-waveform primitives.

This module deliberately contains no circuit, port, or pass/fail policy.  It
is the common parser/algorithm layer for direct tabular simulator output;
design recipes supply signal names, windows, and their own measurement
contracts.
"""

from __future__ import annotations

import csv
import io
import math
from bisect import bisect_left
from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class Waveform:
    """Strictly increasing sampled independent variable plus named traces."""

    time_s: tuple[float, ...]
    signals: Mapping[str, tuple[float, ...]]

    def __post_init__(self) -> None:
        if len(self.time_s) < 2:
            raise ValueError("waveform requires at least two samples")
        if any(not math.isfinite(value) for value in self.time_s):
            raise ValueError("waveform time contains a non-finite value")
        if any(right <= left for left, right in zip(self.time_s, self.time_s[1:])):
            raise ValueError("waveform time must be strictly increasing")
        for name, values in self.signals.items():
            if not name:
                raise ValueError("waveform signal names must be non-empty")
            if len(values) != len(self.time_s):
                raise ValueError(f"signal {name} length does not match waveform time")
            if any(not math.isfinite(value) for value in values):
                raise ValueError(f"signal {name} contains a non-finite value")

    def signal(self, name: str) -> tuple[float, ...]:
        try:
            return self.signals[name]
        except KeyError as exc:
            raise ValueError(f"waveform does not contain signal {name!r}") from exc


def parse_spectre_direct_print(text: str, *, signals: Sequence[str]) -> Waveform:
    """Parse a rectangular Spectre ``print`` table without partial-row guesses."""

    if not signals or len(set(signals)) != len(signals):
        raise ValueError("direct-print signal names must be unique and non-empty")
    rows: list[tuple[float, ...]] = []
    width = len(signals) + 1
    for line in text.splitlines():
        fields = line.replace(",", " ").split()
        if len(fields) < width:
            continue
        try:
            values = tuple(float(field) for field in fields[:width])
        except ValueError:
            continue
        if len(fields) != width:
            if all(_is_number(field) for field in fields):
                raise ValueError("unrecognized Spectre print row width")
            continue
        rows.append(values)
    if len(rows) < 2:
        raise ValueError("Spectre print output contains fewer than two data rows")
    return Waveform(
        time_s=tuple(row[0] for row in rows),
        signals={
            name: tuple(row[index + 1] for row in rows)
            for index, name in enumerate(signals)
        },
    )


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def _validate_sampled_curve(
    independent: Sequence[float],
    dependent: Sequence[float],
) -> None:
    if len(independent) != len(dependent) or len(independent) < 2:
        raise ValueError("sampled curve requires matching arrays with at least two points")
    if any(not math.isfinite(value) for value in (*independent, *dependent)):
        raise ValueError("sampled curve values must be finite")
    if any(right <= left for left, right in zip(independent, independent[1:])):
        raise ValueError("sampled curve independent values must be strictly increasing")


def zero_crossings(
    independent: Sequence[float],
    dependent: Sequence[float],
    *,
    zero_tolerance: float,
    merge_independent_tolerance: float = 0.0,
) -> tuple[float, ...]:
    """Return linear-interpolated roots of a sampled scalar curve.

    ``zero_tolerance`` is expressed in the dependent variable's unit, and
    ``merge_independent_tolerance`` is expressed in the independent variable's
    unit.  Neither carries circuit or measurement-policy semantics.
    """

    _validate_sampled_curve(independent, dependent)
    if not math.isfinite(zero_tolerance) or zero_tolerance < 0.0:
        raise ValueError("zero tolerance must be finite and non-negative")
    if (
        not math.isfinite(merge_independent_tolerance)
        or merge_independent_tolerance < 0.0
    ):
        raise ValueError("merge tolerance must be finite and non-negative")

    roots: list[float] = []
    for (left_x, left_y), (right_x, right_y) in zip(
        zip(independent, dependent), zip(independent[1:], dependent[1:])
    ):
        if abs(left_y) <= zero_tolerance:
            roots.append(left_x)
        if left_y * right_y < 0.0:
            roots.append(left_x + (-left_y / (right_y - left_y)) * (right_x - left_x))
    if abs(dependent[-1]) <= zero_tolerance:
        roots.append(independent[-1])

    result: list[float] = []
    for root in sorted(roots):
        if not result or abs(root - result[-1]) > merge_independent_tolerance:
            result.append(root)
    return tuple(result)


def piecewise_linear_slope_at(
    independent: Sequence[float],
    dependent: Sequence[float],
    independent_value: float,
) -> float:
    """Return the enclosing segment's slope at one sampled-curve coordinate."""

    _validate_sampled_curve(independent, dependent)
    if not math.isfinite(independent_value):
        raise ValueError("requested independent value must be finite")
    for left in range(len(independent) - 1):
        right = left + 1
        if independent[left] <= independent_value <= independent[right]:
            return (dependent[right] - dependent[left]) / (
                independent[right] - independent[left]
            )
    raise ValueError("requested independent value is outside sampled-curve bounds")


def render_sampled_csv(waveform: Waveform, *, independent_name: str) -> str:
    """Render a stable CSV with an explicit independent-variable column name."""

    if not independent_name or "," in independent_name or "\n" in independent_name:
        raise ValueError("independent CSV column name must be a non-empty single field")

    stream = io.StringIO(newline="")
    names = tuple(waveform.signals)
    writer = csv.writer(stream)
    writer.writerow((independent_name, *names))
    for index, time_s in enumerate(waveform.time_s):
        writer.writerow(
            (
                f"{time_s:.17g}",
                *(f"{waveform.signals[name][index]:.17g}" for name in names),
            )
        )
    return stream.getvalue()


def render_waveform_csv(waveform: Waveform) -> str:
    """Render a transient waveform CSV whose independent variable is seconds."""

    return render_sampled_csv(waveform, independent_name="time_s")


def waveform_from_csv(text: str) -> Waveform:
    """Load a CSV generated by :func:`render_waveform_csv`."""

    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or not rows[0] or "time_s" not in rows[0]:
        raise ValueError("waveform CSV must contain a time_s column and data rows")
    names = tuple(name for name in rows[0] if name != "time_s")
    if not names:
        raise ValueError("waveform CSV must contain at least one signal")
    return Waveform(
        time_s=tuple(float(row["time_s"]) for row in rows),
        signals={name: tuple(float(row[name]) for row in rows) for name in names},
    )


def value_at(waveform: Waveform, signal: str, time_s: float) -> float:
    """Return a linearly interpolated sample within the captured domain."""

    if time_s < waveform.time_s[0] or time_s > waveform.time_s[-1]:
        raise ValueError(f"requested time {time_s:g} is outside waveform bounds")
    values = waveform.signal(signal)
    right_index = bisect_left(waveform.time_s, time_s)
    if right_index < len(waveform.time_s) and waveform.time_s[right_index] == time_s:
        return values[right_index]
    left_index = right_index - 1
    left = waveform.time_s[left_index]
    right = waveform.time_s[right_index]
    fraction = (time_s - left) / (right - left)
    return values[left_index] + fraction * (values[right_index] - values[left_index])


def pwl_active_intervals(
    initial_value: float,
    transitions: Sequence[tuple[float, float]],
    *,
    edge_s: float,
    stop_s: float,
    active_reference: float,
) -> tuple[tuple[tuple[float, float], ...], tuple[tuple[float, float], ...]]:
    """Return active and physical-transition intervals for a rendered PWL control.

    The construction matches the repository's voltage-source renderers: each
    effective target change begins at its declared time and ends one ``edge_s``
    later.  Active-state boundaries use the exact linear crossing of
    ``active_reference``; duplicate no-change targets are ignored.
    """

    numeric = (initial_value, edge_s, stop_s, active_reference)
    if any(not math.isfinite(value) for value in numeric):
        raise ValueError("PWL interval inputs must be finite")
    if edge_s <= 0.0 or stop_s <= 0.0:
        raise ValueError("PWL edge and stop time must be positive")
    if initial_value == active_reference:
        raise ValueError("PWL initial value cannot equal the active reference")

    current = initial_value
    state_active = current > active_reference
    active_start = 0.0 if state_active else None
    active: list[tuple[float, float]] = []
    physical_edges: list[tuple[float, float]] = []
    previous_effective_edge_end = 0.0
    for time_s, target in transitions:
        if any(not math.isfinite(value) for value in (time_s, target)):
            raise ValueError("PWL transition values must be finite")
        if time_s < 0.0 or time_s + edge_s > stop_s:
            raise ValueError("PWL transition lies outside the rendered interval")
        if target == current:
            continue
        if time_s < previous_effective_edge_end:
            raise ValueError("PWL control transitions overlap before rendering")
        if target == active_reference:
            raise ValueError("PWL target cannot equal the active reference")
        if (current > active_reference) == (target > active_reference):
            raise ValueError("PWL control target changes without crossing active reference")
        crossing_fraction = (active_reference - current) / (target - current)
        crossing_s = time_s + edge_s * crossing_fraction
        next_active = target > active_reference
        if state_active and not next_active:
            assert active_start is not None
            active.append((active_start, crossing_s))
            active_start = None
        elif not state_active and next_active:
            active_start = crossing_s
        physical_edges.append((time_s, time_s + edge_s))
        previous_effective_edge_end = time_s + edge_s
        current = target
        state_active = next_active
    if state_active:
        assert active_start is not None
        active.append((active_start, stop_s))
    return tuple(active), tuple(physical_edges)


def interval_overlap_duration(
    first: Sequence[tuple[float, float]],
    second: Sequence[tuple[float, float]],
) -> float:
    """Return the union duration where intervals from both sets overlap."""

    intersections = [
        (max(left_start, right_start), min(left_stop, right_stop))
        for left_start, left_stop in first
        for right_start, right_stop in second
        if max(left_start, right_start) < min(left_stop, right_stop)
    ]
    if not intersections:
        return 0.0
    ordered = sorted(intersections)
    merged: list[tuple[float, float]] = [ordered[0]]
    for start_s, stop_s in ordered[1:]:
        previous_start, previous_stop = merged[-1]
        if start_s <= previous_stop:
            merged[-1] = (previous_start, max(previous_stop, stop_s))
        else:
            merged.append((start_s, stop_s))
    return sum(stop_s - start_s for start_s, stop_s in merged)


def minimum_interval_gap(
    first: Sequence[tuple[float, float]],
    second: Sequence[tuple[float, float]],
) -> float | None:
    """Return the smallest nonnegative gap between differently labelled intervals."""

    labelled = sorted(
        (*((start, stop, 0) for start, stop in first),
         *((start, stop, 1) for start, stop in second))
    )
    gaps = [
        right[0] - left[1]
        for left, right in zip(labelled, labelled[1:])
        if left[2] != right[2] and right[0] >= left[1]
    ]
    if interval_overlap_duration(first, second) > 0.0:
        gaps.append(0.0)
    return min(gaps) if gaps else None


def window_knots(
    waveform: Waveform,
    signal: str,
    start_s: float,
    end_s: float,
) -> tuple[tuple[float, float], ...]:
    """Return samples plus interpolated endpoints for one closed time window."""

    if end_s <= start_s:
        raise ValueError("measurement window must have positive duration")
    if start_s < waveform.time_s[0] or end_s > waveform.time_s[-1]:
        raise ValueError("measurement window exceeds waveform bounds")
    points = [(start_s, value_at(waveform, signal, start_s))]
    values = waveform.signal(signal)
    points.extend(
        (time_s, values[index])
        for index, time_s in enumerate(waveform.time_s)
        if start_s < time_s < end_s
    )
    points.append((end_s, value_at(waveform, signal, end_s)))
    return tuple(points)


def mean_in_window(waveform: Waveform, signal: str, start_s: float, end_s: float) -> float:
    """Trapezoidal mean of a sampled trace over a declared time window."""

    knots = window_knots(waveform, signal, start_s, end_s)
    area = sum(
        (right_t - left_t) * (left_v + right_v) / 2.0
        for (left_t, left_v), (right_t, right_v) in zip(knots, knots[1:])
    )
    return area / (end_s - start_s)


def extrema_in_window(
    waveform: Waveform,
    signal: str,
    start_s: float,
    end_s: float,
) -> tuple[float, float]:
    values = tuple(value for _time, value in window_knots(waveform, signal, start_s, end_s))
    return min(values), max(values)


def first_crossing(
    waveform: Waveform,
    signal: str,
    threshold: float,
    *,
    direction: str,
    start_s: float,
    end_s: float | None = None,
) -> float | None:
    """Find the first linearly interpolated threshold crossing in a window."""

    if direction not in {"rising", "falling"}:
        raise ValueError("crossing direction must be rising or falling")
    stop_s = waveform.time_s[-1] if end_s is None else end_s
    knots = window_knots(waveform, signal, start_s, stop_s)
    for (left_t, left_v), (right_t, right_v) in zip(knots, knots[1:]):
        crossed = (
            left_v < threshold <= right_v
            if direction == "rising"
            else left_v > threshold >= right_v
        )
        if not crossed:
            continue
        if right_v == left_v:
            return right_t
        fraction = (threshold - left_v) / (right_v - left_v)
        return left_t + fraction * (right_t - left_t)
    return None


def positive_supply_energy(
    waveform: Waveform,
    *,
    vdd: float,
    current_signal: str,
    start_s: float,
    end_s: float,
) -> float:
    """Integrate positive energy drawn from a DC supply source, in joules.

    For the usual Spectre voltage-source sign convention, source-delivered
    power is ``-VDD * I(source)``.  Returned energy is clipped to zero; recipes
    that need net energy should implement that distinct definition explicitly.
    """

    knots = window_knots(waveform, current_signal, start_s, end_s)
    powers = tuple(max(0.0, -vdd * current) for _time, current in knots)
    return sum(
        (right_t - left_t) * (left_p + right_p) / 2.0
        for ((left_t, _), left_p), ((right_t, _), right_p) in zip(
            zip(knots, powers), zip(knots[1:], powers[1:])
        )
    )
