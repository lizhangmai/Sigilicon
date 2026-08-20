from __future__ import annotations

import math
from pathlib import Path

import pytest

from sigilicon.domain.oa_simulation import (
    OANativeDiagnosticContract,
    OANativeLegacyMeasurement,
    OANativeLegacySeries,
    OANativeRdbContract,
)
from sigilicon.virtuoso.maestro_rdb import (
    read_native_maestro_rdb_export,
    reconstruct_native_diagnostic,
    reconstruct_native_legacy_measurement,
)


def test_native_maestro_rdb_export_preserves_official_identities(tmp_path: Path) -> None:
    path = tmp_path / "rdb.tsv"
    path.write_text(
        """RDB_SCHEMA\t1
OUTPUT\t1\ttt_25c\ttran_generator\tvgc_sample_0\t0.6741\tundefined
OUTPUT\t1\ttt_25c\ttran_generator\tvgc_sample_1\t0.5632\tundefined
SUMMARY\t1\t2
OVERALL_SPEC\tnil
""",
        encoding="utf-8",
    )

    result = read_native_maestro_rdb_export(path)

    assert result["source"] == "Cadence maeReadResDB/point->params+point->outputs"
    assert result["outputs"][0]["corner"] == "tt_25c"
    assert result["outputs"][1]["name"] == "vgc_sample_1"
    assert result["outputs"][0]["value"] == pytest.approx(0.6741)
    assert result["identity"] == {
        "points": [
            {
                "point": 1,
                "corners": ["tt_25c"],
                "tests": ["tran_generator"],
                "outputs": ["vgc_sample_0", "vgc_sample_1"],
                "parameters": {},
            }
        ],
        "corners": ["tt_25c"],
        "tests": ["tran_generator"],
        "outputs": ["vgc_sample_0", "vgc_sample_1"],
    }


def test_native_maestro_rdb_export_rejects_missing_scalar_value(tmp_path: Path) -> None:
    path = tmp_path / "rdb.tsv"
    path.write_text(
        """RDB_SCHEMA\t1
OUTPUT\t1\ttt_25c\ttran_generator\tvgc_sample_0\tnil\tundefined
SUMMARY\t1\t1
OVERALL_SPEC\tnil
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no scalar value"):
        read_native_maestro_rdb_export(path)


def test_native_maestro_rdb_export_accepts_reviewed_nullable_output(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rdb.tsv"
    path.write_text(
        """RDB_SCHEMA\t1
OUTPUT\t1\ttt_25c\ttran_bank\tdiag_bank_post_ascending_boundary_00\tnil\tundefined
SUMMARY\t1\t1
OVERALL_SPEC\tnil
""",
        encoding="utf-8",
    )

    result = read_native_maestro_rdb_export(
        path,
        nullable_outputs=("diag_bank_post_ascending_boundary_00",),
    )

    assert result["outputs"][0]["value"] is None
    assert result["scalar_values_finite"] is False
    assert result["nullable_output_count"] == 1


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_native_maestro_rdb_export_rejects_nonfinite_scalar_value(
    tmp_path: Path, value: str
) -> None:
    path = tmp_path / "rdb.tsv"
    path.write_text(
        f"""RDB_SCHEMA\t1
OUTPUT\t1\ttt_25c\ttran_generator\tvgc_sample_0\t{value}\tundefined
SUMMARY\t1\t1
OVERALL_SPEC\tnil
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not finite"):
        read_native_maestro_rdb_export(path)


def test_native_maestro_rdb_export_can_check_an_independent_identity_model(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rdb.tsv"
    path.write_text(
        """RDB_SCHEMA\t1
OUTPUT\t1\ttt_25c\ttran_generator\tvgc_sample_0\t0.6741\tundefined
SUMMARY\t1\t1
OVERALL_SPEC\tnil
""",
        encoding="utf-8",
    )

    result = read_native_maestro_rdb_export(
        path,
        expected_point_count=1,
        expected_corners=("tt_25c",),
        expected_tests=("tran_generator",),
        expected_outputs=("vgc_sample_0",),
        expected_expression_count=1,
    )

    assert result["scalar_output_count"] == 1
    assert result["scalar_values_finite"] is True
    assert result["point_parameters"] == []


def test_native_maestro_rdb_export_preserves_point_parameters(tmp_path: Path) -> None:
    path = tmp_path / "rdb.tsv"
    path.write_text(
        """RDB_SCHEMA\t1
PARAM\t1\tBANK_SWEEP_DIRECTION\t-1
PARAM\t1\tmonteCarlo::param::sequence\t7
OUTPUT\t1\ttt_25c\ttran_bank\tboundary\t0.001\tpass
SUMMARY\t1\t1
OVERALL_SPEC\tpass
""",
        encoding="utf-8",
    )

    result = read_native_maestro_rdb_export(path)

    assert result["point_parameters"] == [
        {"point": 1, "name": "BANK_SWEEP_DIRECTION", "value": -1},
        {"point": 1, "name": "monteCarlo::param::sequence", "value": 7},
    ]
    assert result["identity"]["points"][0]["parameters"] == {
        "BANK_SWEEP_DIRECTION": -1,
        "monteCarlo::param::sequence": 7,
    }


def test_native_maestro_rdb_export_rejects_wrong_cartesian_count(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rdb.tsv"
    path.write_text(
        """RDB_SCHEMA\t1
OUTPUT\t1\ttt_25c\ttran_generator\tvgc_sample_0\t0.6741\tundefined
SUMMARY\t1\t1
OVERALL_SPEC\tnil
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="expression count"):
        read_native_maestro_rdb_export(
            path,
            expected_point_count=1,
            expected_corners=("tt_25c",),
            expected_tests=("tran_generator",),
            expected_outputs=("vgc_sample_0",),
            expected_expression_count=2,
        )


def test_native_rdb_reconstructs_legacy_sample_arrays() -> None:
    contract = OANativeRdbContract(
        path=Path("native_rdb.toml"),
        point_count=1,
        corners=("tt_25c",),
        tests=("tran_main",),
        waveform_outputs=(("out", "/OUT"),),
        scalar_outputs=(
            ("endpoint", 'value(VT("/OUT") 2n)'),
            ("legacy_out_000_00", 'value(VT("/OUT") 1n)'),
            ("legacy_out_001_00", 'value(VT("/OUT") 2n)'),
        ),
        legacy_measurement=OANativeLegacyMeasurement(
            alias="samples",
            sample_times=("1n", "2n"),
            series=(
                OANativeLegacySeries(
                    export="out_samples",
                    signals=("/OUT",),
                    prefix="legacy_out",
                ),
            ),
        ),
    )
    result = {
        "outputs": [
            {
                "point": 1,
                "corner": "tt_25c",
                "test": "tran_main",
                "name": "endpoint",
                "value": 0.3,
            },
            {
                "point": 1,
                "corner": "tt_25c",
                "test": "tran_main",
                "name": "legacy_out_000_00",
                "value": 0.1,
            },
            {
                "point": 1,
                "corner": "tt_25c",
                "test": "tran_main",
                "name": "legacy_out_001_00",
                "value": 0.2,
            },
        ]
    }

    legacy = reconstruct_native_legacy_measurement(result, contract)

    assert legacy is not None
    assert legacy["alias"] == "samples"
    assert legacy["contexts"][0]["exports"] == {
        "sample_times": ["1n", "2n"],
        "out_samples": [0.1, 0.2],
    }


def test_frontend_diagnostic_uses_dynamic_ready_without_fixed_samples() -> None:
    diagnostic = OANativeDiagnosticContract(
        kind="frontend_valid_edges",
        settings={
            "kind": "frontend_valid_edges",
            "scores": (1,),
            "warmup_scores": (0,),
            "edge_signal": "/VALID",
            "edge_count": 6,
            "threshold_v": 0.45,
            "ready_signal": "/RDY",
            "result_sample_delay": "20p",
            "result_signals": (
                "/D5", "/D4", "/D3", "/D2", "/D1", "/D0",
            ),
            "window_starts": ("1n", "2n"),
            "window_ends": ("2n", "3n"),
            "vcm_dac_by_corner": {"tt": 0.45},
            "vcm_adc_by_corner": {"tt": 0.45},
            "vcm_cal_by_corner": {"tt": 0.45},
            "exact_window": (-32, 31),
            "raw_domain": (-224, 224),
            "decisions": 6,
            "history_directions": ("ascending", "descending"),
            "diagnostic_equal_drive_values_v": (0.45,),
            "expected_codes": (32, 33),
            "code_mapping": {
                "kind": "affine_saturating",
                "score_multiplier": 1,
                "code_offset": 32,
                "minimum_code": 0,
                "maximum_code": 63,
            },
            "code_mapping_contract": (
                "ip/cim_compute/configs/architecture/behavioral_contract.toml"
            ),
        },
        scalar_outputs=(),
    )
    contract = OANativeRdbContract(
        path=Path("native_rdb.toml"),
        point_count=1,
        corners=("tt",),
        tests=("tran_frontend",),
        waveform_outputs=(),
        scalar_outputs=(),
        legacy_measurement=None,
        diagnostic_equivalence=diagnostic,
    )
    outputs = []
    for index, (code, ready_edge) in enumerate(((32, 1.7e-9), (33, 2.7e-9))):
        values = {
            **{
                f"/D{bit}": 0.9 if code & (1 << bit) else 0.0
                for bit in range(5, -1, -1)
            },
        }
        outputs.append({
            "point": 1, "corner": "tt", "test": "tran_frontend",
            "name": f"diag_ready_edge_{index:03d}", "value": ready_edge,
        })
        outputs.extend(
            {
                "point": 1, "corner": "tt", "test": "tran_frontend",
                "name": f"diag_result_{signal.strip('/').lower()}_{index:03d}",
                "value": value,
            }
            for signal, value in values.items()
        )
        for edge in range(6):
            outputs.append({
                "point": 1, "corner": "tt", "test": "tran_frontend",
                "name": f"diag_valid_edge_{index:03d}_{edge:02d}",
                "value": ready_edge - (6 - edge) * 50e-12,
            })
    result = reconstruct_native_diagnostic(
        {"outputs": outputs}, contract, None
    )

    assert result is not None
    assert result["contexts"][0]["passed"] is True
    row = result["contexts"][0]["rows"][1]
    assert row["code"] == 33
    assert row["rdy"] == 1
    assert row["conversion_time_s"] == pytest.approx(0.7e-9)
    assert row["legacy_fixed_sample"] is None
    assert result["result_sample_is_fixed_time"] is False
    assert result["legacy_fixed_samples_retained_for_equivalence"] is False
    assert result["analog_diagnostics_are_scalarized"] is False
    assert result["legacy_fixed_samples_are_primary_results"] is False


def test_bank_diagnostic_groups_decisions_and_transfers_by_path() -> None:
    diagnostic = OANativeDiagnosticContract(
        kind="bank_calibration_trajectory",
        settings={
            "kind": "bank_calibration_trajectory",
            "paths": 1,
            "decision_sample_times": tuple(f"{step + 1}n" for step in range(7)),
            "transfer_sample_times": tuple(
                f"{step + 1}.5n" for step in range(7)
            ),
            "threshold_v": 0.45,
        },
        scalar_outputs=(),
    )
    contract = OANativeRdbContract(
        path=Path("native_rdb.toml"),
        point_count=1,
        corners=("tt",),
        tests=("tran_bank",),
        waveform_outputs=(),
        scalar_outputs=(),
        diagnostic_equivalence=diagnostic,
    )
    outputs = []
    for step in range(7):
        differential = 0.2 if step % 2 == 0 else -0.2
        outputs.extend(
            (
                {
                    "point": 1,
                    "corner": "tt",
                    "test": "tran_bank",
                    "name": f"diag_bank_decision_diff_{step:03d}_00",
                    "value": differential,
                },
                {
                    "point": 1,
                    "corner": "tt",
                    "test": "tran_bank",
                    "name": f"diag_bank_decision_onehot_{step:03d}_00",
                    "value": -0.2,
                },
                {
                    "point": 1,
                    "corner": "tt",
                    "test": "tran_bank",
                    "name": f"diag_bank_vcal_diff_{step:03d}_00",
                    "value": step * 0.01,
                },
            )
        )

    result = reconstruct_native_diagnostic(
        {"outputs": outputs}, contract, None
    )

    assert result is not None
    assert result["format"] == "native_rdb_bank_calibration_trajectory"
    path = result["contexts"][0]["paths"][0]
    assert [row["decision"] for row in path["decisions"]] == [1, 0, 1, 0, 1, 0, 1]
    assert path["all_decisions_one_hot"] is True
    assert path["transfers"][-1]["vcal_differential_v"] == pytest.approx(0.06)
    assert result["raw_offset_measured"] is False
    assert result["post_calibration_residual_measured"] is False


def test_bank_measurement_reads_in_point_boundaries_by_mc_sequence() -> None:
    settings = {
        "kind": "bank_calibration_measurement",
        "paths": 1,
        "monte_carlo_samples": 1,
        "model_file": "mismatch_models.scs",
        "model_sections": ("local_mos", "local_mom"),
        "boundary_directions": ("ascending", "descending"),
        "supply_v": 0.9,
        "threshold_v": 0.45,
        "raw_half_span_v": 0.001,
        "post_half_span_v": 0.0005,
        "retention_half_span_v": 0.0005,
        "raw_sweep_min_v": -0.002,
        "raw_sweep_max_v": 0.002,
        "raw_sweep_step_v": 0.002,
        "raw_ascending_start": "1n",
        "raw_ascending_stop": "3n",
        "raw_descending_start": "4n",
        "raw_descending_stop": "6n",
        "raw_sample_period": "1n",
        "post_sweep_min_v": -0.001,
        "post_sweep_max_v": 0.001,
        "post_sweep_step_v": 0.001,
        "post_ascending_start": "7n",
        "post_ascending_stop": "9n",
        "post_descending_start": "10n",
        "post_descending_stop": "12n",
        "post_sample_period": "1n",
        "retention_hold_time_s": 1.0e-3,
        "retention_hold_start": "12n",
        "retention_hold_sample": "1.000012m",
        "retention_sweep_min_v": -0.001,
        "retention_sweep_max_v": 0.001,
        "retention_sweep_step_v": 0.001,
        "retention_ascending_start": "1.000013m",
        "retention_ascending_stop": "1.000015m",
        "retention_descending_start": "1.000016m",
        "retention_descending_stop": "1.000018m",
        "retention_sample_period": "1n",
        "decision_sample_times": tuple(f"{step + 7}n" for step in range(7)),
        "transfer_sample_times": tuple(f"{step + 7}.5n" for step in range(7)),
    }
    diagnostic = OANativeDiagnosticContract(
        kind="bank_calibration_measurement",
        settings=settings,
        scalar_outputs=(),
    )
    contract = OANativeRdbContract(
        path=Path("native_rdb.toml"),
        point_count=1,
        corners=("tt",),
        tests=("tran_bank",),
        waveform_outputs=(),
        scalar_outputs=(),
        diagnostic_equivalence=diagnostic,
    )
    outputs = []
    point = 1
    for phase, boundaries in (
        ("raw", {"ascending": 0.001, "descending": 0.001}),
        ("post", {"ascending": 0.0, "descending": 0.0}),
        ("retention", {"ascending": 0.0002, "descending": 0.0002}),
    ):
        for direction, boundary in boundaries.items():
            outputs.extend(
                (
                    {
                        "point": point, "corner": "tt", "test": "tran_bank",
                        "name": f"diag_bank_{phase}_{direction}_boundary_00",
                        "value": boundary,
                    },
                    {
                        "point": point, "corner": "tt", "test": "tran_bank",
                        "name": f"diag_bank_{phase}_{direction}_onehot_00",
                        "value": -0.2,
                    },
                )
            )
    for step in range(7):
        outputs.extend(
            (
                {
                    "point": point, "corner": "tt", "test": "tran_bank",
                    "name": f"diag_bank_decision_diff_{step:03d}_00",
                    "value": 0.4 if step % 2 == 0 else -0.4,
                },
                {
                    "point": point, "corner": "tt", "test": "tran_bank",
                    "name": f"diag_bank_decision_onehot_{step:03d}_00",
                    "value": -0.2,
                },
                {
                    "point": point, "corner": "tt", "test": "tran_bank",
                    "name": f"diag_bank_vcal_diff_{step:03d}_00",
                    "value": step * 0.01,
                },
                {
                    "point": point, "corner": "tt", "test": "tran_bank",
                    "name": f"bank_vgc_weight_{step:03d}",
                    "value": 0.08 / (1 << step),
                },
            )
        )
    outputs.extend(
        {
            "point": point, "corner": "tt", "test": "tran_bank",
            "name": name, "value": value,
        }
        for name, value in {
            "bank_hold_vcalp_0": 0.4,
            "bank_hold_vcaln_0": 0.385,
            "bank_retention_vcalp_0": 0.399,
            "bank_retention_vcaln_0": 0.385,
            "bank_normal_positive_outp_0": 0.8,
            "bank_normal_positive_outn_0": 0.1,
            "bank_normal_negative_outp_0": 0.1,
            "bank_normal_negative_outn_0": 0.8,
        }.items()
    )
    result = reconstruct_native_diagnostic(
        {
            "outputs": outputs,
            "point_parameters": [
                {
                    "point": 1,
                    "name": "monteCarlo::param::sequence",
                    "value": 1,
                },
                {
                    "point": 1,
                    "name": "corModelSpec",
                    "value": "((mismatch_models.scs local_mos) "
                    "(mismatch_models.scs local_mom))",
                },
            ],
        },
        contract,
        None,
    )

    assert result is not None
    assert result["format"] == "native_rdb_bank_calibration_measurement"
    path = result["contexts"][0]["paths"][0]
    assert path["raw_offset_v"] == pytest.approx(-0.001)
    assert path["post_calibration_residual_v"] == pytest.approx(0.0)
    assert path["retention_residual_v"] == pytest.approx(-0.0002)
    assert path["retention_hold_rail_legal"] is True
    assert path["signed_calibration_correction_v"] == pytest.approx(-0.001)
    assert result["contexts"][0]["shared_vgc_weight_trajectory"][-1][
        "vgc_minus_vcm_cal_v"
    ] == pytest.approx(0.00125)
    assert result["all_boundary_measurements_complete"] is True
    assert result["all_retention_measurements_complete"] is True
    assert result["raw_offset_statistics"]["count"] == 1
    finite_gain = result["finite_gain_model"]
    assert finite_gain["phi1"]["fit_available"] is True
    assert finite_gain["phi1"]["sample_count"] == 4
    assert finite_gain["phi2"]["fit_available"] is True
    assert finite_gain["phi2"]["sample_count"] == 3
    assert math.isfinite(finite_gain["phi1"]["alpha"])
    assert math.isfinite(finite_gain["phi1"]["beta"])
    assert math.isfinite(finite_gain["phi1"]["epsilon_v"])
    assert result["transfer_direction_diagnostics"][0]["sample_count"] == 1
    assert result["product_qualification_conclusion"] is False


def test_bank_measurement_reports_missing_directed_boundary() -> None:
    settings = {
        "kind": "bank_calibration_measurement",
        "paths": 1,
        "monte_carlo_samples": 1,
        "model_file": "mismatch_models.scs",
        "model_sections": ("local_mos", "local_mom"),
        "boundary_directions": ("ascending", "descending"),
        "supply_v": 0.9,
        "threshold_v": 0.45,
        "raw_half_span_v": 0.001,
        "post_half_span_v": 0.0005,
        "retention_half_span_v": 0.0005,
        "raw_sweep_min_v": -0.002,
        "raw_sweep_max_v": 0.002,
        "raw_sweep_step_v": 0.002,
        "raw_ascending_start": "1n",
        "raw_ascending_stop": "3n",
        "raw_descending_start": "4n",
        "raw_descending_stop": "6n",
        "raw_sample_period": "1n",
        "post_sweep_min_v": -0.001,
        "post_sweep_max_v": 0.001,
        "post_sweep_step_v": 0.001,
        "post_ascending_start": "7n",
        "post_ascending_stop": "9n",
        "post_descending_start": "10n",
        "post_descending_stop": "12n",
        "post_sample_period": "1n",
        "retention_hold_time_s": 1.0e-3,
        "retention_hold_start": "12n",
        "retention_hold_sample": "1.000012m",
        "retention_sweep_min_v": -0.001,
        "retention_sweep_max_v": 0.001,
        "retention_sweep_step_v": 0.001,
        "retention_ascending_start": "1.000013m",
        "retention_ascending_stop": "1.000015m",
        "retention_descending_start": "1.000016m",
        "retention_descending_stop": "1.000018m",
        "retention_sample_period": "1n",
        "decision_sample_times": tuple(f"{step + 7}n" for step in range(7)),
        "transfer_sample_times": tuple(f"{step + 7}.5n" for step in range(7)),
    }
    diagnostic = OANativeDiagnosticContract(
        kind="bank_calibration_measurement",
        settings=settings,
        scalar_outputs=(),
    )
    contract = OANativeRdbContract(
        path=Path("native_rdb.toml"),
        point_count=1,
        corners=("tt",),
        tests=("tran_bank",),
        waveform_outputs=(),
        scalar_outputs=(),
        diagnostic_equivalence=diagnostic,
    )
    outputs = []
    for phase in ("raw", "post", "retention"):
        for direction in ("ascending", "descending"):
            outputs.extend(
                (
                    {
                        "point": 1,
                        "corner": "tt",
                        "test": "tran_bank",
                        "name": f"diag_bank_{phase}_{direction}_boundary_00",
                        "value": (
                            None if phase == "post" else 0.0
                        ),
                    },
                    {
                        "point": 1,
                        "corner": "tt",
                        "test": "tran_bank",
                        "name": f"diag_bank_{phase}_{direction}_onehot_00",
                        "value": -0.2,
                    },
                )
            )
    for step in range(7):
        for name, value in (
            (f"diag_bank_decision_diff_{step:03d}_00", -0.8),
            (f"diag_bank_decision_onehot_{step:03d}_00", -0.2),
            (f"diag_bank_vcal_diff_{step:03d}_00", -0.1),
        ):
            outputs.append(
                {
                    "point": 1,
                    "corner": "tt",
                    "test": "tran_bank",
                    "name": name,
                    "value": value,
                }
            )
    for name, value in {
        "bank_hold_vcalp_0": 0.2,
        "bank_hold_vcaln_0": 0.385,
        "bank_retention_vcalp_0": 0.2,
        "bank_retention_vcaln_0": 0.385,
        "bank_normal_positive_outp_0": 0.1,
        "bank_normal_positive_outn_0": 0.8,
        "bank_normal_negative_outp_0": 0.1,
        "bank_normal_negative_outn_0": 0.8,
    }.items():
        outputs.append(
            {
                "point": 1,
                "corner": "tt",
                "test": "tran_bank",
                "name": name,
                "value": value,
            }
        )

    result = reconstruct_native_diagnostic(
        {
            "outputs": outputs,
            "point_parameters": [
                {
                    "point": 1,
                    "name": "monteCarlo::param::sequence",
                    "value": 1,
                },
                {
                    "point": 1,
                    "name": "corModelSpec",
                    "value": "((mismatch_models.scs local_mos) "
                    "(mismatch_models.scs local_mom))",
                },
            ],
        },
        contract,
        None,
    )

    assert result is not None
    path = result["contexts"][0]["paths"][0]
    assert path["post_calibration_residual_v"] is None
    assert path["post"]["ascending"]["directed_transition_found"] is False
    assert result["post_residual_statistics"]["count"] == 0
    assert result["finite_gain_model"]["phi1"]["fit_available"] is False
    assert result["all_boundary_measurements_complete"] is False
    assert result["all_retention_measurements_complete"] is True
