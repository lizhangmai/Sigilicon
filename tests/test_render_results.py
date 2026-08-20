from __future__ import annotations

from sigilicon.ams.render import (
    render_analog_netlist,
    render_load_wrapper_netlist,
    render_reference_module,
    render_testbench,
)
from sigilicon.ams.provenance import ams_fingerprint
from sigilicon.ams.load_contract import validate_load_attestation
from sigilicon.ams.results import parse_results, validate_truth_contract
from sigilicon.ams.spec import load_ams_spec


def test_one_spec_renders_both_testbench_modes(project_factory) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)

    standalone = render_testbench(
        spec,
        dut_cell=spec.wrapper_cell,
        include_supplies=False,
        dump_vcd=True,
    )
    ade = render_testbench(
        spec,
        dut_cell=spec.wrapper_cell,
        include_supplies=False,
        dump_vcd=False,
    )

    assert "inv_ams XDUT" in standalone
    assert "$dumpfile" in standalone
    assert "inv_ams XDUT" in ade
    assert "supply1 VDD" not in ade
    assert "$fatal(1" in standalone and "$fatal(1" in ade
    assert f"FLOW_FINGERPRINT {ams_fingerprint(spec)}" in standalone


def test_analog_wrapper_includes_canonical_source_and_explicit_port_order(
    project_factory,
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)

    analog = render_analog_netlist(spec, reference_file="inv_ams_ref.v")

    assert f'include "{spec.design.source_netlist}"' in analog
    assert "XDUT (IN OUT VDD 0) inv" in analog
    assert "CLOAD0 (OUT 0) capacitor c=2f" in analog
    assert "module inv_ams (IN, OUT);" in render_reference_module(spec)


def test_standalone_and_ade_share_the_exact_load_wrapper(project_factory) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)

    standalone = render_analog_netlist(spec, reference_file="inv_ams_ref.v")
    ade_load = render_load_wrapper_netlist(spec)

    common = "CLOAD0 (OUT 0) capacitor c=2f"
    assert common in standalone
    assert common in ade_load
    assert spec.simulation.interface.load_cap == "2f"


def test_oa_load_attestation_enforces_standalone_ade_parity() -> None:
    observed = (
        {
            "instance": "CLOAD0",
            "library": "analogLib",
            "cell": "cap",
            "value": "2e-15",
            "nets": ("OUT", "gnd!"),
        },
    )

    attestation = validate_load_attestation(("OUT",), "2f", observed)

    assert attestation["load_cap_farads"] == "2E-15"
    assert attestation["instances"][0]["ground"] == "gnd!"

    import pytest

    with pytest.raises(RuntimeError, match="expected 3E-15"):
        validate_load_attestation(("OUT",), "3f", observed)


def test_truth_protocol_parses_rows_and_summary() -> None:
    text = """
TRUTH vector=0 inputs=0 expected=1 observed=1 pass=1
TRUTH vector=1 inputs=1 expected=0 observed=0 pass=1
SUMMARY vectors=2 failed=0
"""

    rows, summary = parse_results(text)

    assert [row["observed"] for row in rows] == ["1", "0"]
    assert summary == {"vectors": 2, "failed": 0}


def test_truth_contract_rejects_stale_generated_checker(project_factory) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    text = """
FLOW_FINGERPRINT aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
TRUTH vector=0 inputs=0 expected=1 observed=1 pass=1
TRUTH vector=1 inputs=1 expected=0 observed=0 pass=1
SUMMARY vectors=2 failed=0
"""
    rows, summary = parse_results(text)

    import pytest

    with pytest.raises(RuntimeError, match="stale"):
        validate_truth_contract(spec, text, rows, summary)
