from pathlib import Path

import pytest

from sigilicon.flow.model import FlowExecutionError
from sigilicon.flow.synopsys_reports import parse_synopsys_fc_report_facts


FIXTURES = Path(__file__).parent / "fixtures" / "synopsys_fc"


def _write_report(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _physical_reports(
    tmp_path: Path,
    drc: Path,
    completion: Path | None = None,
    tieoff: Path | None = FIXTURES / "tieoff_complete.rpt",
) -> dict[str, Path]:
    reports = {
        "design-check-report": _write_report(
            tmp_path,
            "check_design.rpt",
            """
Total 12 non-EMS messages : 0 errors, 12 warnings
Total 29 EMS messages : 0 errors, 29 warnings
""",
        ),
        "qor-report": _write_report(
            tmp_path,
            "qor.rpt",
            """
Worst Hold Violation: -0.08
Max Trans Violations: 2
Max Cap Violations: 2
""",
        ),
        "timing-report": _write_report(
            tmp_path,
            "timing.rpt",
            "slack (VIOLATED) -0.04\n",
        ),
        "area-report": _write_report(
            tmp_path,
            "area.rpt",
            """
Total physical cell area: 387.590
TOTAL LEAF CELLS 418
""",
        ),
        "power-report": _write_report(
            tmp_path,
            "power.rpt",
            """
switching activity propagation in scalar mode!
Total Dynamic Power = 95500 nW
Cell Leakage Power = 386 nW
""",
        ),
        "drc-report": drc,
    }
    if completion is not None:
        reports["physical-completion-report"] = completion
    if tieoff is not None:
        reports["tie-off-check-report"] = tieoff
    return reports


def test_real_fc_report_distinguishes_missing_antenna_rules_and_tie_check(
    tmp_path: Path,
) -> None:
    facts = parse_synopsys_fc_report_facts(
        "asic.physical-implementation",
        _physical_reports(
            tmp_path,
            FIXTURES / "paper_0p8v_incomplete_drc.rpt",
            FIXTURES / "physical_completion_incomplete.rpt",
        ),
    )

    assert facts["antenna-check-active"] is False
    assert facts["antenna-check-status"] == "no-rules"
    assert "antenna-violation-count" not in facts
    assert facts["tie-to-rail-check-performed"] is False
    assert facts["tie-to-rail-check-status"] == "not-performed"
    assert "tie-to-rail-violation-count" not in facts
    assert "tie-to-rail-direct-violation-count" not in facts


def test_physical_completion_report_exposes_complete_pg_facts(
    tmp_path: Path,
) -> None:
    facts = parse_synopsys_fc_report_facts(
        "asic.physical-implementation",
        _physical_reports(
            tmp_path,
            FIXTURES / "route_checks_active.rpt",
            FIXTURES / "physical_completion_complete.rpt",
        ),
    )

    assert facts["required-pg-port-count"] == 2
    assert facts["placed-required-pg-port-count"] == 2
    assert facts["unplaced-required-pg-port-count"] == 0
    assert facts["pg-connectivity-check-performed"] is True
    assert facts["pg-connectivity-check-status"] == "performed"
    assert facts["pg-connectivity-violation-count"] == 0


def test_physical_completion_report_exposes_incomplete_pg_facts(
    tmp_path: Path,
) -> None:
    facts = parse_synopsys_fc_report_facts(
        "asic.physical-implementation",
        _physical_reports(
            tmp_path,
            FIXTURES / "paper_0p8v_incomplete_drc.rpt",
            FIXTURES / "physical_completion_incomplete.rpt",
        ),
    )

    assert facts["required-pg-port-count"] == 2
    assert facts["placed-required-pg-port-count"] == 0
    assert facts["unplaced-required-pg-port-count"] == 2
    assert facts["pg-connectivity-check-performed"] is True
    assert facts["pg-connectivity-violation-count"] == 6


def test_missing_physical_completion_report_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(
        FlowExecutionError,
        match="omitted 'physical-completion-report'",
    ):
        parse_synopsys_fc_report_facts(
            "asic.physical-implementation",
            _physical_reports(
                tmp_path,
                FIXTURES / "paper_0p8v_incomplete_drc.rpt",
            ),
        )


def test_malformed_physical_completion_report_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(
        FlowExecutionError,
        match="malformed Synopsys physical-completion report",
    ):
        parse_synopsys_fc_report_facts(
            "asic.physical-implementation",
            _physical_reports(
                tmp_path,
                FIXTURES / "route_checks_active.rpt",
                FIXTURES / "physical_completion_malformed.rpt",
            ),
        )


def test_pg_connectivity_not_performed_has_no_violation_count(
    tmp_path: Path,
) -> None:
    facts = parse_synopsys_fc_report_facts(
        "asic.physical-implementation",
        _physical_reports(
            tmp_path,
            FIXTURES / "route_checks_inactive.rpt",
            FIXTURES / "physical_completion_not_checked.rpt",
        ),
    )

    assert facts["pg-connectivity-check-performed"] is False
    assert facts["pg-connectivity-check-status"] == "not-performed"
    assert "pg-connectivity-violation-count" not in facts


@pytest.mark.parametrize(
    ("fixture", "active", "status", "violations"),
    [
        ("route_checks_active.rpt", True, "active", 3),
        ("route_checks_inactive.rpt", False, "inactive", None),
        ("paper_0p8v_incomplete_drc.rpt", False, "no-rules", None),
    ],
)
def test_antenna_check_states_remain_distinct(
    tmp_path: Path,
    fixture: str,
    active: bool,
    status: str,
    violations: int | None,
) -> None:
    facts = parse_synopsys_fc_report_facts(
        "asic.physical-implementation",
        _physical_reports(
            tmp_path,
            FIXTURES / fixture,
            FIXTURES / "physical_completion_complete.rpt",
        ),
    )

    assert facts["antenna-check-active"] is active
    assert facts["antenna-check-status"] == status
    if violations is None:
        assert "antenna-violation-count" not in facts
    else:
        assert facts["antenna-violation-count"] == violations


@pytest.mark.parametrize(
    ("fixture", "performed", "status", "violations", "direct"),
    [
        ("route_checks_active.rpt", True, "performed", 4, 1),
        ("paper_0p8v_incomplete_drc.rpt", False, "not-performed", None, None),
    ],
)
def test_tie_to_rail_check_states_remain_distinct(
    tmp_path: Path,
    fixture: str,
    performed: bool,
    status: str,
    violations: int | None,
    direct: int | None,
) -> None:
    facts = parse_synopsys_fc_report_facts(
        "asic.physical-implementation",
        _physical_reports(
            tmp_path,
            FIXTURES / fixture,
            FIXTURES / "physical_completion_complete.rpt",
        ),
    )

    assert facts["tie-to-rail-check-performed"] is performed
    assert facts["tie-to-rail-check-status"] == status
    if violations is None:
        assert "tie-to-rail-violation-count" not in facts
        assert "tie-to-rail-direct-violation-count" not in facts
    else:
        assert facts["tie-to-rail-violation-count"] == violations
        assert facts["tie-to-rail-direct-violation-count"] == direct


@pytest.mark.parametrize(
    ("fixture", "violations"),
    [("tieoff_complete.rpt", 0), ("tieoff_violations.rpt", 1)],
)
def test_tie_off_check_report_exposes_performed_facts(
    tmp_path: Path,
    fixture: str,
    violations: int,
) -> None:
    facts = parse_synopsys_fc_report_facts(
        "asic.physical-implementation",
        _physical_reports(
            tmp_path,
            FIXTURES / "paper_0p8v_incomplete_drc.rpt",
            FIXTURES / "physical_completion_complete.rpt",
            FIXTURES / fixture,
        ),
    )

    assert facts["tie-off-check-performed"] is True
    assert facts["tie-off-check-status"] == "performed"
    assert facts["tie-off-violation-count"] == violations


def test_missing_tie_off_check_report_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(FlowExecutionError, match="omitted 'tie-off-check-report'"):
        parse_synopsys_fc_report_facts(
            "asic.physical-implementation",
            _physical_reports(
                tmp_path,
                FIXTURES / "paper_0p8v_incomplete_drc.rpt",
                FIXTURES / "physical_completion_complete.rpt",
                None,
            ),
        )
