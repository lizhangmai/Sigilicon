from __future__ import annotations

from pathlib import Path

import pytest

from sigilicon.domain.oa_simulation import load_oa_simulation_spec
from sigilicon.virtuoso.ade import _native_setup_entry_point
from sigilicon.workflows.oa_simulation import _elaborated_netlist
from conftest import write_component_owner, write_project_context, write_test_platform


def _write_native_simulation_spec(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "project"
    write_project_context(root)
    owner = root / "ip/compute/verification/oa/tb_native"
    owner.mkdir(parents=True)
    write_test_platform(root)
    (owner / "setup.il").write_text(
        "procedure(fixtureNativeConfig(lib cell dut sourceView refs) t)\n"
        "procedure(fixtureNativeMaestro(session lib cell modelFile modelSection) t)\n",
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
config_procedure = "fixtureNativeConfig"
maestro_procedure = "fixtureNativeMaestro"
""",
        encoding="utf-8",
    )
    write_component_owner(
        root,
        "compute",
        filesets={
            "verification": (
                "ip/compute/verification/oa/tb_native/simulation.toml",
                "ip/compute/verification/oa/tb_native/setup.il",
            ),
        },
    )
    return root, spec


def test_elaborated_netlist_selects_one_completed_history_file(
    tmp_path: Path,
) -> None:
    history = "ExplorerRORun.0.RO"
    netlist = tmp_path / history / "1" / "tran_main" / "netlist" / "netlist.vams"
    netlist.parent.mkdir(parents=True)
    netlist.write_text("module DUT; endmodule\n", encoding="utf-8")

    assert _elaborated_netlist(tmp_path, history) == netlist

    history_copy = tmp_path / history / "psf" / "tran_main" / "netlist" / "netlist.vams"
    history_copy.parent.mkdir(parents=True)
    history_copy.write_text("module DUT; endmodule\n", encoding="utf-8")
    assert _elaborated_netlist(tmp_path, history) == netlist

    conflicting = tmp_path / history / "2" / "tran_main" / "netlist" / "netlist.vams"
    conflicting.parent.mkdir(parents=True)
    conflicting.write_text("different\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="did not resolve one final netlist"):
        _elaborated_netlist(tmp_path, history)


def test_elaborated_netlist_accepts_unique_spectre_history_file(
    tmp_path: Path,
) -> None:
    netlist = tmp_path / "ExplorerRORun.0.RO/1/tran_main/netlist/spectre.inp"
    netlist.parent.mkdir(parents=True)
    netlist.write_text("simulator lang=spectre\n", encoding="utf-8")
    history_copy = tmp_path / "psf" / "tran_main" / "netlist" / "spectre.inp"
    history_copy.parent.mkdir(parents=True)
    history_copy.write_text("simulator lang=spectre\n", encoding="utf-8")
    (history_copy.parent / "netlist").write_text(
        "protected config map\n", encoding="utf-8"
    )

    assert _elaborated_netlist(tmp_path, "ExplorerRORun.0.RO") == netlist


def test_elaborated_netlist_prefers_ams_design_over_spectre_config_map(
    tmp_path: Path,
) -> None:
    netlist_dir = tmp_path / "ExplorerRORun.0.RO/1/tran_main/netlist"
    netlist_dir.mkdir(parents=True)
    (netlist_dir / "netlist.vams").write_text(
        "module DUT; endmodule\n", encoding="utf-8"
    )
    (netlist_dir / "spectre.inp").write_text(
        "simulator lang=spectre\n", encoding="utf-8"
    )

    assert _elaborated_netlist(tmp_path, "ExplorerRORun.0.RO") == (
        netlist_dir / "netlist.vams"
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


def test_native_loader_rejects_unsupported_schema(tmp_path: Path) -> None:
    root, spec_path = _write_native_simulation_spec(tmp_path)
    spec_path.write_text(
        spec_path.read_text(encoding="utf-8").replace("schema = 3", "schema = 2"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="schema must be exactly 3"):
        load_oa_simulation_spec(spec_path, project_root=root)


def test_native_rdb_identity_contract_is_source_owned_and_setup_consistent(
    tmp_path: Path,
) -> None:
    root, spec_path = _write_native_simulation_spec(tmp_path)
    spec_path.parent.joinpath("setup.il").write_text(
        """procedure(fixtureNativeConfig(lib cell dut sourceView refs) t)
procedure(fixtureNativeMaestro(session lib cell modelFile modelSection)
  maeCreateTest("tran_main")
  maeAddOutput("out_wave" "tran_main" ?signalName "/OUT")
  maeAddOutput("out_scalar" "tran_main"
        ?expr "value(VT(\\"/OUT\\") 1u)")
  axlPutCorner(sdb "tt")
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

""",
        encoding="utf-8",
    )

    spec = load_oa_simulation_spec(spec_path, project_root=root)

    contract = spec.native_setup.rdb_contract
    assert contract is not None
    assert contract.scalar_names == ("out_scalar",)
    assert contract.expected_expression_count == 1


def _write_local_diagnostic_processor(path: Path) -> None:
    path.write_text(
        '''from sigilicon.domain.native_diagnostics import NativeDiagnosticContract


def load_contract(raw, *, contract_path, project_root):
    if raw != {"kind": "fixture"}:
        raise ValueError("unexpected fixture diagnostic")
    return NativeDiagnosticContract(
        kind="fixture",
        settings={},
        scalar_outputs=(("diag_value", 'value(VT("/OUT") 1u)'),),
    )


def validate_contract(diagnostic, *, point_count):
    if point_count != 1:
        raise ValueError("fixture expects one point")


def validate_source(diagnostic, setup_text):
    return ()


def nullable_scalar_names(diagnostic):
    return ()


def reconstruct(result, contract):
    return {"passed": True}


def attestation_requirements(diagnostic, tests):
    return {}
''',
        encoding="utf-8",
    )


def test_native_rdb_selects_a_testbench_local_diagnostic_processor(
    tmp_path: Path,
) -> None:
    root, spec_path = _write_native_simulation_spec(tmp_path)
    spec_path.parent.joinpath("setup.il").write_text(
        '''procedure(fixtureNativeConfig(lib cell dut sourceView refs) t)
procedure(fixtureNativeMaestro(session lib cell modelFile modelSection)
  maeCreateTest("tran_main")
  maeAddOutput("out_wave" "tran_main" ?signalName "/OUT")
  axlPutCorner(sdb "tt")
)
''',
        encoding="utf-8",
    )
    processor = spec_path.parent / "native_diagnostics.py"
    _write_local_diagnostic_processor(processor)
    owner_processor = root / "ip/compute/verification/native_diagnostics.py"
    owner_processor.parent.mkdir(parents=True, exist_ok=True)
    _write_local_diagnostic_processor(owner_processor)
    owner_processor.write_text(
        owner_processor.read_text(encoding="utf-8").replace(
            "diag_value", "owner_diag_value"
        ),
        encoding="utf-8",
    )
    component = root / "ip/compute/component.toml"
    component.write_text(
        component.read_text(encoding="utf-8")
        + 'native_diagnostics = ["ip/compute/verification/native_diagnostics.py"]\n',
        encoding="utf-8",
    )
    (spec_path.parent / "native_rdb.toml").write_text(
        '''schema = 2
point_count = 1
corners = ["tt"]
tests = ["tran_main"]
diagnostic_processor = "native_diagnostics.py"
waveforms = [{ name = "out_wave", signal = "/OUT" }]
scalars = []

[diagnostic_equivalence]
kind = "fixture"
''',
        encoding="utf-8",
    )

    spec = load_oa_simulation_spec(spec_path, project_root=root)

    contract = spec.native_setup.rdb_contract
    assert contract is not None
    assert contract.diagnostic_processor is not None
    assert contract.diagnostic_processor.source == processor.resolve()
    assert contract.diagnostic_scalar_names == ("diag_value",)
    assert processor.resolve() in contract.support_sources
    assert owner_processor.resolve() not in contract.support_sources


def test_native_rdb_rejects_a_nonlocal_diagnostic_processor(tmp_path: Path) -> None:
    root, spec_path = _write_native_simulation_spec(tmp_path)
    (spec_path.parent / "native_rdb.toml").write_text(
        '''schema = 2
point_count = 1
corners = ["tt"]
tests = ["tran_main"]
diagnostic_processor = "../native_diagnostics.py"
waveforms = [{ name = "out_wave", signal = "/OUT" }]
scalars = []

[diagnostic_equivalence]
kind = "fixture"
''',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="testbench-local Python filename"):
        load_oa_simulation_spec(spec_path, project_root=root)


def test_native_rdb_rejects_an_unused_diagnostic_processor(tmp_path: Path) -> None:
    root, spec_path = _write_native_simulation_spec(tmp_path)
    _write_local_diagnostic_processor(spec_path.parent / "native_diagnostics.py")
    (spec_path.parent / "native_rdb.toml").write_text(
        '''schema = 2
point_count = 1
corners = ["tt"]
tests = ["tran_main"]
diagnostic_processor = "native_diagnostics.py"
waveforms = [{ name = "out_wave", signal = "/OUT" }]
scalars = []
''',
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="diagnostic_processor requires diagnostic_equivalence",
    ):
        load_oa_simulation_spec(spec_path, project_root=root)


def test_native_rdb_requires_local_processor_for_diagnostic_equivalence(
    tmp_path: Path,
) -> None:
    root, spec_path = _write_native_simulation_spec(tmp_path)
    (spec_path.parent / "native_rdb.toml").write_text(
        '''schema = 2
point_count = 1
corners = ["tt"]
tests = ["tran_main"]
waveforms = [{ name = "out_wave", signal = "/OUT" }]
scalars = []

[diagnostic_equivalence]
kind = "fixture"
''',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="testbench-local diagnostic_processor"):
        load_oa_simulation_spec(spec_path, project_root=root)


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
        '''procedure(fixtureNativeConfig(lib cell dut sourceView refs) t)
procedure(fixtureNativeMaestro(session lib cell modelFile modelSection)
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
        '''procedure(fixtureNativeConfig(lib cell dut sourceView refs) t)
procedure(fixtureNativeMaestro(session lib cell modelFile modelSection)
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
        "procedure(fixtureNativeConfig(lib cell dut sourceView refs) t)\n",
        encoding="utf-8",
    )
    assert _native_setup_entry_point(source, declared="fixtureNativeConfig") == (
        "fixtureNativeConfig"
    )

    source.write_text("procedure(fixturePilotConfig(lib cell dut refs) t)\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not define declared entry point"):
        _native_setup_entry_point(source, declared="fixtureNativeConfig")
