"""Portable facts parsed from Synopsys Library Manager and FC reports."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Mapping

from sigilicon.flow.model import FlowExecutionError


_MESSAGE = {
    "error": re.compile(r"^Error:", re.MULTILINE),
    "warning": re.compile(r"^Warning:", re.MULTILINE),
}
_WORKSPACE_COMPLETION = re.compile(
    r"^Workspace check (succeeded|failed)!\s*$",
    re.MULTILINE,
)
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_DESIGN_SUMMARY = re.compile(
    r"^\s*Total\s+\d+\s+(?P<kind>non-EMS|EMS) messages\s*:\s*"
    r"(?P<errors>\d+)\s+errors?,\s*"
    r"(?P<warnings>\d+)\s+warnings?",
    re.MULTILINE,
)


def _required_numbers(text: str, pattern: str, label: str) -> list[float]:
    values = [float(value) for value in re.findall(pattern, text, re.MULTILINE)]
    if not values:
        raise FlowExecutionError(
            f"malformed Synopsys {label} report: required observation is missing"
        )
    return values


def _required_int(text: str, pattern: str, label: str) -> int:
    values = [int(value) for value in re.findall(pattern, text, re.MULTILINE)]
    if not values:
        raise FlowExecutionError(
            f"malformed Synopsys {label} report: required observation is missing"
        )
    if len(set(values)) != 1:
        raise FlowExecutionError(
            f"malformed Synopsys {label} report: conflicting observations"
        )
    return values[0]


def _required_summary_value(text: str, label: str) -> str:
    values = [
        value.strip()
        for value in re.findall(
            rf"^\s*{re.escape(label)}\s*=\s*([^\r\n]+?)\s*$",
            text,
            re.MULTILINE,
        )
    ]
    if not values:
        raise FlowExecutionError(
            "malformed Synopsys DRC report: "
            f"{label!r} observation is missing"
        )
    if len(set(values)) != 1:
        raise FlowExecutionError(
            "malformed Synopsys DRC report: "
            f"{label!r} observations conflict"
        )
    return values[0]


def _route_verification_facts(
    drc: str,
) -> dict[str, bool | int | str]:
    antenna = _required_summary_value(
        drc,
        "Total number of antenna violations",
    )
    facts: dict[str, bool | int | str]
    if antenna.isdigit():
        facts = {
            "antenna-check-active": True,
            "antenna-check-status": "active",
            "antenna-violation-count": int(antenna),
        }
    elif antenna == "no antenna rules defined":
        facts = {
            "antenna-check-active": False,
            "antenna-check-status": "no-rules",
        }
    elif antenna == "antenna checking not active":
        facts = {
            "antenna-check-active": False,
            "antenna-check-status": "inactive",
        }
    else:
        raise FlowExecutionError(
            "malformed Synopsys DRC report: unsupported antenna status "
            f"{antenna!r}"
        )

    tie = _required_summary_value(
        drc,
        "Total number of tie to rail violations",
    )
    tie_direct = _required_summary_value(
        drc,
        "Total number of tie to rail directly violations",
    )
    if tie.isdigit() and tie_direct.isdigit():
        facts.update(
            {
                "tie-to-rail-check-performed": True,
                "tie-to-rail-check-status": "performed",
                "tie-to-rail-violation-count": int(tie),
                "tie-to-rail-direct-violation-count": int(tie_direct),
            }
        )
    elif tie == "not checked" and tie_direct == "not checked":
        facts.update(
            {
                "tie-to-rail-check-performed": False,
                "tie-to-rail-check-status": "not-performed",
            }
        )
    else:
        raise FlowExecutionError(
            "malformed Synopsys DRC report: tie-to-rail summary is "
            f"inconsistent ({tie!r}, {tie_direct!r})"
        )
    return facts


def _physical_completion_facts(
    report: str,
) -> dict[str, bool | int | str]:
    if len(
        re.findall(
            r"^SIGILICON_PHYSICAL_COMPLETION_REPORT 1\s*$",
            report,
            re.MULTILINE,
        )
    ) != 1:
        raise FlowExecutionError(
            "malformed Synopsys physical-completion report: "
            "version marker is missing or duplicated"
        )

    required = _required_int(
        report,
        r"^Required PG ports\s*=\s*(\d+)\s*$",
        "physical-completion",
    )
    placed = _required_int(
        report,
        r"^Placed required PG ports\s*=\s*(\d+)\s*$",
        "physical-completion",
    )
    unplaced = _required_int(
        report,
        r"^Unplaced required PG ports\s*=\s*(\d+)\s*$",
        "physical-completion",
    )
    if placed + unplaced != required:
        raise FlowExecutionError(
            "malformed Synopsys physical-completion report: "
            "placed and unplaced PG port counts do not equal the required count"
        )

    checks = re.findall(
        r"^PG connectivity check\s*=\s*([^\r\n]+?)\s*$",
        report,
        re.MULTILINE,
    )
    if len(checks) != 1 or checks[0] not in {"performed", "not-performed"}:
        raise FlowExecutionError(
            "malformed Synopsys physical-completion report: "
            "PG connectivity check status is missing or unsupported"
        )

    facts: dict[str, bool | int | str] = {
        "required-pg-port-count": required,
        "placed-required-pg-port-count": placed,
        "unplaced-required-pg-port-count": unplaced,
        "pg-connectivity-check-performed": checks[0] == "performed",
        "pg-connectivity-check-status": checks[0],
    }
    violation_values = re.findall(
        r"^PG connectivity violations\s*=\s*(\d+)\s*$",
        report,
        re.MULTILINE,
    )
    if checks[0] == "performed":
        if len(violation_values) != 1:
            raise FlowExecutionError(
                "malformed Synopsys physical-completion report: "
                "performed PG connectivity check lacks one violation count"
            )
        facts["pg-connectivity-violation-count"] = int(violation_values[0])
    elif violation_values:
        raise FlowExecutionError(
            "malformed Synopsys physical-completion report: "
            "a non-performed PG connectivity check has a violation count"
        )
    return facts


def _tie_off_facts(report: str) -> dict[str, bool | int | str]:
    headers = re.findall(
        r"^Report\s*:\s*check_mv_design\s*$",
        report,
        re.MULTILINE,
    )
    modes = re.findall(r"^\s*-tieoff\s*$", report, re.MULTILINE)
    summaries = re.findall(
        r"^Information:\s*Total\s+(\d+)\s+error\(s\)\s+and\s+"
        r"(\d+)\s+warning\(s\)\s+from\s+check_mv_design\.\s*"
        r"\(MV-082\)\s*$",
        report,
        re.MULTILINE,
    )
    if len(headers) != 1 or len(modes) != 1 or len(summaries) != 1:
        raise FlowExecutionError(
            "malformed Synopsys tie-off check report: "
            "check identity or completion summary is missing or duplicated"
        )
    errors, warnings = (int(value) for value in summaries[0])
    return {
        "tie-off-check-performed": True,
        "tie-off-check-status": "performed",
        "tie-off-violation-count": errors + warnings,
    }


def _power_nw(text: str, label: str) -> float:
    match = re.search(
        rf"^\s*{re.escape(label)}\s*=\s*({_NUMBER})\s*"
        r"(W|mW|uW|nW|pW)\b",
        text,
        re.MULTILINE,
    )
    if match is None:
        raise FlowExecutionError(
            "malformed Synopsys power report: "
            f"{label!r} observation is missing"
        )
    scale = {
        "W": 1.0e9,
        "mW": 1.0e6,
        "uW": 1.0e3,
        "nW": 1.0,
        "pW": 1.0e-3,
    }
    return float(match.group(1)) * scale[match.group(2)]


def _report_text(reports: Mapping[str, Path], role: str) -> str:
    try:
        path = reports[role]
    except KeyError as exc:
        raise FlowExecutionError(
            f"Synopsys FC report set omitted {role!r}"
        ) from exc
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise FlowExecutionError(
            f"cannot parse Synopsys FC report {role!r}: {exc}"
        ) from exc


def parse_synopsys_fc_report_facts(
    action_kind: str,
    reports: Mapping[str, Path],
) -> dict[str, bool | int | float | str]:
    """Parse vendor observations without applying an owner acceptance policy."""

    if action_kind == "asic.reference-library-construction":
        report = _report_text(reports, "library-check-report")
        completions = _WORKSPACE_COMPLETION.findall(report)
        if not completions:
            raise FlowExecutionError(
                "malformed Synopsys library-check report: "
                "workspace completion marker is missing"
            )
        return {
            "library-check-succeeded": all(
                status == "succeeded" for status in completions
            ),
            "library-check-error-count": len(_MESSAGE["error"].findall(report)),
            "library-check-warning-count": len(
                _MESSAGE["warning"].findall(report)
            ),
        }
    if action_kind == "asic.physical-implementation":
        design = _report_text(reports, "design-check-report")
        summaries = list(_DESIGN_SUMMARY.finditer(design))
        summary_kinds = [match.group("kind") for match in summaries]
        if sorted(summary_kinds) != ["EMS", "non-EMS"]:
            raise FlowExecutionError(
                "malformed Synopsys design-check report: "
                "EMS/non-EMS message summary is missing or duplicated"
            )

        qor = _report_text(reports, "qor-report")
        hold_slacks = _required_numbers(
            qor,
            rf"^\s*Worst Hold Violation:\s*({_NUMBER})\s*$",
            "QoR",
        )
        max_transition = _required_int(
            qor,
            r"^\s*Max Trans Violations:\s*(\d+)\s*$",
            "QoR",
        )
        max_capacitance = _required_int(
            qor,
            r"^\s*Max Cap Violations:\s*(\d+)\s*$",
            "QoR",
        )

        timing = _report_text(reports, "timing-report")
        setup_slacks = _required_numbers(
            timing,
            rf"^\s*slack\s+\((?:MET|VIOLATED)\)\s+({_NUMBER})\s*$",
            "timing",
        )

        area = _report_text(reports, "area-report")
        physical_area = _required_numbers(
            area,
            rf"^\s*Total physical cell area:\s*({_NUMBER})\s*$",
            "area",
        )
        leaf_cells = _required_int(
            area,
            r"^\s*TOTAL LEAF CELLS\s+(\d+)\b",
            "area",
        )

        power = _report_text(reports, "power-report")
        activity = re.search(
            r"switching activity propagation in\s+([A-Za-z0-9_-]+)\s+mode!",
            power,
        )
        if activity is None:
            raise FlowExecutionError(
                "malformed Synopsys power report: activity mode is missing"
            )

        drc = _report_text(reports, "drc-report")
        physical_completion = _report_text(
            reports,
            "physical-completion-report",
        )
        tie_off = _report_text(reports, "tie-off-check-report")
        open_nets = _required_int(
            drc,
            r"^\s*Total number of open nets\s*=\s*(\d+)\b",
            "DRC",
        )
        route_drc = _required_int(
            drc,
            r"^\s*@+\s*TOTAL VIOLATIONS\s*=\s*(\d+)\s*$",
            "DRC",
        )

        facts: dict[str, bool | int | float | str] = {
            "design-check-error-count": sum(
                int(match.group("errors")) for match in summaries
            ),
            "design-check-warning-count": sum(
                int(match.group("warnings")) for match in summaries
            ),
            "open-net-count": open_nets,
            "route-drc-violation-count": route_drc,
            "worst-setup-slack-ns": min(setup_slacks),
            "worst-hold-slack-ns": min(hold_slacks),
            "max-transition-violation-count": max_transition,
            "max-capacitance-violation-count": max_capacitance,
            "physical-cell-area-um2": physical_area[0],
            "leaf-cell-count": leaf_cells,
            "power-activity-mode": activity.group(1),
            "total-dynamic-power-nw": _power_nw(power, "Total Dynamic Power"),
            "cell-leakage-power-nw": _power_nw(power, "Cell Leakage Power"),
        }
        facts.update(_route_verification_facts(drc))
        facts.update(_physical_completion_facts(physical_completion))
        facts.update(_tie_off_facts(tie_off))
        return facts
    raise FlowExecutionError(
        f"unsupported Synopsys FC report action: {action_kind!r}"
    )


__all__ = ["parse_synopsys_fc_report_facts"]
