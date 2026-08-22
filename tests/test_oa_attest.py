from __future__ import annotations

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


def test_simulation_work_retention_is_explicit() -> None:
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
            "--keep-work",
        ]
    )

    assert args.keep_work is True


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
    plan = type("Plan", (), {
        "library": "fixture_lib",
        "testbenches": (type("Step", (), {"cell": "tb_main"})(),),
    })()
    payload = {
        "passed": True,
        "library": "fixture_lib",
        "testbench": "tb_main",
        "source_fingerprint": "a" * 64,
        "semantic_fingerprint": "b" * 64,
        "simulation_run": False,
        "product_qualification_conclusion": False,
    }
    monkeypatch.setattr(
        flow_cli, "plan_oa_library_rebuild", lambda *_args, **_kwargs: plan
    )
    monkeypatch.setattr(
        flow_cli,
        "attest_oa_testbench",
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
