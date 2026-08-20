from __future__ import annotations

from types import SimpleNamespace

from sigilicon.waveforms import Waveform
from sigilicon.workflows.nominal_cell import LogicCase, evaluate_logic_waveform
from sigilicon.workflows.nominal_switch import evaluate_switch_waveform


def test_nominal_logic_evaluator_reports_truth_and_measured_delay() -> None:
    config = SimpleNamespace(
        design=SimpleNamespace(inputs=("A",)), cycle_s=1.0, stimulus_s=0.2,
        sample_s=0.8, vdd_v=0.9,
        cases=(
            LogicCase("static_low", "truth", (0,), (0,), 1, 1, None),
            LogicCase("fall_after_a_rises", "delay", (0,), (1,), 1, 0, "A"),
        ),
    )
    time_s = (0.0, 0.19, 0.2, 0.21, 0.8, 0.99, 1.0, 1.19, 1.2, 1.21, 1.25, 1.8, 2.0)
    waveform = Waveform(
        time_s,
        {
            "a": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.9, 0.9, 0.9, 0.9),
            "out": (0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.0, 0.0, 0.0),
        },
    )

    result = evaluate_logic_waveform(config, waveform)

    assert result["passed"] is True
    assert result["criteria"] == {
        "complete_truth_table": True,
        "delay_transitions_correct": True,
        "all_declared_delays_measured": True,
    }
    assert 0.0 < result["maximum_propagation_delay_s"] < 0.1
    assert result["diagnostic_semantics"]["fixed_propagation_delay_limit_s"] is None


def test_nominal_switch_evaluator_keeps_direction_isolation_ron_separate() -> None:
    config = SimpleNamespace(
        vdd_v=0.9, vcm_v=0.45, ab_edge_s=0.5, source_edge_s=0.1,
        ab_sample_s=1.0, ba_edge_s=1.5, ba_sample_s=2.0,
        ron_sample_s=2.25, isolation_end_s=2.5,
        ron_delta_v=0.05,
        load_f=2.0e-14, leakage_resistance_ohm=1.0e9,
    )
    time_s = (0.0, 0.4, 0.5, 0.6, 0.7, 1.0, 1.5, 1.6, 1.7, 2.0, 2.25, 2.5, 3.0)
    waveform = Waveform(
        time_s,
        {
            "a_ab": (0.0, 0.0, 0.0, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9),
            "b_ab": (0.0, 0.0, 0.0, 0.4, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9),
            "a_ba": (0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.4, 0.0, 0.0, 0.0, 0.0, 0.0),
            "b_ba": (0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            "a_off": (0.0, 0.0, 0.0, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9),
            "b_off": (0.45, 0.45, 0.45, 0.451, 0.451, 0.451, 0.451, 0.451, 0.451, 0.451, 0.451, 0.451, 0.451),
            "iron": (0.001,) * len(time_s),
        },
    )

    result = evaluate_switch_waveform(config, waveform)

    assert result["passed"] is True
    assert result["off_isolation_peak_disturbance_v"] < 0.005
    assert result["on_resistance_ohm"] == 50.0
    assert result["diagnostic_semantics"]["fixed_on_resistance_limit_ohm"] is None
