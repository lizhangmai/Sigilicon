from __future__ import annotations

from types import SimpleNamespace

from sigilicon.domain.valid_edge_diagnostics import (
    ValidEdgeContractDefinition,
    ValidEdgeResultBinding,
    ValidEdgeScenario,
    build_valid_edge_contract,
    evaluate_valid_edge_diagnostic,
)


def test_valid_edge_algorithm_consumes_resolved_scenarios_without_product_policy() -> None:
    diagnostic = build_valid_edge_contract(
        ValidEdgeContractDefinition(
            kind="example_valid_edges",
            scenarios=(
                ValidEdgeScenario(
                    name="case_a",
                    expected_code=2,
                    metadata={"caller_label": "opaque"},
                ),
            ),
            sample_times=("1.5n",),
            ready_signal="/RDY",
            decision_signals=("/D1", "/D0"),
            analog_signals=("/VP", "/VN"),
            window_starts=("1n",),
            window_ends=("2n",),
            edge_signal="/VALID",
            edge_count=2,
            threshold_v=0.45,
        )
    )
    contract = SimpleNamespace(
        diagnostic_equivalence=diagnostic,
        point_count=1,
        corners=("tt",),
        tests=("tran",),
    )
    result = {
        "outputs": [
            {
                "point": 1,
                "corner": "tt",
                "test": "tran",
                "name": name,
                "value": value,
            }
            for name, value in (
                ("diag_ready_000_00", 0.9),
                ("diag_decision_000_00", 0.9),
                ("diag_decision_000_01", 0.0),
                ("diag_analog_000_00", 0.2),
                ("diag_analog_000_01", 0.7),
            )
        ] + [
            {
                "point": 1,
                "corner": "tt",
                "test": "tran",
                "name": f"diag_valid_edge_000_{edge:02d}",
                "value": 1.1e-9 + edge * 0.1e-9,
            }
            for edge in range(2)
        ]
    }

    decoded = evaluate_valid_edge_diagnostic(
        result,
        contract,
        binding=ValidEdgeResultBinding(
            analog_output_keys=("positive_v", "negative_v"),
        ),
    )

    assert [name for name, _expression in diagnostic.scalar_outputs] == [
        "diag_ready_000_00",
        "diag_decision_000_00",
        "diag_decision_000_01",
        "diag_analog_000_00",
        "diag_analog_000_01",
        "diag_valid_edge_000_00",
        "diag_valid_edge_000_01",
    ]
    assert 'VT("/VALID") 1n 2n' in diagnostic.scalar_outputs[-1][1]
    context = decoded["contexts"][0]
    assert context["passed"] is True
    assert context["rows"] == [
        {
            "caller_label": "opaque",
            "name": "case_a",
            "expected_code": 2,
            "code": 2,
            "code_bits_msb_first": [1, 0],
            "rdy": 1,
            "valid_rising_edges_s": [1.1e-9, 1.2e-9],
            "positive_v": 0.2,
            "negative_v": 0.7,
        }
    ]
