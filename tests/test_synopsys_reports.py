from pathlib import Path

import pytest

from sigilicon.flow.model import FlowExecutionError
from sigilicon.workflows.synopsys_reports import parse_synopsys_fc_report_facts


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


@pytest.mark.parametrize(
    ("drc", "completion", "antenna", "pg_status", "pg_violations"),
    [
        (
            "route_checks_active.rpt",
            "physical_completion_complete.rpt",
            "active",
            "performed",
            0,
        ),
        (
            "route_checks_inactive.rpt",
            "physical_completion_not_checked.rpt",
            "inactive",
            "not-performed",
            None,
        ),
        (
            "incomplete_drc.rpt",
            "physical_completion_incomplete.rpt",
            "no-rules",
            "performed",
            6,
        ),
    ],
)
def test_fc_report_states_capture_completion_and_checks(
    tmp_path: Path,
    drc: str,
    completion: str,
    antenna: str,
    pg_status: str,
    pg_violations: int | None,
) -> None:
    facts = parse_synopsys_fc_report_facts(
        "asic.physical-implementation",
        _physical_reports(
            tmp_path,
            FIXTURES / drc,
            FIXTURES / completion,
        ),
    )

    assert facts["antenna-check-active"] is (antenna == "active")
    assert facts["antenna-check-status"] == antenna
    assert facts["pg-connectivity-check-status"] == pg_status
    if pg_violations is None:
        assert facts["pg-connectivity-check-performed"] is False
        assert "pg-connectivity-violation-count" not in facts
    else:
        assert facts["pg-connectivity-check-performed"] is True
        assert facts["pg-connectivity-violation-count"] == pg_violations
    if antenna != "active":
        assert "antenna-violation-count" not in facts
    else:
        assert facts["antenna-violation-count"] == 3


@pytest.mark.parametrize(
    ("drc", "status", "violations", "direct"),
    [
        ("route_checks_active.rpt", "performed", 4, 1),
        ("incomplete_drc.rpt", "not-performed", None, None),
    ],
)
def test_fc_report_tie_to_rail_states_remain_distinct(
    tmp_path: Path,
    drc: str,
    status: str,
    violations: int | None,
    direct: int | None,
) -> None:
    facts = parse_synopsys_fc_report_facts(
        "asic.physical-implementation",
        _physical_reports(
            tmp_path,
            FIXTURES / drc,
            FIXTURES / "physical_completion_complete.rpt",
        ),
    )

    assert facts["tie-to-rail-check-performed"] is (status == "performed")
    assert facts["tie-to-rail-check-status"] == status
    if violations is None:
        assert "tie-to-rail-violation-count" not in facts
        assert "tie-to-rail-direct-violation-count" not in facts
    else:
        assert facts["tie-to-rail-violation-count"] == violations
        assert facts["tie-to-rail-direct-violation-count"] == direct


@pytest.mark.parametrize(
    ("completion", "message"),
    (
        (None, "omitted 'physical-completion-report'"),
        (
            "physical_completion_malformed.rpt",
            "malformed Synopsys physical-completion report",
        ),
    ),
)
def test_fc_report_rejects_missing_or_malformed_completion(
    tmp_path: Path,
    completion: str | None,
    message: str,
) -> None:
    with pytest.raises(FlowExecutionError, match=message):
        parse_synopsys_fc_report_facts(
            "asic.physical-implementation",
            _physical_reports(
                tmp_path,
                FIXTURES / "route_checks_active.rpt",
                None if completion is None else FIXTURES / completion,
            ),
        )
