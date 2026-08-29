from __future__ import annotations

import pytest

from sigilicon.cli import flow as flow_cli
from sigilicon.cli.flow import _parser


def test_attest_is_a_current_testbench_setup_check() -> None:
    args = _parser().parse_args(
        [
            "oa",
            "attest",
            "--manifest",
            "ip/fixture_block/configs/oa.toml",
            "--library",
            "fixture_lib",
            "--testbench",
            "tb_main",
        ]
    )

    assert args.action == "attest"
    assert args.testbench == "tb_main"
    assert args.timeout == 300


def test_simulation_has_no_temporary_work_retention_option() -> None:
    args = _parser().parse_args(
        [
            "oa",
            "simulate",
            "--manifest",
            "ip/fixture_block/configs/oa.toml",
            "--library",
            "fixture_lib",
            "--testbench",
            "tb_main",
        ]
    )

    assert not hasattr(args, "keep_work")
    with pytest.raises(SystemExit):
        _parser().parse_args(
            [
                "oa",
                "simulate",
                "--manifest",
                "ip/fixture_block/configs/oa.toml",
                "--testbench",
                "tb_main",
                "--keep-work",
            ]
        )


def test_rebuild_can_select_exactly_one_design_cell() -> None:
    args = _parser().parse_args(
        [
            "oa",
            "rebuild",
            "--manifest",
            "ip/fixture_block/configs/oa.toml",
            "--library",
            "fixture_lib",
            "--cell",
            "FIXTURE_CELL",
        ]
    )

    assert args.cell == "FIXTURE_CELL"
    assert args.testbench is None


def test_cli_attest_reports_current_check_without_prior_state(
    monkeypatch, capsys, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    payload = {
        "passed": True,
        "library": "fixture_lib",
        "testbench": "tb_main",
        "simulation_run": False,
        "product_qualification_conclusion": False,
    }
    monkeypatch.setattr(
        flow_cli.ProjectOaWorkflow,
        "attest",
        lambda *_args, **_kwargs: payload,
    )

    assert (
        flow_cli.main(
            [
                "oa",
                "attest",
                "--manifest",
                "ip/fixture_block/configs/oa.toml",
                "--library",
                "fixture_lib",
                "--testbench",
                "tb_main",
            ],
            client_factory=lambda: object(),
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "OA setup check passed: fixture_lib/tb_main" in output
    assert "manifest=" not in output
