from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import write_test_platform
from sigilicon.project import Project
from sigilicon.domain.platform import load_platform
from sigilicon.virtuoso.models import OaModelInputs

from sigilicon.virtuoso.bridge import decode_skill_output

from sigilicon.virtuoso.attestation import (
    compare_native_setup_attestation_output,
    attest_native_setup,
)


def _spec() -> SimpleNamespace:
    contract = SimpleNamespace(
        tests=("tran_main",),
        corners=("tt",),
        waveform_outputs=(("wave", "/OUT"),),
        scalar_outputs=(("scalar", 'value(VT("/OUT") 1u)'),),
        setup_model_identities=(),
        diagnostic_equivalence=None,
        diagnostic_program=None,
    )
    return SimpleNamespace(
        library="fixture_lib",
        cell="tb_main",
        dut="dut",
        top_view="schematic",
        simulator="spectre",
        native_setup=SimpleNamespace(
            rdb_contract=contract,
            pdk=SimpleNamespace(
                simulation=SimpleNamespace(
                    default=SimpleNamespace(
                        file=Path("toplevel.scs"), single_section="top_tt"
                    )
                )
            ),
        ),
    )


def test_bridge_quoted_attestation_fixture_decodes_before_row_parsing() -> None:
    fixture = Path(__file__).parent / "fixtures" / "oa_native_attestation_output.txt"
    decoded = decode_skill_output(fixture.read_text(encoding="utf-8"))
    result = compare_native_setup_attestation_output(_spec(), decoded)
    observations = result["observations"]

    assert result["passed"] is True
    assert observations["outputs"][1]["expression"] == 'value(VT("/OUT") 1u)'


def test_attestation_comparison_covers_setup_and_result_identity() -> None:
    result = compare_native_setup_attestation_output(
        _spec(),
        "\n".join(
            (
                "CONFIG|fixture_lib|tb_main|config|fixture_lib|tb_main|schematic",
                "BIND||DUT0|fixture_lib|dut|schematic|true|true|true|fixture_lib|dut|schematic|1",
                "TEST|tran_main|fixture_lib|tb_main|config|spectre|active|$AXL",
                "ANALYSIS|tran_main|tran|tran",
                "ENV|tran_main|modelFiles|((toplevel.scs top_tt))",
                "OUTPUT|tran_main|wave|net|/OUT|||true|false|undefined",
                'OUTPUT|tran_main|scalar|point||value(VT("/OUT") 1u)||true|true|undefined',
                "MODEL|tt|toplevel.scs|/pdk/toplevel.scs|top_tt",
                "SPEC_OVERALL|tran_main|undefined",
                "PERSISTENCE|tests|1|setup=(tran_main)",
                "SESSION|opened|fnxSession1",
                "SESSION|closed|nil",
            )
        ),
    )

    assert result["passed"] is True
    assert all(result["checks"].values())
    assert result["mismatches"] == []
    assert result["diagnostics"]["calculator_scalars"]["missing"] == []


def test_attestation_uses_explicit_nondefault_setup_model_identity() -> None:
    spec = _spec()
    spec.native_setup.rdb_contract.setup_model_identities = (
        ("local_models.scs", "local_mos"),
    )
    result = compare_native_setup_attestation_output(
        spec,
        "\n".join(
            (
                "CONFIG|fixture_lib|tb_main|config|fixture_lib|tb_main|schematic",
                "BIND||DUT0|fixture_lib|dut|schematic|true|true|true|fixture_lib|dut|schematic|1",
                "TEST|tran_main|fixture_lib|tb_main|config|spectre|active|$AXL",
                "ANALYSIS|tran_main|tran|tran",
                "ENV|tran_main|modelFiles|((local_models.scs local_mos))",
                "OUTPUT|tran_main|wave|net|/OUT|||true|false|undefined",
                'OUTPUT|tran_main|scalar|point||value(VT("/OUT") 1u)||true|true|undefined',
                "MODEL|tt|local_mos|/pdk/local_models.scs|local_mos",
                "SPEC_OVERALL|tran_main|undefined",
                "PERSISTENCE|tests|1|setup=(tran_main)",
                "SESSION|opened|fnxSession1",
                "SESSION|closed|nil",
            )
        ),
    )

    assert result["passed"] is True
    assert result["diagnostics"]["model_file_section"]["expected"] == [
        ["local_models.scs", "local_mos"]
    ]


def test_attestation_rejects_unexpected_test_environment_model_section() -> None:
    fixture = Path(__file__).parent / "fixtures" / "oa_native_attestation_output.txt"
    transcript = decode_skill_output(fixture.read_text(encoding="utf-8"))
    result = compare_native_setup_attestation_output(
        _spec(), transcript + "\nTEST_MODEL|tran_main|/pdk/toplevel.scs|top_ff\n"
    )

    assert result["passed"] is False
    assert result["checks"]["model_file_section"] is False
    assert result["observations"]["test_models"][0]["section"] == "top_ff"


def test_attestation_diagnostics_identify_the_changed_result_contract_field() -> None:
    result = compare_native_setup_attestation_output(
        _spec(),
        "\n".join(
            (
                "CONFIG|fixture_lib|tb_main|config|fixture_lib|tb_main|schematic",
                "BIND||DUT0|fixture_lib|dut|schematic|true|true|true|fixture_lib|dut|schematic|1",
                "TEST|tran_main|fixture_lib|tb_main|config|spectre|active|$AXL",
                "ANALYSIS|tran_main|tran|tran",
                "ENV|tran_main|modelFiles|((toplevel.scs top_tt))",
                "OUTPUT|tran_main|wave|net|/WRONG|||true|false|undefined",
                'OUTPUT|tran_main|scalar|point||value(VT("/OUT") 1u)||true|true|undefined',
                "MODEL|tt|toplevel.scs|/pdk/toplevel.scs|top_tt",
                "SPEC_OVERALL|tran_main|undefined",
                "PERSISTENCE|tests|1|setup=(tran_main)",
                "SESSION|opened|fnxSession1",
                "SESSION|closed|nil",
            )
        ),
    )

    assert result["passed"] is False
    assert result["checks"]["waveform_outputs"] is False
    assert result["diagnostics"]["waveform_outputs"]["missing"] == [["wave", "/OUT"]]
    assert [item["check"] for item in result["mismatches"]] == ["waveform_outputs"]


def test_attestation_marks_failed_hdb_cleanup_uncertain(tmp_path, workspace_factory):
    write_test_platform(tmp_path)
    models = load_platform(Project.open(tmp_path), "testpdk").simulation.default
    inputs = OaModelInputs.capture(models, {path: path for path in models.paths})
    client = SimpleNamespace(execute_skill=lambda *_args, **_kwargs: SimpleNamespace(
        output="", errors=["HDB preflight close uncertain; preserved exact scope handle"]
    ))
    with pytest.raises(RuntimeError, match="HDB preflight close uncertain"):
        with workspace_factory(client, library="fixture_lib") as operation:
            attest_native_setup(_spec(), client, operation=operation, model_inputs=inputs)
    assert operation.uncertain_reason is not None
    assert "HDB/Maestro cleanup" in operation.uncertain_reason
