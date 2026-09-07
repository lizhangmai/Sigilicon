"""FC diagnostic observations, with explicit unknowns and report provenance."""
from dataclasses import asdict, dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class Observation:
    value: float | int | None
    report: str
    status: str
    unit: str


def observations(directory: Path) -> dict:
    # Counts spanning scenarios/path groups use max, not a sum of duplicate paths.
    patterns = {
        "setup_violating_paths_lower_bound": ("report_qor", r"No\.\s*of\s+Violating\s+Paths\s*:\s*(\d+)", "paths", max),
        "hold_violations_lower_bound": ("report_qor", r"No\.\s*of\s+Hold\s+Violations\s*:\s*(\d+)", "paths", max),
        "open_nets": ("check_routes", r"Total\s+number\s+of\s+open\s+nets\s*=\s*(\d+)", "nets", max),
        "routing_drcs": ("check_routes", r"Total\s+number\s+of\s+DRCs\s*=\s*(\d+)", "violations", max),
        "cell_area": ("report_qor", r"Cell\s+Area\s*:\s*([\d.eE+-]+)", "library_area_unit", max),
        "setup_wns": ("report_qor", r"Critical\s+Path\s+Slack\s*:\s*([\d.eE+-]+)", "library_time_unit", min),
    }
    for category in ("macro", "digital", "physical_only"):
        patterns[category + "_count"] = ("macro_accounting", rf"^{category}_count=(\d+)$", "cells", max)
        patterns[category + "_area"] = ("macro_accounting", rf"^{category}_area=([\d.eE+-]+)$", "library_area_unit", max)
    result = {}
    for name, (filename, pattern, unit, aggregate) in patterns.items():
        path = directory / filename
        text = path.read_text(errors="replace") if path.is_file() else ""
        values = [float(v) for v in re.findall(pattern, text, re.I | re.M)]
        value = aggregate(values) if values else None
        if value is not None and unit in {"paths", "nets", "violations", "cells"}:
            value = int(value)
        result[name] = asdict(Observation(value, filename,
            "observed" if values else ("unparsed" if text.strip() else "unavailable"), unit))
    power_path = directory / "digital_power"
    text = power_path.read_text(errors="replace") if power_path.is_file() else ""
    number = r"([\d.eE+-]+)\s*([munpf]?W)"
    totals = re.findall(r"Total\s*\(\s*\d+\s+cells\)\s*" + number + r"\s+" + number
                        + r"\s+" + number + r"\s*\([^)]*\)\s*" + number, text)
    scales = {"W": 1, "mW": 1e-3, "uW": 1e-6, "nW": 1e-9, "pW": 1e-12, "fW": 1e-15}
    for i, name in enumerate(("internal", "driven_net_switching", "dynamic", "leakage")):
        # Report current scenario only. Multiple totals are ambiguous, not a sum.
        value = float(totals[0][2*i]) * scales[totals[0][2*i+1]] if len(totals) == 1 else None
        result["digital_" + name + "_power"] = asdict(Observation(value, "digital_power",
            "observed" if value is not None else ("unparsed" if text.strip() else "unavailable"), "W"))
    return result
