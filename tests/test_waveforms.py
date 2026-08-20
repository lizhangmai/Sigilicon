from __future__ import annotations

import math

import pytest

from sigilicon.waveforms import (
    Waveform,
    first_crossing,
    interval_overlap_duration,
    mean_in_window,
    minimum_interval_gap,
    parse_spectre_direct_print,
    piecewise_linear_slope_at,
    positive_supply_energy,
    pwl_active_intervals,
    render_sampled_csv,
    render_waveform_csv,
    waveform_from_csv,
    value_at,
    zero_crossings,
)


def test_direct_print_parser_requires_rectangular_samples_and_round_trips_csv() -> None:
    waveform = parse_spectre_direct_print(
        "header\n0 0 1\n1e-9 1 0\n",
        signals=("Q", "QB"),
    )

    assert waveform.signal("Q") == (0.0, 1.0)
    assert waveform_from_csv(render_waveform_csv(waveform)) == waveform
    assert render_sampled_csv(waveform, independent_name="force_voltage_v").splitlines()[0] == (
        "force_voltage_v,Q,QB"
    )
    assert first_crossing(
        waveform,
        "Q",
        0.5,
        direction="rising",
        start_s=0.0,
    ) == 0.5e-9

    with pytest.raises(ValueError, match="row width"):
        parse_spectre_direct_print("0 1 2 3\n1 2 3 4\n", signals=("Q", "QB"))


def test_window_algorithms_interpolate_and_integrate_declared_boundaries() -> None:
    waveform = Waveform(
        time_s=(0.0, 1.0, 2.0),
        signals={"I_VDD": (0.0, -2.0, 0.0)},
    )

    assert math.isclose(mean_in_window(waveform, "I_VDD", 0.5, 1.5), -1.5)
    assert math.isclose(
        positive_supply_energy(
            waveform,
            vdd=1.0,
            current_signal="I_VDD",
            start_s=0.5,
            end_s=1.5,
        ),
        1.5,
    )
    assert value_at(waveform, "I_VDD", 0.0) == 0.0
    assert value_at(waveform, "I_VDD", 1.0) == -2.0
    assert value_at(waveform, "I_VDD", 1.5) == -1.0
    assert value_at(waveform, "I_VDD", 2.0) == 0.0


def test_sampled_curve_roots_and_segment_slope_are_axis_generic() -> None:
    independent = (0.0, 0.2, 0.4, 0.6)
    dependent = (0.0, -2.0, 2.0, 4.0)

    assert zero_crossings(
        independent,
        dependent,
        zero_tolerance=1e-15,
    ) == pytest.approx((0.0, 0.3))
    assert piecewise_linear_slope_at(independent, dependent, 0.3) == pytest.approx(20.0)


def test_sampled_curve_algorithms_reject_invalid_axis_and_tolerances() -> None:
    with pytest.raises(ValueError, match="strictly increasing"):
        zero_crossings((0.0, 0.0), (1.0, -1.0), zero_tolerance=0.0)
    with pytest.raises(ValueError, match="zero tolerance"):
        zero_crossings((0.0, 1.0), (1.0, -1.0), zero_tolerance=-1.0)
    with pytest.raises(ValueError, match="outside"):
        piecewise_linear_slope_at((0.0, 1.0), (0.0, 1.0), 2.0)


def test_pwl_control_intervals_use_actual_reference_crossings_and_edges() -> None:
    first, first_edges = pwl_active_intervals(
        0.0, ((1.0, 1.0), (3.0, 0.0)),
        edge_s=0.2, stop_s=5.0, active_reference=0.5,
    )
    second, second_edges = pwl_active_intervals(
        0.0, ((3.4, 1.0), (4.4, 0.0)),
        edge_s=0.2, stop_s=5.0, active_reference=0.5,
    )
    assert len(first) == 1 and first[0] == pytest.approx((1.1, 3.1))
    assert len(second) == 1 and second[0] == pytest.approx((3.5, 4.5))
    assert all(
        actual == pytest.approx(expected)
        for actual, expected in zip(
            first_edges, ((1.0, 1.2), (3.0, 3.2)), strict=True
        )
    )
    assert all(
        actual == pytest.approx(expected)
        for actual, expected in zip(
            second_edges, ((3.4, 3.6), (4.4, 4.6)), strict=True
        )
    )
    assert interval_overlap_duration(first, second) == 0.0
    assert minimum_interval_gap(first, second) == pytest.approx(0.4)

    overlapping, _ = pwl_active_intervals(
        0.0, ((2.8, 1.0), (4.0, 0.0)),
        edge_s=0.2, stop_s=5.0, active_reference=0.5,
    )
    assert interval_overlap_duration(first, overlapping) == pytest.approx(0.2)
    assert minimum_interval_gap(first, overlapping) == 0.0
