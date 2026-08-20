from __future__ import annotations

from pathlib import Path

import pytest

from sigilicon.domain.oa_simulation import load_oa_simulation_spec, oa_simulation_fingerprint
from sigilicon.virtuoso.ade import _native_setup_entry_point
from sigilicon.workflows.oa_simulation import _elaborated_netlist_fingerprint
from conftest import write_project_context, write_test_platform


def _write_native_simulation_spec(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "project"
    write_project_context(root)
    owner = root / "ip/compute/verification/oa/tb_native"
    owner.mkdir(parents=True)
    write_test_platform(root)
    (owner / "setup.il").write_text(
        "procedure(llmCimNativeConfig(lib cell dut sourceView refs) t)\n"
        "procedure(llmCimNativeMaestro(session lib cell modelFile modelSection) t)\n",
        encoding="utf-8",
    )
    spec = owner / "simulation.toml"
    spec.write_text(
        """schema = 3

[testbench]
library = "lib"
cell = "tb_native"
dut = "dut"
source_view = "schematic"
simulator = "spectre"

[platform]
pdk = "testpdk"

[setup]
source = "setup.il"
config_procedure = "llmCimNativeConfig"
maestro_procedure = "llmCimNativeMaestro"
""",
        encoding="utf-8",
    )
    return root, spec


def test_elaborated_netlist_fingerprint_requires_one_final_content(
    tmp_path: Path,
) -> None:
    netlist = tmp_path / "results" / "netlist" / "netlist.vams"
    netlist.parent.mkdir(parents=True)
    netlist.write_text("module DUT; endmodule\n", encoding="utf-8")

    assert _elaborated_netlist_fingerprint(tmp_path) == (
        "948dc7a5519832fa3c96b0fcf0def326cd70caff619758826d4082e3bc93301d"
    )

    history_copy = tmp_path / "history" / "netlist.vams"
    history_copy.parent.mkdir()
    history_copy.write_text("module DUT; endmodule\n", encoding="utf-8")
    assert _elaborated_netlist_fingerprint(tmp_path) == (
        "948dc7a5519832fa3c96b0fcf0def326cd70caff619758826d4082e3bc93301d"
    )

    conflicting = tmp_path / "other" / "netlist.vams"
    conflicting.parent.mkdir()
    conflicting.write_text("different\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="conflicting elaborated netlist"):
        _elaborated_netlist_fingerprint(tmp_path)


def test_elaborated_netlist_fingerprint_accepts_unique_spectre_content(
    tmp_path: Path,
) -> None:
    netlist = tmp_path / "groupRunDataDir" / "netlist" / "netlist"
    netlist.parent.mkdir(parents=True)
    netlist.write_text("simulator lang=spectre\n", encoding="utf-8")
    history_copy = tmp_path / "psf" / "tran_main" / "netlist" / "netlist"
    history_copy.parent.mkdir(parents=True)
    history_copy.write_text("internal netlist\n", encoding="utf-8")

    assert _elaborated_netlist_fingerprint(tmp_path) == (
        "050d088f04a34128775c0e282e9c9a8906363ff14fc7354b5df4833915d0b4af"
    )


def test_elaborated_netlist_fingerprint_prefers_ams_design_over_config_map(
    tmp_path: Path,
) -> None:
    netlist_dir = tmp_path / "groupRunDataDir" / "netlist"
    netlist_dir.mkdir(parents=True)
    (netlist_dir / "netlist.vams").write_text(
        "module DUT; endmodule\n", encoding="utf-8"
    )
    (netlist_dir / "netlist").write_text(
        "lib: llm_cim\ncell: tb_ams\nview: config\n", encoding="utf-8"
    )

    assert _elaborated_netlist_fingerprint(tmp_path) == (
        "948dc7a5519832fa3c96b0fcf0def326cd70caff619758826d4082e3bc93301d"
    )


def test_native_simulation_contract_is_thin_and_source_owned(tmp_path: Path) -> None:
    root, spec_path = _write_native_simulation_spec(tmp_path)

    spec = load_oa_simulation_spec(spec_path, project_root=root)

    assert spec.contract_schema == 3
    assert spec.native_setup.source.name == "setup.il"
    assert spec.native_setup.rdb_contract is None
    assert spec.simulator == "spectre"
    assert not hasattr(spec, "measurement_program")


def test_native_simulation_contract_rejects_maestro_schema_duplication(
    tmp_path: Path,
) -> None:
    root, spec_path = _write_native_simulation_spec(tmp_path)
    spec_path.write_text(
        spec_path.read_text(encoding="utf-8") + "\n[[analyses]]\nname = \"bad\"\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported fields"):
        load_oa_simulation_spec(spec_path, project_root=root)


def test_schema_two_mdl_contracts_are_rejected_by_native_loader(tmp_path: Path) -> None:
    root, spec_path = _write_native_simulation_spec(tmp_path)
    spec_path.write_text(
        spec_path.read_text(encoding="utf-8").replace("schema = 3", "schema = 2"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="schema must be 3"):
        load_oa_simulation_spec(spec_path, project_root=root)


def test_native_rdb_identity_contract_is_source_owned_and_setup_consistent(
    tmp_path: Path,
) -> None:
    root, spec_path = _write_native_simulation_spec(tmp_path)
    spec_path.parent.joinpath("setup.il").write_text(
        """procedure(llmCimNativeConfig(lib cell dut sourceView refs) t)
procedure(llmCimNativeMaestro(session lib cell modelFile modelSection)
  maeCreateTest("tran_main")
  maeAddOutput("out_wave" "tran_main" ?signalName "/OUT")
  maeAddOutput("out_scalar" "tran_main"
        ?expr "value(VT(\\"/OUT\\") 1u)")
  axlPutCorner(sdb "tt")
  ;; legacySampleTimes legacySeries "1u" "out" "legacy_out"
  ;; "%s_%03d_%02d" "value(VT(\\"%s\\") %s)"
)
""",
        encoding="utf-8",
    )
    (spec_path.parent / "native_rdb.toml").write_text(
        """schema = 2
point_count = 1
corners = ["tt"]
tests = ["tran_main"]

[[waveforms]]
name = "out_wave"
signal = "/OUT"

[[scalars]]
name = "out_scalar"
expression = "value(VT(\\"/OUT\\") 1u)"

[legacy_equivalence]
alias = "samples"
sample_times = ["1u"]

[[legacy_equivalence.series]]
export = "out"
signals = ["/OUT"]
prefix = "legacy_out"
""",
        encoding="utf-8",
    )

    spec = load_oa_simulation_spec(spec_path, project_root=root)

    contract = spec.native_setup.rdb_contract
    assert contract is not None
    assert contract.scalar_names == ("out_scalar", "legacy_out_000_00")
    assert contract.expected_expression_count == 2
    assert contract.legacy_scalar_names == ("legacy_out_000_00",)


def test_native_rdb_contract_rejects_setup_identity_mismatch(tmp_path: Path) -> None:
    root, spec_path = _write_native_simulation_spec(tmp_path)
    (spec_path.parent / "native_rdb.toml").write_text(
        """schema = 2
point_count = 1
corners = ["tt"]
tests = ["tran_wrong"]

[[waveforms]]
name = "out_wave"
signal = "/OUT"

[[scalars]]
name = "out_scalar"
expression = "value(VT(\\"/OUT\\") 1u)"
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not consistent with setup.il"):
        load_oa_simulation_spec(spec_path, project_root=root)


def test_native_rdb_contract_can_audit_nondefault_setup_model_identity(
    tmp_path: Path,
) -> None:
    root, spec_path = _write_native_simulation_spec(tmp_path)
    simulation = root / "configs/platform/testpdk/simulation.toml"
    simulation.write_text(
        simulation.read_text(encoding="utf-8")
        + '''

[model_sets.local]
file = "local_models.scs"
sections = ["local_mos"]
''',
        encoding="utf-8",
    )
    spec_path.parent.joinpath("setup.il").write_text(
        '''procedure(llmCimNativeConfig(lib cell dut sourceView refs) t)
procedure(llmCimNativeMaestro(session lib cell modelFile modelSection)
  maeCreateTest("tran_main")
  maeAddOutput("out_wave" "tran_main" ?signalName "/OUT")
  maeAddOutput("out_scalar" "tran_main"
    ?expr "value(VT(\\\"/OUT\\\") 1u)")
  axlPutCorner(sdb "tt")
  mismatchModelFile = "/pdk/local_models.scs"
  modelSection = "local_mos"
)
''',
        encoding="utf-8",
    )
    (spec_path.parent / "native_rdb.toml").write_text(
        '''schema = 2
point_count = 1
corners = ["tt"]
tests = ["tran_main"]
waveforms = [{ name = "out_wave", signal = "/OUT" }]
scalars = [
  { name = "out_scalar", expression = "value(VT(\\\"/OUT\\\") 1u)" },
]

[setup_identity]
models = [
  { file = "local_models.scs", section = "local_mos" },
]
''',
        encoding="utf-8",
    )

    spec = load_oa_simulation_spec(spec_path, project_root=root)

    assert spec.native_setup.rdb_contract.setup_model_identities == (
        ("local_models.scs", "local_mos"),
    )


def test_native_rdb_contract_rejects_models_not_declared_by_platform(
    tmp_path: Path,
) -> None:
    root, spec_path = _write_native_simulation_spec(tmp_path)
    spec_path.parent.joinpath("setup.il").write_text(
        '''procedure(llmCimNativeConfig(lib cell dut sourceView refs) t)
procedure(llmCimNativeMaestro(session lib cell modelFile modelSection)
  maeCreateTest("tran_main")
  maeAddOutput("out_wave" "tran_main" ?signalName "/OUT")
  maeAddOutput("out_scalar" "tran_main"
    ?expr "value(VT(\\"/OUT\\") 1u)")
  axlPutCorner(sdb "tt")
  modelFile = "/pdk/unknown.scs"
  modelSection = "unknown_section"
)
''',
        encoding="utf-8",
    )
    (spec_path.parent / "native_rdb.toml").write_text(
        '''schema = 2
point_count = 1
corners = ["tt"]
tests = ["tran_main"]
waveforms = [{ name = "out_wave", signal = "/OUT" }]
scalars = [
  { name = "out_scalar", expression = "value(VT(\\"/OUT\\") 1u)" },
]

[setup_identity]
models = [
  { file = "unknown.scs", section = "unknown_section" },
]
''',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not declared by the selected platform"):
        load_oa_simulation_spec(spec_path, project_root=root)


def test_native_setup_entry_point_requires_the_declared_definition(tmp_path: Path) -> None:
    source = tmp_path / "setup.il"
    source.write_text(
        "procedure(llmCimNativeConfig(lib cell dut sourceView refs) t)\n",
        encoding="utf-8",
    )
    assert _native_setup_entry_point(source, declared="llmCimNativeConfig") == (
        "llmCimNativeConfig"
    )

    source.write_text("procedure(llmCimPilotConfig(lib cell dut refs) t)\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not define declared entry point"):
        _native_setup_entry_point(source, declared="llmCimNativeConfig")


def test_native_simulation_fingerprint_includes_setup_source(tmp_path: Path) -> None:
    root, spec_path = _write_native_simulation_spec(tmp_path)
    source = spec_path.parent / "testbench.scs"
    source.write_text("subckt tb_native OUT\nends tb_native\n", encoding="utf-8")
    spec = load_oa_simulation_spec(spec_path, project_root=root)
    first = oa_simulation_fingerprint(spec, source)
    spec.native_setup.source.write_text(
        "procedure(llmCimNativeConfig(lib cell dut sourceView refs) t)\n"
        "procedure(llmCimNativeMaestro(session lib cell modelFile modelSection) t)\n"
        "; changed\n",
        encoding="utf-8",
    )
    second = oa_simulation_fingerprint(spec, source)
    assert first != second
