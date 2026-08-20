"""Reusable parsers for Synopsys reports used by design-owned PPA workflows."""

from __future__ import annotations

import math
import os
from pathlib import Path
import re
import shutil
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sigilicon.artifacts import ArtifactRecord, file_sha256
from sigilicon.domain.config_contracts import require_config_header
from sigilicon.external_tools import run_process_group


_POWER_UNITS = {"W": 1.0, "mW": 1e-3, "uW": 1e-6, "nW": 1e-9, "pW": 1e-12}


def _require_tool_profile_header(raw: Mapping[str, Any], path: Path) -> None:
    """Accept IP- and product-owned tool profiles without mixing their scopes."""

    header = require_config_header(
        raw,
        path,
        contract_kind=("ip-toolchain-contract", "soc-toolchain-contract"),
        path_scope=("owner", "product"),
    )
    expected_scope = {
        "ip-toolchain-contract": "owner",
        "soc-toolchain-contract": "product",
    }[header.contract_kind]
    if header.path_scope != expected_scope:
        raise ValueError(
            f"{path}: {header.contract_kind} must use {expected_scope!r} scope"
        )


@dataclass(frozen=True)
class SynopsysPpaInputs:
    """Resolved and provenance-checked inputs for one boundary synthesis stage."""

    dc_launcher: Path
    stdcell_db: Path
    designware_root: Path
    designware_foundation: Path
    designware_models: Mapping[str, Path]
    operators: tuple[str, ...]
    release: str
    reference_filter: str


@dataclass(frozen=True)
class SynopsysBoundary:
    """Design-provided topology for one generic synthesis boundary."""

    top: str
    source_roles: tuple[str, ...]
    expected_references: Mapping[str, int]
    defines: str = ""
    clock_port: str = ""
    reset_ports: str = ""


@dataclass(frozen=True)
class SynopsysBoundaryResults:
    """Reports and copied inputs returned by the generic boundary runner."""

    rows: Mapping[str, Mapping[str, Any]]
    proof: tuple[Path, ...]
    mapped_netlists: Mapping[str, Path]
    designware_inputs: Mapping[str, Path]


def load_synopsys_ppa_toolchain(
    tool_profile_path: Path,
    platform_binding_path: Path,
) -> dict[str, Any]:
    """Compose tool provenance and technology binding without design policy."""

    resolved_profile = tool_profile_path.resolve()
    with resolved_profile.open("rb") as stream:
        profile_raw = tomllib.load(stream)
    _require_tool_profile_header(profile_raw, resolved_profile)
    resolved_binding = platform_binding_path.resolve()
    with resolved_binding.open("rb") as stream:
        binding_raw = tomllib.load(stream)
    require_config_header(
        binding_raw,
        resolved_binding,
        contract_kind="platform-tool-binding",
        path_scope="platform",
    )
    toolchain = profile_raw.get("toolchain")
    technology = binding_raw.get("technology")
    if (
        profile_raw.get("tool") != "synopsys"
        or not isinstance(toolchain, Mapping)
    ):
        raise ValueError("Synopsys PPA tool profile contract is unsupported")
    if (
        binding_raw.get("tool") != "synopsys"
        or not isinstance(binding_raw.get("platform"), str)
        or not isinstance(binding_raw.get("corner"), str)
        or not isinstance(technology, Mapping)
    ):
        raise ValueError("Synopsys PPA platform binding contract is unsupported")
    overlap = set(toolchain).intersection(technology)
    if overlap:
        raise ValueError(
            "Synopsys tool profile and platform binding overlap: "
            f"{sorted(overlap)}"
        )
    design_policy = {
        "designware_model_sha256",
        "designware_foundation_sha256",
        "designware_operators",
        "reference_filter",
    }.intersection({*toolchain, *technology})
    if design_policy:
        raise ValueError(
            "Synopsys tool/platform configuration cannot own design policy: "
            f"{sorted(design_policy)}"
        )
    required_profile = (
        "eda_root_environment",
        "designware_root",
        "designware_foundation_sldb",
        "designware_release",
    )
    if any(
        not isinstance(toolchain.get(name), str) or not toolchain[name]
        for name in required_profile
    ):
        raise ValueError("Synopsys PPA tool profile is incomplete")
    required_technology = ("stdcell_db", "stdcell_library")
    if any(
        not isinstance(technology.get(name), str) or not technology[name]
        for name in required_technology
    ):
        raise ValueError("Synopsys PPA platform binding is incomplete")
    return {**toolchain, **technology}


def load_designware_contract(path: Path) -> dict[str, Any]:
    """Load design-selected DesignWare operators and their model provenance."""

    resolved = path.resolve()
    with resolved.open("rb") as stream:
        raw = tomllib.load(stream)
    header = require_config_header(
        raw,
        resolved,
        contract_kind=("ip-component", "soc-toolchain-contract"),
        path_scope=("owner", "product"),
    )
    expected_scope = {
        "ip-component": "owner",
        "soc-toolchain-contract": "product",
    }[header.contract_kind]
    if header.path_scope != expected_scope:
        raise ValueError(
            f"{resolved}: {header.contract_kind} must use {expected_scope!r} scope"
        )
    contract = raw.get("designware")
    if not isinstance(contract, Mapping):
        raise ValueError("DesignWare component contract is unsupported")
    operators = contract.get("operators")
    reference_filter = contract.get("reference_filter")
    if (
        not isinstance(operators, list)
        or not operators
        or any(not isinstance(item, str) or not item for item in operators)
        or len(set(operators)) != len(operators)
        or not isinstance(reference_filter, str)
        or not reference_filter.strip()
    ):
        raise ValueError("DesignWare component contract is invalid")
    return dict(contract)


def synopsys_constraint_environment(value: object) -> dict[str, str]:
    """Validate tracked boundary assumptions and render their Tcl environment."""

    if not isinstance(value, Mapping):
        raise ValueError("Synopsys boundary_constraints must be a TOML table")
    numeric: dict[str, float] = {}
    for name in (
        "clock_uncertainty_ns",
        "input_delay_ns",
        "output_delay_ns",
        "input_static_probability",
        "input_toggle_rate",
        "output_load",
    ):
        raw = value.get(name)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"Synopsys boundary constraint {name} must be numeric")
        parsed = float(raw)
        if not math.isfinite(parsed) or parsed < 0.0:
            raise ValueError(
                f"Synopsys boundary constraint {name} must be non-negative"
            )
        numeric[name] = parsed
    if numeric["input_static_probability"] > 1.0:
        raise ValueError("input_static_probability must not exceed one")
    return {
        "PPA_CLOCK_UNCERTAINTY_NS": str(numeric["clock_uncertainty_ns"]),
        "PPA_INPUT_DELAY_NS": str(numeric["input_delay_ns"]),
        "PPA_OUTPUT_DELAY_NS": str(numeric["output_delay_ns"]),
        "PPA_INPUT_STATIC_PROBABILITY": str(numeric["input_static_probability"]),
        "PPA_INPUT_TOGGLE_RATE": str(numeric["input_toggle_rate"]),
        "PPA_OUTPUT_LOAD": str(numeric["output_load"]),
    }


def resolve_eda_root(config: Mapping[str, Any]) -> Path:
    """Resolve the site-local EDA root named by a tracked toolchain contract."""

    name = str(config.get("eda_root_environment"))
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required EDA root environment variable {name} is not set")
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"EDA root does not exist: {root}")
    return root


def synopsys_compile_efforts(strategy: object) -> tuple[str, str]:
    """Translate a tracked compile strategy into Design Compiler efforts."""

    match = re.fullmatch(
        r"compile-map-(low|medium|high)-area-(low|medium|high)", str(strategy)
    )
    if match is None:
        raise ValueError(f"unsupported Synopsys compile strategy: {strategy!r}")
    return match.group(1), match.group(2)


def _positive(value: float, label: str) -> float:
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"{label} must be a finite positive number")
    return value


def parse_area_report(path: Path) -> float:
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"Total cell area:\s*([0-9.eE+-]+)", text)
    if match is None:
        raise ValueError(f"Synopsys area report lacks Total cell area: {path}")
    return _positive(float(match.group(1)), f"cell area in {path}")


def parse_power_report(path: Path) -> float:
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(
        r"Total Dynamic Power\s*=\s*([0-9.eE+-]+)\s*(W|mW|uW|nW|pW)\b",
        text,
    )
    if match is None:
        raise ValueError(f"Synopsys power report lacks Total Dynamic Power: {path}")
    return _positive(float(match.group(1)), f"dynamic power in {path}") * _POWER_UNITS[
        match.group(2)
    ]


def parse_qor_timing(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    patterns = {
        "critical_path_length_ns": r"Critical Path Length:\s*([0-9.eE+-]+)",
        "critical_path_slack_ns": r"Critical Path Slack:\s*([0-9.eE+-]+)",
        "clock_period_ns": r"Critical Path Clk Period:\s*([0-9.eE+-]+)",
        "total_negative_slack_ns": r"Total Negative Slack:\s*([0-9.eE+-]+)",
        "violating_paths": r"No\. of Violating Paths:\s*([0-9.eE+-]+)",
    }
    parsed: dict[str, float] = {}
    for name, pattern in patterns.items():
        match = re.search(pattern, text)
        if match is None:
            raise ValueError(f"Synopsys QoR report lacks {name}: {path}")
        value = float(match.group(1))
        if not math.isfinite(value):
            raise ValueError(f"Synopsys QoR report has non-finite {name}: {path}")
        parsed[name] = value
    violating_paths = int(parsed.pop("violating_paths"))
    return {
        **parsed,
        "violating_paths": violating_paths,
        "timing_closed": parsed["critical_path_slack_ns"] >= 0.0,
    }


def parse_designware_inventory(
    path: Path, operators: Sequence[str]
) -> dict[str, int]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines or not lines[0].startswith("total\t"):
        raise ValueError(f"DesignWare inventory lacks its total row: {path}")
    try:
        total = int(lines[0].split("\t", 1)[1])
    except ValueError as error:
        raise ValueError(f"DesignWare inventory has an invalid total: {path}") from error
    if not operators or len(set(operators)) != len(operators):
        raise ValueError("DesignWare operator contract must be non-empty and unique")
    counts = {operator: 0 for operator in operators}
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) != 2:
            raise ValueError(f"DesignWare inventory row is malformed: {line!r}")
        reference = fields[1]
        matched = [operator for operator in counts if operator in reference]
        if len(matched) != 1:
            raise ValueError(f"unrecognized DesignWare FP reference {reference!r}")
        counts[matched[0]] += 1
    if sum(counts.values()) != total:
        raise ValueError(
            f"DesignWare inventory total {total} does not match parsed operators {counts}"
        )
    return counts


def designware_release(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"DesignWare_release:\s*(\S+)", text)
    if match is None:
        raise ValueError(f"DesignWare simulation model lacks release provenance: {path}")
    return match.group(1)


def resolve_synopsys_ppa_inputs(
    toolchain: Mapping[str, Any],
    designware_contract: Mapping[str, Any],
) -> SynopsysPpaInputs:
    """Resolve installed tools and verify the declared DesignWare release."""

    eda_root = resolve_eda_root(toolchain)
    dc_shell = shutil.which("dc_shell")
    if dc_shell is None:
        raise RuntimeError("managed PPA synthesis requires dc_shell on PATH")
    dc_launcher = Path(os.path.abspath(dc_shell))
    stdcell_db = (eda_root / str(toolchain["stdcell_db"])).resolve()
    designware_root = Path(str(toolchain["designware_root"])).expanduser()
    if not designware_root.is_absolute():
        designware_root = eda_root / designware_root
    designware_root = designware_root.resolve()
    designware_foundation = Path(
        str(toolchain["designware_foundation_sldb"])
    ).expanduser()
    if not designware_foundation.is_absolute():
        designware_foundation = eda_root / designware_foundation
    designware_foundation = designware_foundation.resolve()
    operators = tuple(str(operator) for operator in designware_contract["operators"])
    if not operators:
        raise ValueError("DesignWare operators must not be empty")
    designware_models = {
        operator: designware_root / "sim_ver" / f"{operator}.v"
        for operator in operators
    }
    required_files = (
        stdcell_db,
        designware_foundation,
        *designware_models.values(),
    )
    missing = [path for path in required_files if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Synopsys PPA input is missing: {missing}")
    release = str(toolchain["designware_release"])
    for operator, model_path in designware_models.items():
        if designware_release(model_path) != release:
            raise ValueError(f"DesignWare {operator} release provenance drifted")
    return SynopsysPpaInputs(
        dc_launcher=dc_launcher,
        stdcell_db=stdcell_db,
        designware_root=designware_root,
        designware_foundation=designware_foundation,
        designware_models=designware_models,
        operators=operators,
        release=release,
        reference_filter=str(designware_contract["reference_filter"]),
    )


def run_synopsys_boundaries(
    record: ArtifactRecord,
    *,
    inputs: SynopsysPpaInputs,
    script: Path,
    rtl_sources: Mapping[str, Path],
    boundaries: Mapping[str, SynopsysBoundary],
    compile_strategy: object,
    compile_clock_period_ns: float,
    report_clock_period_ns: float,
    boundary_constraints: Mapping[str, Any],
    timeout_s: int,
) -> SynopsysBoundaryResults:
    """Run and collect design-declared boundaries without topology policy."""

    if not script.is_file():
        raise FileNotFoundError(f"managed synthesis Tcl is missing: {script}")
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, int)
        or timeout_s <= 0
    ):
        raise ValueError("synthesis timeout must be a positive integer")
    compile_clock = _positive(float(compile_clock_period_ns), "compile clock")
    report_clock = _positive(float(report_clock_period_ns), "report clock")
    map_effort, area_effort = synopsys_compile_efforts(compile_strategy)
    constraint_environment = synopsys_constraint_environment(boundary_constraints)
    if not boundaries:
        raise ValueError("at least one Synopsys boundary is required")
    if not rtl_sources or any(not path.is_file() for path in rtl_sources.values()):
        raise FileNotFoundError("boundary RTL sources are incomplete")
    basenames = [path.name for path in rtl_sources.values()]
    if len(basenames) != len(set(basenames)):
        raise ValueError("boundary RTL source basenames must be unique")

    copied_script = record.copy_file(
        "inputs", ("synthesis.tcl",), script, label="managed synthesis Tcl"
    )
    copied_db = record.copy_file(
        "inputs",
        ("pdk", inputs.stdcell_db.name),
        inputs.stdcell_db,
        label="standard-cell database",
    )
    copied_foundation = record.copy_file(
        "inputs",
        ("designware", inputs.designware_foundation.name),
        inputs.designware_foundation,
        label="DesignWare synthetic library",
    )
    copied_designware = {
        operator: record.copy_file(
            "inputs",
            ("designware", path.name),
            path,
            label=f"Synopsys {operator} model provenance",
        )
        for operator, path in inputs.designware_models.items()
    }
    copied_rtl = {
        role: record.copy_file(
            "inputs", ("rtl", path.name), path, label=f"synthesis RTL role {role}"
        )
        for role, path in rtl_sources.items()
    }
    work = record.paths.role("work")
    for name, boundary in boundaries.items():
        if not boundary.top or not boundary.source_roles:
            raise ValueError(f"Synopsys boundary {name} is incomplete")
        missing_roles = set(boundary.source_roles) - set(copied_rtl)
        if missing_roles:
            raise ValueError(
                f"Synopsys boundary {name} references missing RTL roles: "
                f"{sorted(missing_roles)}"
            )
        expected = dict(boundary.expected_references)
        if set(expected) != set(inputs.operators) or any(
            isinstance(count, bool) or not isinstance(count, int) or count < 0
            for count in expected.values()
        ):
            raise ValueError(
                f"Synopsys boundary {name} reference inventory is invalid"
            )
        report_root = work / name
        environment = os.environ.copy()
        environment.update(
            {
                "PPA_WORK_ROOT": str(report_root),
                "PPA_STDCELL_DB": str(copied_db),
                "PPA_DW_FOUNDATION": str(copied_foundation),
                "PPA_RTL_SOURCES": os.pathsep.join(
                    str(copied_rtl[role]) for role in boundary.source_roles
                ),
                "PPA_TOP": boundary.top,
                "PPA_DEFINES": boundary.defines,
                "PPA_CLOCK_PORT": boundary.clock_port,
                "PPA_RESET_PORTS": boundary.reset_ports,
                "PPA_REFERENCE_FILTER": inputs.reference_filter,
                "PPA_EXPECTED_REFERENCE_INSTANCES": str(sum(expected.values())),
                "PPA_COMPILE_CLOCK_PERIOD_NS": str(compile_clock),
                "PPA_REPORT_CLOCK_PERIOD_NS": str(report_clock),
                "PPA_MAP_EFFORT": map_effort,
                "PPA_AREA_EFFORT": area_effort,
                **constraint_environment,
            }
        )
        process = run_process_group(
            [str(inputs.dc_launcher), "-f", str(copied_script)],
            cwd=work,
            env=environment,
            timeout=timeout_s,
        )
        record.write_text(
            "logs",
            (f"dc_shell-{name}.log",),
            process.stdout,
            label=f"{name} Design Compiler log",
        )
        if process.returncode != 0:
            raise RuntimeError(
                f"Synopsys boundary {name} failed: {process.returncode}"
            )

    rows: dict[str, Mapping[str, Any]] = {}
    proof: list[Path] = []
    mapped_netlists: dict[str, Path] = {}
    report_names = (
        "check_design.rpt",
        "qor.rpt",
        "area.rpt",
        "power.rpt",
        "power_hierarchy.rpt",
        "references.rpt",
        "timing.rpt",
        "reference_inventory.rpt",
    )
    for name, boundary in boundaries.items():
        report_root = work / name
        record.directory("results", name)
        copied_reports: dict[str, Path] = {}
        for report_name in report_names:
            report = report_root / report_name
            if report.is_file():
                copied_reports[report_name] = record.copy_file(
                    "results",
                    (name, report_name),
                    report,
                    label=f"{name} {report_name}",
                )
                proof.append(copied_reports[report_name])
        required_reports = {
            "area.rpt",
            "power.rpt",
            "qor.rpt",
            "reference_inventory.rpt",
        }
        if not required_reports.issubset(copied_reports):
            raise RuntimeError(f"Synopsys boundary {name} reports are incomplete")
        mapped = report_root / "mapped.v"
        if not mapped.is_file():
            raise RuntimeError(f"Synopsys boundary {name} mapped netlist is missing")
        mapped_copy = record.copy_file(
            "results",
            (name, "mapped_netlist.v"),
            mapped,
            label=f"{name} mapped netlist",
        )
        mapped_netlists[name] = mapped_copy
        proof.append(mapped_copy)
        inventory = parse_designware_inventory(
            copied_reports["reference_inventory.rpt"], inputs.operators
        )
        if inventory != dict(boundary.expected_references):
            raise RuntimeError(
                f"Synopsys boundary {name} reference inventory drifted"
            )
        dynamic_power_w = parse_power_report(copied_reports["power.rpt"])
        rows[name] = {
            "cell_area_um2": parse_area_report(copied_reports["area.rpt"]),
            "dynamic_power_w": dynamic_power_w,
            "dynamic_energy_pj_per_1ghz_cycle": dynamic_power_w * 1e3,
            "designware_instances": inventory,
            "timing": parse_qor_timing(copied_reports["qor.rpt"]),
            "mapped_netlist_sha256": file_sha256(mapped_copy),
        }
    return SynopsysBoundaryResults(
        rows=rows,
        proof=tuple(proof),
        mapped_netlists=mapped_netlists,
        designware_inputs=copied_designware,
    )
