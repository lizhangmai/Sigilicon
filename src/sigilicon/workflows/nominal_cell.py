"""Shared mechanics for design-owned nominal standard-cell qualification.

The owning cell supplies its canonical design, truth vectors, and diagnostic
fixture timing.  Logic is decoded at the supply-derived VDD/2 reference.
Propagation delay is measured and reported, but is not a product performance
gate unless a separately confirmed requirement is added to canonical metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any, Callable, Mapping, Sequence

from sigilicon.artifacts import file_sha256
from sigilicon.domain.config_contracts import require_config_header
from sigilicon.domain.design import DesignSpec, load_design_spec
from sigilicon.domain.provenance import design_fingerprint, digest
from sigilicon.paths import ProjectContext
from sigilicon.waveforms import (
    Waveform,
    first_crossing,
    parse_spectre_direct_print,
    render_waveform_csv,
    value_at,
)
from sigilicon.workflows.design_lifecycle import inspect_design
from sigilicon.workflows.spectre import (
    MeasurementContractFailure,
    SpectreArtifactContext,
    StagedSpectreInput,
    run_spectre_measurement,
)


PDK_SUPPORT_FILES = (
    "cln28hpcp_1d8_elk_v1d0_2p2_shrink0d9_embedded_usage.scs",
    "cln28hpcp_1d8_elk_v1d0_2p2.scs",
    "res_metal.scs",
)


@dataclass(frozen=True)
class LogicCase:
    name: str
    kind: str
    initial: tuple[int, ...]
    final: tuple[int, ...]
    initial_output: int
    expected_output: int
    trigger_input: str | None


@dataclass(frozen=True)
class LogicQualification:
    path: Path
    project_root: Path
    design: DesignSpec
    testbench: str
    model_section: str
    temperature_c: float
    vdd_v: float
    cycle_s: float
    stimulus_s: float
    sample_s: float
    edge_s: float
    maxstep_s: float
    timeout_s: int
    output_load_f: float
    logic_decode_reference: str
    propagation_delay_role: str
    cases: tuple[LogicCase, ...]


def _positive(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{field} must be positive")
    return float(value)


def _bits(value: Any, field: str, width: int) -> tuple[int, ...]:
    if not isinstance(value, list) or len(value) != width:
        raise ValueError(f"{field} must contain {width} logic values")
    result = tuple(int(item) for item in value)
    if any(item not in (0, 1) for item in result):
        raise ValueError(f"{field} values must be zero or one")
    return result


def load_logic_qualification(
    path: Path,
    *,
    context: ProjectContext,
) -> LogicQualification:
    config_path = path.resolve()
    with config_path.open("rb") as stream:
        raw = tomllib.load(stream)
    require_config_header(
        raw,
        config_path,
        contract_kind="diagnostic-campaign",
        path_scope="cell",
        owner="cim-compute",
    )
    design_raw = raw.get("design")
    point = raw.get("simulation_point")
    electrical = raw.get("electrical")
    case_rows = raw.get("cases")
    if not isinstance(design_raw, dict) or not isinstance(point, dict):
        raise ValueError("design and simulation_point must be tables")
    if not isinstance(electrical, dict) or not isinstance(case_rows, list) or not case_rows:
        raise ValueError("electrical table and cases array are required")
    semantics = electrical.get("semantics")
    if not isinstance(semantics, dict):
        raise ValueError("electrical.semantics must be a table")
    project_root = context.project_root
    design = load_design_spec(
        config_path.parent / str(design_raw["top"]), project_root=project_root
    )
    if len(design.outputs) != 1 or design.inouts:
        raise ValueError("nominal logic fixture requires one output and no signal inouts")
    width = len(design.inputs)
    parsed: list[LogicCase] = []
    for index, row in enumerate(case_rows):
        if not isinstance(row, dict):
            raise ValueError(f"cases[{index}] must be a table")
        kind = str(row.get("kind"))
        if kind not in {"truth", "delay"}:
            raise ValueError(f"cases[{index}].kind must be truth or delay")
        trigger = row.get("trigger_input")
        if trigger is not None and trigger not in design.inputs:
            raise ValueError(f"cases[{index}].trigger_input is not a design input")
        initial = _bits(row.get("initial"), f"cases[{index}].initial", width)
        final = _bits(row.get("final"), f"cases[{index}].final", width)
        initial_output = int(row.get("initial_output", -1))
        expected_output = int(row.get("expected_output", -1))
        if initial_output not in (0, 1) or expected_output not in (0, 1):
            raise ValueError(f"cases[{index}] outputs must be zero or one")
        changed = tuple(
            design.inputs[bit]
            for bit, (left, right) in enumerate(zip(initial, final, strict=True))
            if left != right
        )
        if kind == "truth" and (changed or trigger is not None):
            raise ValueError("truth cases must be static and have no trigger")
        if kind == "delay" and (changed != (trigger,) or initial_output == expected_output):
            raise ValueError("delay cases must toggle one named input and change the output")
        parsed.append(LogicCase(
            name=str(row["name"]), kind=kind, initial=initial, final=final,
            initial_output=initial_output, expected_output=expected_output,
            trigger_input=str(trigger) if trigger is not None else None,
        ))
    if len({item.name for item in parsed}) != len(parsed):
        raise ValueError("logic case names must be unique")
    config = LogicQualification(
        path=config_path,
        project_root=project_root,
        design=design,
        testbench=str(design_raw["testbench"]),
        model_section=str(electrical["model_section"]),
        temperature_c=float(point["temperature_c"]),
        vdd_v=_positive(point.get("vdd_v"), "vdd_v"),
        cycle_s=_positive(electrical.get("cycle_s"), "cycle_s"),
        stimulus_s=_positive(electrical.get("stimulus_s"), "stimulus_s"),
        sample_s=_positive(electrical.get("sample_s"), "sample_s"),
        edge_s=_positive(electrical.get("edge_s"), "edge_s"),
        maxstep_s=_positive(electrical.get("maxstep_s"), "maxstep_s"),
        timeout_s=int(electrical["timeout_s"]),
        output_load_f=_positive(electrical.get("output_load_f"), "output_load_f"),
        logic_decode_reference=str(semantics.get("logic_decode_reference")),
        propagation_delay_role=str(semantics.get("propagation_delay_role")),
        cases=tuple(parsed),
    )
    if config.model_section != design.pdk.model_section:
        raise ValueError("logic qualification model section drifted from the platform")
    if not (config.stimulus_s + config.edge_s < config.sample_s < config.cycle_s):
        raise ValueError("logic stimulus, sample, and cycle times must be ordered")
    if config.timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    if config.logic_decode_reference != "VDD/2":
        raise ValueError("logic_decode_reference must be the supply-derived VDD/2")
    if config.propagation_delay_role != "diagnostic_measurement":
        raise ValueError("propagation_delay_role must be diagnostic_measurement")
    truth_inputs = {item.final for item in config.cases if item.kind == "truth"}
    if len(truth_inputs) != 2 ** len(design.inputs):
        raise ValueError("logic qualification must contain a complete truth table")
    return config


def validate_logic_topology(config: LogicQualification) -> Mapping[str, object]:
    inspection = inspect_design(config.design.path, project_root=config.project_root)
    if config.design.netlist_snapshot.subckts != (config.design.cell,):
        raise ValueError(f"{config.design.cell} circuit.scs must define only its own subckt")
    return {
        "passed": True,
        "library": config.design.library,
        "cell": config.design.cell,
        "source_fingerprint": inspection.source_fingerprint,
        "subckts": list(config.design.netlist_snapshot.subckts),
        "inputs": list(config.design.inputs),
        "outputs": list(config.design.outputs),
        "ports": list(config.design.port_order),
        "truth_table_rows": 2 ** len(config.design.inputs),
        "delay_cases": [item.name for item in config.cases if item.kind == "delay"],
        "logic_decode_reference": config.logic_decode_reference,
        "propagation_delay_role": config.propagation_delay_role,
    }


def _input_wave(config: LogicQualification, input_index: int) -> str:
    points: list[tuple[float, float]] = []
    prior = config.cases[0].initial[input_index] * config.vdd_v
    points.append((0.0, prior))
    for index, case in enumerate(config.cases):
        base = index * config.cycle_s
        initial = case.initial[input_index] * config.vdd_v
        final = case.final[input_index] * config.vdd_v
        if initial != prior:
            points.extend(((base, prior), (base + config.edge_s, initial)))
        points.append((base + config.stimulus_s, initial))
        if final != initial:
            points.append((base + config.stimulus_s + config.edge_s, final))
        points.append((base + config.cycle_s - config.edge_s, final))
        prior = final
    return "[" + " ".join(f"{time:.17g} {value:.17g}" for time, value in points) + "]"


def render_logic_deck(config: LogicQualification) -> Callable[[Mapping[str, str]], str]:
    stop_s = len(config.cases) * config.cycle_s
    sources = "\n".join(
        f"V{name} ({name} 0) vsource type=pwl wave={_input_wave(config, index)}"
        for index, name in enumerate(config.design.inputs)
    )
    node_by_port = {
        **{name: name for name in config.design.inputs},
        config.design.outputs[0]: "OUT",
        config.design.primary_supply: "VDD",
        config.design.ground_supply: "0",
    }
    nodes = " ".join(node_by_port[name] for name in config.design.port_order)
    prints = ",".join(
        (f"v({name})" for name in (*config.design.inputs, "OUT"))
    )

    def render(paths: Mapping[str, str]) -> str:
        return f'''simulator lang=spectre
include "{paths["model"]}" section={config.model_section}
include "{paths["canonical_source"]}"
global 0
simulatorOptions options temp={config.temperature_c:.17g}

VVDD (VDD 0) vsource dc={config.vdd_v:.17g}
{sources}
XDUT ({nodes}) {config.design.cell}
CLOAD (OUT 0) capacitor c={config.output_load_f:.17g}

tran_logic tran stop={stop_s:.17g} maxstep={config.maxstep_s:.17g} errpreset=conservative
print {prints},name=tran_logic to="logic.prn" precision="%.17g"
'''

    return render


def _logic(config: LogicQualification, value: float) -> int | None:
    reference = config.vdd_v / 2.0
    if value < reference:
        return 0
    if value > reference:
        return 1
    return None


def evaluate_logic_waveform(
    config: LogicQualification, waveform: Waveform
) -> Mapping[str, object]:
    rows: list[dict[str, object]] = []
    delays: list[float] = []
    for index, case in enumerate(config.cases):
        base = index * config.cycle_s
        output_v = value_at(waveform, "out", base + config.sample_s)
        observed = _logic(config, output_v)
        delay = None
        if case.kind == "delay":
            assert case.trigger_input is not None
            trigger_index = config.design.inputs.index(case.trigger_input)
            input_direction = "rising" if case.final[trigger_index] else "falling"
            output_direction = "rising" if case.expected_output else "falling"
            start = base + config.stimulus_s
            end = base + config.sample_s
            input_crossing = first_crossing(
                waveform, case.trigger_input.lower(), config.vdd_v / 2.0,
                direction=input_direction, start_s=start, end_s=end,
            )
            output_crossing = first_crossing(
                waveform, "out", config.vdd_v / 2.0,
                direction=output_direction, start_s=start, end_s=end,
            )
            if input_crossing is not None and output_crossing is not None:
                delay = output_crossing - input_crossing
                delays.append(delay)
        rows.append({
            "name": case.name,
            "kind": case.kind,
            "initial_inputs": dict(zip(config.design.inputs, case.initial, strict=True)),
            "final_inputs": dict(zip(config.design.inputs, case.final, strict=True)),
            "expected_output": case.expected_output,
            "observed_output": observed,
            "output_v": output_v,
            "passed": observed == case.expected_output,
            "trigger_input": case.trigger_input,
            "propagation_delay_s": delay,
        })
    truth_rows = [row for row in rows if row["kind"] == "truth"]
    delay_rows = [row for row in rows if row["kind"] == "delay"]
    criteria = {
        "complete_truth_table": all(bool(row["passed"]) for row in truth_rows),
        "delay_transitions_correct": all(bool(row["passed"]) for row in delay_rows),
        "all_declared_delays_measured": len(delays) == len(delay_rows),
    }
    return {
        "contract_version": 1,
        "passed": all(criteria.values()),
        "criteria": criteria,
        "rows": rows,
        "truth_table_rows": len(truth_rows),
        "propagation_delays_s": {
            str(row["name"]): row["propagation_delay_s"] for row in delay_rows
        },
        "maximum_propagation_delay_s": max(delays) if delays else None,
        "diagnostic_semantics": {
            "logic_decode_reference": "VDD/2",
            "logic_decode_reference_v": config.vdd_v / 2.0,
            "propagation_delay_role": "diagnostic_measurement",
            "fixed_propagation_delay_limit_s": None,
        },
        "uncovered_qualification": ["PVT", "Monte Carlo", "PEX", "supply noise"],
    }


def _pdk_files(config: LogicQualification) -> tuple[Path, ...]:
    root = config.design.pdk.model_file.parent
    paths = (
        config.design.pdk.model_file,
        *(root / name for name in PDK_SUPPORT_FILES),
    )
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"required PDK model file does not exist: {path}")
    return paths


def run_logic_qualification(
    config: LogicQualification,
    *,
    runner_path: Path,
    spectre: Path | None = None,
) -> Mapping[str, object]:
    topology = validate_logic_topology(config)
    models = _pdk_files(config)
    runner = runner_path.resolve()
    setup = digest({
        "qualification": file_sha256(config.path),
        "runner": file_sha256(runner),
        "pdk": {path.name: file_sha256(path) for path in models},
    })
    source = design_fingerprint(config.design)
    context = SpectreArtifactContext(
        project_root=config.project_root,
        library=config.design.library,
        cell=config.design.cell,
        testbench=config.testbench,
        source_fingerprint=source,
        setup_fingerprint=setup,
    )
    inputs = (
        StagedSpectreInput("model", models[0], ("pdk", models[0].name), "PDK model entry"),
        *(
            StagedSpectreInput(
                f"model_support_{index}", path, ("pdk", path.name), "PDK model support"
            )
            for index, path in enumerate(models[1:])
        ),
        StagedSpectreInput(
            "canonical_source", config.design.source_netlist,
            ("sources", "circuit.scs"), f"canonical {config.design.cell} source",
        ),
        StagedSpectreInput("design_spec", config.design.path, ("design.toml",), "cell interface declaration"),
        StagedSpectreInput("spec", config.path, ("qualification.toml",), "cell-owned qualification contract"),
        StagedSpectreInput("runner", runner, ("characterization.py",), "cell-owned qualification entry"),
    )
    condition = {
        "corner": config.model_section,
        "temperature_c": config.temperature_c,
        "vdd_v": config.vdd_v,
        "truth_table_rows": 2 ** len(config.design.inputs),
        "nominal_only": True,
    }
    try:
        result = run_spectre_measurement(
            context,
            kind="nominal_logic_cell",
            condition=condition,
            run_fingerprint=digest({"source": source, "setup": setup, "condition": condition}),
            inputs=inputs,
            external_input_references={
                "canonical_topology": dict(topology),
                "canonical_source": {
                    "path": str(config.design.source_netlist),
                    "sha256": file_sha256(config.design.source_netlist),
                },
                "pdk_model_package": [
                    {"path": str(path), "sha256": file_sha256(path)} for path in models
                ],
            },
            render=render_logic_deck(config),
            output_name="logic.prn",
            raw_result_name="logic.prn",
            parse=lambda text: parse_spectre_direct_print(
                text, signals=tuple(name.lower() for name in config.design.inputs) + ("out",)
            ),
            normalize=render_waveform_csv,
            normalized_name="logic.csv",
            evaluate=lambda waveform: evaluate_logic_waveform(config, waveform),
            timeout=config.timeout_s,
            spectre=spectre,
        )
    except MeasurementContractFailure as error:
        result = error.result
    return {
        "passed": result.passed,
        "run_id": result.run_id,
        "manifest": str(result.manifest_path),
        "measurements": str(result.measurements),
    }
