from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sigilicon.domain.oa_simulation import (
    OANativeLegacyMeasurement,
    OANativeLegacySeries,
)
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
            window_starts=("1n",),
            window_ends=("2n",),
            edge_signal="/VALID",
            edge_count=2,
            threshold_v=0.45,
        )
    )
    legacy = OANativeLegacyMeasurement(
        alias="legacy",
        sample_times=("1.5n",),
        series=(
            OANativeLegacySeries("protocol", ("/RDY",), "protocol"),
            OANativeLegacySeries("decisions", ("/D1", "/D0"), "decisions"),
            OANativeLegacySeries("analog", ("/VP", "/VN"), "analog"),
        ),
    )
    contract = SimpleNamespace(
        diagnostic_equivalence=diagnostic,
        legacy_measurement=legacy,
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
                "name": f"diag_valid_edge_000_{edge:02d}",
                "value": 1.1e-9 + edge * 0.1e-9,
            }
            for edge in range(2)
        ]
    }
    legacy_result = {
        "contexts": [
            {
                "point": 1,
                "corner": "tt",
                "test": "tran",
                "exports": {
                    "protocol": [0.9],
                    "decisions": [0.9, 0.0],
                    "analog": [0.2, 0.7],
                },
            }
        ]
    }

    decoded = evaluate_valid_edge_diagnostic(
        result,
        contract,
        legacy_result,
        binding=ValidEdgeResultBinding(
            protocol_export="protocol",
            decision_export="decisions",
            analog_export="analog",
            analog_output_keys=("positive_v", "negative_v"),
        ),
    )

    assert [name for name, _expression in diagnostic.scalar_outputs] == [
        "diag_valid_edge_000_00",
        "diag_valid_edge_000_01",
    ]
    assert 'VT("/VALID") 1n 2n' in diagnostic.scalar_outputs[0][1]
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


def test_valid_edge_module_does_not_own_hardware_product_schema() -> None:
    source = Path(__file__).resolve().parents[1] / (
        "src/sigilicon/domain/valid_edge_diagnostics.py"
    )
    text = source.read_text(encoding="utf-8")

    for product_term in (
        "active_buffers",
        "activation_masks_hex",
        "bounded_campaign",
        "code_mapping_contract",
        "comparator_mismatch_qualified",
        "product_qualification_conclusion",
    ):
        assert product_term not in text
