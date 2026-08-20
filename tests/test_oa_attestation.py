from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sigilicon.virtuoso.bridge import decode_skill_output

from sigilicon.virtuoso.attestation import (
    _parse_rows,
    _normalize_calculator_expression,
    build_native_setup_attestation_skill,
    compare_native_setup_attestation,
)


def _spec() -> SimpleNamespace:
    contract = SimpleNamespace(
        tests=("tran_main",),
        corners=("tt",),
        waveform_outputs=(("wave", "/OUT"),),
        scalar_outputs=(("scalar", 'value(VT("/OUT") 1u)'),),
    )
    return SimpleNamespace(
        library="llm_cim",
        cell="tb_main",
        dut="dut",
        top_view="schematic",
        simulator="spectre",
        native_setup=SimpleNamespace(
            rdb_contract=contract,
            pdk=SimpleNamespace(
                model_file=Path("toplevel.scs"), model_section="top_tt"
            ),
        ),
    )


def test_attestation_skill_uses_official_read_only_objects() -> None:
    skill = build_native_setup_attestation_skill("llm_cim", "tb_main")

    for api in (
        "hdbOpen",
        "pcdbGetInstMasterGen",
        "hdbBind",
        "maeOpenSetup",
        "axlGetTestToolArgs",
        "maeGetAnalysis",
        "maeGetEnvOption",
        "axlGetCorners",
        "axlGetModelFile",
        "maeGetTestOutputs",
        "maeGetSpecStatus",
        "maeGetOverallSpecStatus",
        "maeGetCurrentRunMode",
        "axlGetAllSweepsEnabled",
        "axlGetVars",
        "maeGetVar",
        "axlGetRunOptions",
        "axlGetRunOptionValue",
        "maeGetSessions",
    ):
        assert api in skill
    assert 'load("' not in skill
    assert '"w"' not in skill
    assert '"a"' not in skill
    assert "analysisOptions option envOptions" in skill


def test_attestation_skill_has_balanced_parentheses_and_strings() -> None:
    skill = build_native_setup_attestation_skill("llm_cim", "tb_main")
    depth = 0
    in_string = False
    escaped = False
    for character in skill:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            assert depth >= 0

    assert in_string is False
    assert depth == 0


def test_calculator_comparison_normalizes_cadence_numeric_spelling_only() -> None:
    source = 'ymax(clip(VT("/OUT") 643.2n 645n))-ymin(clip(VT("/OUT") 643.2n 645n))'
    cadence = '(ymax(clip(VT("/OUT") 6.432e-7 6.45e-7)) - ymin(clip(VT("/OUT") 6.432e-7 6.45e-7)))'

    assert _normalize_calculator_expression(source) == _normalize_calculator_expression(
        cadence
    )
    assert _normalize_calculator_expression(source) != _normalize_calculator_expression(
        source.replace("643.2n", "644n")
    )
    dynamic_source = (
        'value(VT("/D0") (cross(clip(VT("/RDY") 2n 3.6n) '
        '0.45 1 "rising" nil "time") + 20p))'
    )
    dynamic_cadence = (
        'value(VT("/D0") (cross(clip(VT("/RDY") 2e-9 3.6e-9) '
        '4.5e-1 1 "rising" nil "time") + 2e-11))'
    )
    assert _normalize_calculator_expression(
        dynamic_source
    ) == _normalize_calculator_expression(dynamic_cadence)


def test_attestation_rows_are_structured_without_python_setup_parsing() -> None:
    observations = _parse_rows(
        "\n".join(
            (
                "CONFIG|llm_cim|tb_main|config|llm_cim|tb_main|schematic",
                "BIND||DUT0|llm_cim|dut|schematic|true|true|true|llm_cim|dut|schematic|1",
                "TEST|tran_main|llm_cim|tb_main|config|spectre|active|$AXL",
                "ANALYSIS|tran_main|tran|tran",
                "ENV|tran_main|modelFiles|((toplevel.scs top_tt))",
                "OUTPUT|tran_main|wave|net|/OUT|||true|undefined",
                'OUTPUT|tran_main|scalar|point||value(VT("/OUT") 1u)||true|true|undefined',
                "MODEL|tt|toplevel.scs|/pdk/toplevel.scs|top_tt",
                "SPEC_OVERALL|tran_main|undefined",
                "SESSION|before|nil",
                "SESSION|opened|fnxSession1",
                "SESSION|closed|nil",
                "PERSISTENCE|tests|1|setup=(tran_main)",
            )
        )
    )

    assert observations["config"]["top_view"] == "schematic"
    assert observations["bindings"][0]["bound_cell"] == "dut"
    assert observations["outputs"][1]["expression"] == 'value(VT("/OUT") 1u)'
    assert observations["sessions"][-1]["state"] == "closed"


def test_bridge_quoted_attestation_fixture_decodes_before_row_parsing() -> None:
    fixture = Path(__file__).parent / "fixtures" / "oa_native_attestation_output.txt"
    decoded = decode_skill_output(fixture.read_text(encoding="utf-8"))
    observations = _parse_rows(decoded)

    result = compare_native_setup_attestation(_spec(), observations)

    assert result["passed"] is True
    assert observations["outputs"][1]["expression"] == 'value(VT("/OUT") 1u)'


def test_attestation_comparison_covers_setup_and_result_identity() -> None:
    observations = _parse_rows(
        "\n".join(
            (
                "CONFIG|llm_cim|tb_main|config|llm_cim|tb_main|schematic",
                "BIND||DUT0|llm_cim|dut|schematic|true|true|true|llm_cim|dut|schematic|1",
                "TEST|tran_main|llm_cim|tb_main|config|spectre|active|$AXL",
                "ANALYSIS|tran_main|tran|tran",
                "ENV|tran_main|modelFiles|((toplevel.scs top_tt))",
                "OUTPUT|tran_main|wave|net|/OUT|||true|undefined",
                'OUTPUT|tran_main|scalar|point||value(VT("/OUT") 1u)||true|true|undefined',
                "MODEL|tt|toplevel.scs|/pdk/toplevel.scs|top_tt",
                "SPEC_OVERALL|tran_main|undefined",
                "PERSISTENCE|tests|1|setup=(tran_main)",
                "SESSION|opened|fnxSession1",
                "SESSION|closed|nil",
            )
        )
    )

    result = compare_native_setup_attestation(_spec(), observations)

    assert result["passed"] is True
    assert all(result["checks"].values())
    assert result["mismatches"] == []
    assert result["diagnostics"]["calculator_scalars"]["missing"] == []


def test_attestation_uses_explicit_nondefault_setup_model_identity() -> None:
    spec = _spec()
    spec.native_setup.rdb_contract.setup_model_identities = (
        ("local_models.scs", "local_mos"),
    )
    observations = _parse_rows(
        "\n".join(
            (
                "CONFIG|llm_cim|tb_main|config|llm_cim|tb_main|schematic",
                "BIND||DUT0|llm_cim|dut|schematic|true|true|true|llm_cim|dut|schematic|1",
                "TEST|tran_main|llm_cim|tb_main|config|spectre|active|$AXL",
                "ANALYSIS|tran_main|tran|tran",
                "ENV|tran_main|modelFiles|((local_models.scs local_mos))",
                "OUTPUT|tran_main|wave|net|/OUT|||true|undefined",
                'OUTPUT|tran_main|scalar|point||value(VT("/OUT") 1u)||true|true|undefined',
                "MODEL|tt|local_mos|/pdk/local_models.scs|local_mos",
                "SPEC_OVERALL|tran_main|undefined",
                "PERSISTENCE|tests|1|setup=(tran_main)",
                "SESSION|opened|fnxSession1",
                "SESSION|closed|nil",
            )
        )
    )

    result = compare_native_setup_attestation(spec, observations)

    assert result["passed"] is True
    assert result["diagnostics"]["model_file_section"]["expected"] == [
        ["local_models.scs", "local_mos"]
    ]


def test_attestation_diagnostics_identify_the_changed_result_contract_field() -> None:
    observations = _parse_rows(
        "\n".join(
            (
                "CONFIG|llm_cim|tb_main|config|llm_cim|tb_main|schematic",
                "BIND||DUT0|llm_cim|dut|schematic|true|true|true|llm_cim|dut|schematic|1",
                "TEST|tran_main|llm_cim|tb_main|config|spectre|active|$AXL",
                "ANALYSIS|tran_main|tran|tran",
                "ENV|tran_main|modelFiles|((toplevel.scs top_tt))",
                "OUTPUT|tran_main|wave|net|/WRONG|||true|undefined",
                'OUTPUT|tran_main|scalar|point||value(VT("/OUT") 1u)||true|true|undefined',
                "MODEL|tt|toplevel.scs|/pdk/toplevel.scs|top_tt",
                "SPEC_OVERALL|tran_main|undefined",
                "PERSISTENCE|tests|1|setup=(tran_main)",
                "SESSION|opened|fnxSession1",
                "SESSION|closed|nil",
            )
        )
    )

    result = compare_native_setup_attestation(_spec(), observations)

    assert result["passed"] is False
    assert result["checks"]["waveform_outputs"] is False
    assert result["diagnostics"]["waveform_outputs"]["missing"] == [["wave", "/OUT"]]
    assert [item["check"] for item in result["mismatches"]] == ["waveform_outputs"]


def test_attestation_checks_bank_fixed_stimulus_and_monte_carlo_options() -> None:
    spec = _spec()
    spec.native_setup.rdb_contract.diagnostic_equivalence = SimpleNamespace(
        kind="bank_calibration_measurement",
        settings={
            "raw_half_span_v": 0.0125,
            "post_half_span_v": 0.0025,
            "monte_carlo_samples": 64,
            "model_file": "mismatch_models.scs",
            "model_sections": ("local_mos", "local_mom"),
            "transient_stop": "1.005205e-3",
            "transient_maxstep": "10u",
        },
    )
    observations = _parse_rows(
        "\n".join(
            (
                "CONFIG|llm_cim|tb_main|config|llm_cim|tb_main|schematic",
                "BIND||DUT0|llm_cim|dut|schematic|true|true|true|llm_cim|dut|schematic|1",
                "TEST|tran_main|llm_cim|tb_main|config|spectre|active|$AXL",
                "ANALYSIS|tran_main|tran|tran",
                "ANALYSIS_OPTION|tran_main|tran|stop|1.005205e-3",
                "ANALYSIS_OPTION|tran_main|tran|maxstep|1e-05",
                "ENV|tran_main|modelFiles|((mismatch_models.scs local_mos) (mismatch_models.scs local_mom))",
                "OUTPUT|tran_main|wave|net|/OUT|||true|undefined",
                'OUTPUT|tran_main|scalar|point||value(VT("/OUT") 1u)||true|true|undefined',
                "MODEL|tt|local_mos|/pdk/mismatch_models.scs|local_mos",
                "MODEL|tt|local_mom|/pdk/mismatch_models.scs|local_mom",
                "SPEC_OVERALL|tran_main|undefined",
                "RUNMODE|Monte Carlo Sampling",
                "SWEEPS_ENABLED|false",
                "RUNOPTION|Monte Carlo Sampling|mcmethod|mismatch",
                "RUNOPTION|Monte Carlo Sampling|mcnumpoints|64",
                "RUNOPTION|Monte Carlo Sampling|samplingmode|random",
                "RUNOPTION|Monte Carlo Sampling|donominal|0",
                "RUNOPTION|Monte Carlo Sampling|montecarloseed|20261101",
                "RUNOPTION|Monte Carlo Sampling|mcstartingrunnumber|1",
                "PERSISTENCE|tests|1|setup=(tran_main)",
                "SESSION|opened|fnxSession1",
                "SESSION|closed|nil",
            )
        )
    )

    result = compare_native_setup_attestation(spec, observations)

    assert result["passed"] is True
    assert result["checks"]["design_variables"] is True
    assert result["checks"]["run_mode"] is True
    assert result["checks"]["monte_carlo_options"] is True
    assert result["checks"]["analysis_options"] is True


def test_attestation_rejects_bank_transient_option_drift() -> None:
    spec = _spec()
    spec.native_setup.rdb_contract.diagnostic_equivalence = SimpleNamespace(
        kind="bank_calibration_measurement",
        settings={
            "raw_half_span_v": 0.0125,
            "post_half_span_v": 0.0025,
            "monte_carlo_samples": 64,
            "model_file": "mismatch_models.scs",
            "model_sections": ("local_mos", "local_mom"),
            "transient_stop": "1.005205e-3",
            "transient_maxstep": "10u",
        },
    )
    observations = _parse_rows(
        "\n".join(
            (
                "CONFIG|llm_cim|tb_main|config|llm_cim|tb_main|schematic",
                "BIND||DUT0|llm_cim|dut|schematic|true|true|true|llm_cim|dut|schematic|1",
                "TEST|tran_main|llm_cim|tb_main|config|spectre|active|$AXL",
                "ANALYSIS|tran_main|tran|tran",
                "ANALYSIS_OPTION|tran_main|tran|stop|1.005205e-3",
                "ANALYSIS_OPTION|tran_main|tran|maxstep|20u",
                "ENV|tran_main|modelFiles|((mismatch_models.scs local_mos) (mismatch_models.scs local_mom))",
                "OUTPUT|tran_main|wave|net|/OUT|||true|undefined",
                'OUTPUT|tran_main|scalar|point||value(VT("/OUT") 1u)||true|true|undefined',
                "MODEL|tt|local_mos|/pdk/mismatch_models.scs|local_mos",
                "MODEL|tt|local_mom|/pdk/mismatch_models.scs|local_mom",
                "SPEC_OVERALL|tran_main|undefined",
                "RUNMODE|Monte Carlo Sampling",
                "SWEEPS_ENABLED|false",
                "RUNOPTION|Monte Carlo Sampling|mcmethod|mismatch",
                "RUNOPTION|Monte Carlo Sampling|mcnumpoints|64",
                "RUNOPTION|Monte Carlo Sampling|samplingmode|random",
                "RUNOPTION|Monte Carlo Sampling|donominal|0",
                "RUNOPTION|Monte Carlo Sampling|montecarloseed|20261101",
                "RUNOPTION|Monte Carlo Sampling|mcstartingrunnumber|1",
                "PERSISTENCE|tests|1|setup=(tran_main)",
                "SESSION|opened|fnxSession1",
                "SESSION|closed|nil",
            )
        )
    )

    result = compare_native_setup_attestation(spec, observations)

    assert result["passed"] is False
    assert result["checks"]["analysis_options"] is False
    assert result["diagnostics"]["analysis_options"]["missing"] == [
        ["tran_main", "tran", "maxstep", 1.0e-05]
    ]
