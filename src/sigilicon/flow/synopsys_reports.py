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
            "tool-execution-completed": True,
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

        return {
            "tool-execution-completed": True,
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
    raise FlowExecutionError(
        f"unsupported Synopsys FC report action: {action_kind!r}"
    )


__all__ = ["parse_synopsys_fc_report_facts"]
