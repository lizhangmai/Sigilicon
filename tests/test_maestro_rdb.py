from __future__ import annotations

from pathlib import Path

import pytest

from sigilicon.virtuoso.maestro_rdb import read_native_maestro_rdb_export


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


def test_native_maestro_rdb_export_accepts_explicit_waveform_only_contract(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rdb.tsv"
    path.write_text(
        """RDB_SCHEMA\t1
PARAM\t1\tcorModelSpec\t\"tt_25c\"
PARAM\t1\ttemperature\t25
SUMMARY\t1\t0
OVERALL_SPEC\t((\"overAll\" t))
""",
        encoding="utf-8",
    )

    result = read_native_maestro_rdb_export(
        path,
        expected_point_count=1,
        expected_corners=("tt_25c",),
        expected_tests=("tran_truth_table",),
        expected_outputs=(),
        expected_expression_count=0,
    )

    assert result["expression_count"] == 0
    assert result["scalar_output_count"] == 0
    assert result["outputs"] == []
    assert result["identity"]["points"] == [
        {
            "point": 1,
            "corners": [],
            "tests": [],
            "outputs": [],
            "parameters": {"corModelSpec": "tt_25c", "temperature": 25},
        }
    ]


def test_native_maestro_rdb_export_rejects_unexpected_empty_scalar_results(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rdb.tsv"
    path.write_text(
        """RDB_SCHEMA\t1
PARAM\t1\ttemperature\t25
SUMMARY\t1\t0
OVERALL_SPEC\t((\"overAll\" t))
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no scalar expression outputs"):
        read_native_maestro_rdb_export(path)


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
