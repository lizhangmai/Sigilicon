"""Shared fixture mechanics for cell-owned nominal transmission-gate tests.

Bidirectional logic transfer is the functional contract.  Transfer error,
delay, off-state disturbance, and on-resistance are diagnostic measurements;
this workflow does not invent fixed product performance limits for them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib
from typing import Any, Callable, Mapping

from sigilicon.artifacts import file_sha256
from sigilicon.domain.config_contracts import require_config_header
from sigilicon.domain.design import DesignSpec, load_design_spec
from sigilicon.domain.netlist import extract_subckt_body, iter_spectre_logical_lines
from sigilicon.domain.provenance import design_identity_fingerprint, digest
from sigilicon.paths import ProjectContext
from sigilicon.waveforms import Waveform, extrema_in_window, first_crossing, parse_spectre_direct_print, render_waveform_csv, value_at
from sigilicon.workflows.design_lifecycle import inspect_design
from sigilicon.workflows.spectre import MeasurementContractFailure, SpectreArtifactContext, StagedSpectreInput, run_spectre_measurement


SIGNALS = ("a_ab", "b_ab", "a_ba", "b_ba", "a_off", "b_off", "iron")
MOS_RE = re.compile(r"(?P<name>M[A-Za-z0-9_$]+)\s+\((?P<nodes>[^)]+)\)\s+(?P<master>[A-Za-z0-9_$]+)(?:\s+.*)?\Z")


@dataclass(frozen=True)
class SwitchQualification:
    path: Path
    project_root: Path
    design: DesignSpec
    testbench: str
    model_section: str
    temperature_c: float
    vdd_v: float
    vcm_v: float
    stop_s: float
    ab_edge_s: float
    ba_edge_s: float
    source_edge_s: float
    ab_sample_s: float
    ba_sample_s: float
    isolation_end_s: float
    ron_sample_s: float
    maxstep_s: float
    timeout_s: int
    load_f: float
    leakage_resistance_ohm: float
    ron_delta_v: float
    logic_decode_reference: str
    analog_measurement_role: str


def _positive(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{field} must be positive")
    return float(value)


def load_switch_qualification(
    path: Path,
    *,
    context: ProjectContext,
) -> SwitchQualification:
    config_path = path.resolve()
    with config_path.open("rb") as stream:
        raw = tomllib.load(stream)
    require_config_header(
        raw,
        config_path,
        contract_kind="diagnostic-campaign",
        path_scope="cell",
    )
    design_raw, point, electrical = raw.get("design"), raw.get("simulation_point"), raw.get("electrical")
    if not all(isinstance(item, dict) for item in (design_raw, point, electrical)):
        raise ValueError("design, simulation_point, and electrical must be tables")
    assert isinstance(design_raw, dict) and isinstance(point, dict) and isinstance(electrical, dict)
    semantics = electrical.get("semantics")
    if not isinstance(semantics, dict):
        raise ValueError("electrical.semantics must be a table")
    root = context.project_root
    design = load_design_spec(config_path.parent / str(design_raw["top"]), project_root=root)
    config = SwitchQualification(
        path=config_path, project_root=root, design=design, testbench=str(design_raw["testbench"]),
        model_section=str(electrical["model_section"]), temperature_c=float(point["temperature_c"]),
        vdd_v=_positive(point.get("vdd_v"), "vdd_v"), vcm_v=_positive(point.get("vcm_v"), "vcm_v"),
        stop_s=_positive(electrical.get("stop_s"), "stop_s"), ab_edge_s=_positive(electrical.get("ab_edge_s"), "ab_edge_s"),
        ba_edge_s=_positive(electrical.get("ba_edge_s"), "ba_edge_s"), source_edge_s=_positive(electrical.get("source_edge_s"), "source_edge_s"),
        ab_sample_s=_positive(electrical.get("ab_sample_s"), "ab_sample_s"), ba_sample_s=_positive(electrical.get("ba_sample_s"), "ba_sample_s"),
        isolation_end_s=_positive(electrical.get("isolation_end_s"), "isolation_end_s"), ron_sample_s=_positive(electrical.get("ron_sample_s"), "ron_sample_s"),
        maxstep_s=_positive(electrical.get("maxstep_s"), "maxstep_s"), timeout_s=int(electrical["timeout_s"]),
        load_f=_positive(electrical.get("load_f"), "load_f"), leakage_resistance_ohm=_positive(electrical.get("leakage_resistance_ohm"), "leakage_resistance_ohm"),
        ron_delta_v=_positive(electrical.get("ron_delta_v"), "ron_delta_v"),
        logic_decode_reference=str(semantics.get("logic_decode_reference")),
        analog_measurement_role=str(semantics.get("analog_measurement_role")),
    )
    if design.inputs != ("EN", "ENB") or design.inouts != ("A", "B") or design.outputs:
        raise ValueError("switch fixture requires A/B inouts and EN/ENB controls")
    if config.model_section != design.pdk.simulation.default.single_section or not (0.0 < config.vcm_v < config.vdd_v):
        raise ValueError("switch simulation point drifted from the platform")
    if config.logic_decode_reference != "VDD/2":
        raise ValueError("logic_decode_reference must be the supply-derived VDD/2")
    if config.analog_measurement_role != "diagnostic_measurement":
        raise ValueError("analog_measurement_role must be diagnostic_measurement")
    ordered = (config.ab_edge_s, config.ab_sample_s, config.ba_edge_s, config.ba_sample_s, config.ron_sample_s, config.isolation_end_s, config.stop_s)
    if tuple(sorted(ordered)) != ordered or len(set(ordered)) != len(ordered):
        raise ValueError("switch timing points must be strictly ordered")
    return config


def validate_switch_topology(config: SwitchQualification) -> Mapping[str, object]:
    inspection = inspect_design(config.design.path, project_root=config.project_root)
    if config.design.netlist_snapshot.subckts != (config.design.cell,):
        raise ValueError(f"{config.design.cell} circuit.scs must own only its subckt")
    devices: dict[str, tuple[tuple[str, ...], str]] = {}
    for line in iter_spectre_logical_lines(extract_subckt_body(config.design.netlist_snapshot, config.design.cell)):
        match = MOS_RE.fullmatch(line)
        if match is not None:
            devices[match.group("name")] = (tuple(match.group("nodes").split()), match.group("master"))
    if set(devices) != {"MN0", "MP0"}:
        raise ValueError("transmission gate must contain exactly one NMOS and one PMOS")
    if devices["MN0"][0][:3] != ("B", "EN", "A") or devices["MP0"][0][:3] != ("B", "ENB", "A"):
        raise ValueError("transmission-gate A/B/control connectivity drifted")
    return {
        "passed": True, "library": config.design.library, "cell": config.design.cell,
        "source_fingerprint": inspection.source_fingerprint, "ports": list(config.design.port_order),
        "mos_count": 2, "bidirectional_terminals": ["A", "B"],
        "on_controls": {"EN": 1, "ENB": 0}, "off_controls": {"EN": 0, "ENB": 1},
    }


def render_switch_deck(config: SwitchQualification) -> Callable[[Mapping[str, str]], str]:
    high, low = config.vdd_v, 0.0
    ron_a = config.vcm_v + config.ron_delta_v / 2.0
    ron_b = config.vcm_v - config.ron_delta_v / 2.0

    def render(paths: Mapping[str, str]) -> str:
        return f'''simulator lang=spectre
include "{paths["model"]}" section={config.model_section}
include "{paths["canonical_source"]}"
global 0
simulatorOptions options temp={config.temperature_c:.17g}

VVDD (VDD 0) vsource dc={high:.17g}
VVCM (VCM 0) vsource dc={config.vcm_v:.17g}

VAAB (A_AB 0) vsource type=pwl wave=[0 {low:.17g} {config.ab_edge_s:.17g} {low:.17g} {config.ab_edge_s + config.source_edge_s:.17g} {high:.17g} {config.stop_s:.17g} {high:.17g}]
XAB (A_AB B_AB VDD 0 VDD 0) {config.design.cell}
CL_AB (B_AB 0) capacitor c={config.load_f:.17g}

VBBA (B_BA 0) vsource type=pwl wave=[0 {high:.17g} {config.ba_edge_s:.17g} {high:.17g} {config.ba_edge_s + config.source_edge_s:.17g} {low:.17g} {config.stop_s:.17g} {low:.17g}]
XBA (A_BA B_BA VDD 0 VDD 0) {config.design.cell}
CL_BA (A_BA 0) capacitor c={config.load_f:.17g}

VAOFF (A_OFF 0) vsource type=pwl wave=[0 {low:.17g} {config.ab_edge_s:.17g} {low:.17g} {config.ab_edge_s + config.source_edge_s:.17g} {high:.17g} {config.stop_s:.17g} {high:.17g}]
XOFF (A_OFF B_OFF 0 VDD VDD 0) {config.design.cell}
CL_OFF (B_OFF 0) capacitor c={config.load_f:.17g}
RLEAK_OFF (B_OFF VCM) resistor r={config.leakage_resistance_ohm:.17g}

VARON (A_RON 0) vsource dc={ron_a:.17g}
VBRON (B_RON 0) vsource dc={ron_b:.17g}
XRON (A_RON B_RON VDD 0 VDD 0) {config.design.cell}

tran_switch tran stop={config.stop_s:.17g} maxstep={config.maxstep_s:.17g} errpreset=conservative
print v(A_AB),v(B_AB),v(A_BA),v(B_BA),v(A_OFF),v(B_OFF),i(VBRON),name=tran_switch to="switch.prn" precision="%.17g"
'''

    return render


def evaluate_switch_waveform(config: SwitchQualification, waveform: Waveform) -> Mapping[str, object]:
    ab_value = value_at(waveform, "b_ab", config.ab_sample_s)
    ba_value = value_at(waveform, "a_ba", config.ba_sample_s)
    reference_v = config.vdd_v / 2.0
    ab_source_cross = first_crossing(
        waveform, "a_ab", reference_v, direction="rising",
        start_s=config.ab_edge_s, end_s=config.ab_sample_s,
    )
    ab_output_cross = first_crossing(
        waveform, "b_ab", reference_v, direction="rising",
        start_s=config.ab_edge_s, end_s=config.ab_sample_s,
    )
    ba_source_cross = first_crossing(
        waveform, "b_ba", reference_v, direction="falling",
        start_s=config.ba_edge_s, end_s=config.ba_sample_s,
    )
    ba_output_cross = first_crossing(
        waveform, "a_ba", reference_v, direction="falling",
        start_s=config.ba_edge_s, end_s=config.ba_sample_s,
    )
    ab_delay = (
        ab_output_cross - ab_source_cross
        if ab_source_cross is not None and ab_output_cross is not None else None
    )
    ba_delay = (
        ba_output_cross - ba_source_cross
        if ba_source_cross is not None and ba_output_cross is not None else None
    )
    off_baseline = value_at(
        waveform, "b_off", config.ab_edge_s - config.source_edge_s
    )
    off_min, off_max = extrema_in_window(
        waveform, "b_off", config.ab_edge_s, config.isolation_end_s
    )
    off_disturbance = max(
        abs(off_min - off_baseline), abs(off_max - off_baseline)
    )
    ron_current = abs(value_at(waveform, "iron", config.ron_sample_s))
    ron = config.ron_delta_v / ron_current if ron_current > 0.0 else None
    criteria = {
        "a_to_b_logic_transfer": ab_value > reference_v,
        "b_to_a_logic_transfer": ba_value < reference_v,
        "a_to_b_transition_observed": ab_delay is not None and ab_delay >= 0.0,
        "b_to_a_transition_observed": ba_delay is not None and ba_delay >= 0.0,
        "on_resistance_measured": ron is not None,
    }
    return {
        "contract_version": 1, "passed": all(criteria.values()), "criteria": criteria,
        "a_to_b_sample_v": ab_value, "b_to_a_sample_v": ba_value,
        "a_to_b_transfer_error_v": ab_value - config.vdd_v,
        "b_to_a_transfer_error_v": ba_value,
        "a_to_b_propagation_delay_s": ab_delay,
        "b_to_a_propagation_delay_s": ba_delay,
        "off_isolation_baseline_v": off_baseline,
        "off_isolation_peak_disturbance_v": off_disturbance,
        "on_resistance_ohm": ron, "on_resistance_current_a": ron_current,
        "diagnostic_semantics": {
            "logic_decode_reference": "VDD/2",
            "logic_decode_reference_v": reference_v,
            "analog_measurement_role": "diagnostic_measurement",
            "fixed_transfer_error_limit_v": None,
            "fixed_propagation_delay_limit_s": None,
            "fixed_off_isolation_limit_v": None,
            "fixed_on_resistance_limit_ohm": None,
        },
        "fixture": {
            "load_f": config.load_f,
            "off_bias_v": config.vcm_v,
            "off_bias_resistance_ohm": config.leakage_resistance_ohm,
            "isolation_definition": "peak B disturbance from its pre-A-edge finite-source operating point",
            "ron_delta_v": config.ron_delta_v,
        },
        "uncovered_qualification": ["PVT", "Monte Carlo", "PEX", "signal/supply noise"],
    }


def _pdk_files(config: SwitchQualification) -> tuple[Path, ...]:
    paths = config.design.pdk.simulation.default.files
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"required PDK model file does not exist: {path}")
    return paths


def run_switch_qualification(config: SwitchQualification, *, runner_path: Path, spectre: Path | None = None) -> Mapping[str, object]:
    topology = validate_switch_topology(config)
    models = _pdk_files(config)
    runner = runner_path.resolve()
    setup = digest({
        "qualification": file_sha256(config.path), "runner": file_sha256(runner),
        "pdk": {path.name: file_sha256(path) for path in models},
    })
    source = design_identity_fingerprint(config.design)
    context = SpectreArtifactContext(
        project_root=config.project_root, library=config.design.library, cell=config.design.cell,
        testbench=config.testbench, source_fingerprint=source, setup_fingerprint=setup,
    )
    inputs = (
        StagedSpectreInput("model", models[0], ("pdk", models[0].name), "PDK model entry"),
        *(StagedSpectreInput(f"model_support_{index}", path, ("pdk", path.name), "PDK model support") for index, path in enumerate(models[1:])),
        StagedSpectreInput("canonical_source", config.design.source_netlist, ("sources", "circuit.scs"), f"canonical {config.design.cell} source"),
        StagedSpectreInput("design_spec", config.design.path, ("design.toml",), "cell interface declaration"),
        StagedSpectreInput("spec", config.path, ("qualification.toml",), "cell-owned switch contract"),
        StagedSpectreInput("runner", runner, ("characterization.py",), "cell-owned measurements"),
    )
    condition = {"corner": config.model_section, "temperature_c": config.temperature_c, "vdd_v": config.vdd_v, "nominal_only": True}
    try:
        result = run_spectre_measurement(
            context, kind="nominal_transmission_gate", condition=condition,
            run_fingerprint=digest({"source": source, "setup": setup, "condition": condition}), inputs=inputs,
            external_input_references={
                "canonical_topology": dict(topology),
                "canonical_source": {"path": str(config.design.source_netlist), "sha256": file_sha256(config.design.source_netlist)},
                "pdk_model_package": [{"path": str(path), "sha256": file_sha256(path)} for path in models],
            }, render=render_switch_deck(config), output_name="switch.prn", raw_result_name="switch.prn",
            parse=lambda text: parse_spectre_direct_print(text, signals=SIGNALS), normalize=render_waveform_csv,
            normalized_name="switch.csv", evaluate=lambda waveform: evaluate_switch_waveform(config, waveform),
            timeout=config.timeout_s, spectre=spectre,
        )
    except MeasurementContractFailure as error:
        result = error.result
    return {"passed": result.passed, "run_id": result.run_id, "manifest": str(result.manifest_path), "measurements": str(result.measurements)}
