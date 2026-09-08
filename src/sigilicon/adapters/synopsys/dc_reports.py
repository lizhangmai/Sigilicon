"""Native DC observations, with design-supplied timing coverage and acceptance.

Missing, duplicate, non-finite and rounded-away violation evidence fails closed.
Clock names, periods, macro counts and qualification policy are never inferred.
"""
from dataclasses import dataclass
from fnmatch import fnmatchcase
import math
import re


@dataclass(frozen=True)
class TimingCoverage:
    clock: str
    period_ns: float
    path_groups: tuple[tuple[str, int], ...]


def timing_evidence(report: str, coverage: TimingCoverage):
    fields = {
        "clock_period_ns": "Critical Path Clk Period",
        "setup_wns_ns": "Critical Path Slack",
        "setup_tns_ns": "Total Negative Slack",
        "setup_violating_paths": "No. of Violating Paths",
        "hold_wns_ns": "Worst Hold Violation",
        "hold_tns_ns": "Total Hold Violation",
        "hold_violating_paths": "No. of Hold Violations",
    }
    sections = re.split(r"Timing Path Group '([^']+)'", report)
    groups = {}
    complete = bool(sections[1:])
    for name, section in zip(sections[1::2], sections[2::2]):
        values = {}
        for key, label in fields.items():
            found = re.findall(r"^\s*" + re.escape(label) + r":\s*([-+\d.eE]+)\s*$", section, re.M)
            try:
                value = float(found[0]) if len(found) == 1 else float("nan")
            except ValueError:
                value = float("nan")
            if (not math.isfinite(value)
                    or (key.endswith("_paths") and (value < 0 or not value.is_integer()))
                    or (key == "clock_period_ns" and value <= 0)):
                complete = False
                continue
            values[key] = value
        if name in groups:
            complete = False
        groups[name] = values
    complete = complete and coverage.clock in groups and all(
        sum(fnmatchcase(name, pattern) for pattern, _ in coverage.path_groups) == 1
        for name in groups) and all(
        sum(fnmatchcase(name, pattern) for name in groups) == count
        for pattern, count in coverage.path_groups)
    checks = {
        "timing_report_complete": complete,
        "target_clock_period": complete and math.isclose(
            groups[coverage.clock]["clock_period_ns"], coverage.period_ns,
            rel_tol=0.0, abs_tol=1e-9),
        "no_setup_violations": complete and all(
            group["setup_wns_ns"] >= 0 and group["setup_tns_ns"] >= 0 and
            group["setup_violating_paths"] == 0 for group in groups.values()),
        "no_hold_violations": complete and all(
            group["hold_wns_ns"] >= 0 and group["hold_tns_ns"] >= 0 and
            group["hold_violating_paths"] == 0 for group in groups.values()),
    }
    return groups, checks


def area_evidence(report: str, expected_macros: int):
    fields = {"digital_count", "digital_area", "macro_count", "macro_area"}
    values = {}
    complete = True
    for line in report.splitlines():
        key, separator, text = line.partition("=")
        if not separator or key not in fields or key in values:
            complete = False
            continue
        try:
            value = float(text)
        except ValueError:
            complete = False
            continue
        if not math.isfinite(value) or value < 0 or (key.endswith("_count") and not value.is_integer()):
            complete = False
            continue
        values[key] = int(value) if key.endswith("_count") else value
    complete = complete and values.keys() == fields
    return values, {
        "area_report_complete": complete,
        "macro_count": complete and values["macro_count"] == expected_macros,
        "digital_area": complete and values["digital_area"] > 0 and values["digital_count"] > 0,
    }
