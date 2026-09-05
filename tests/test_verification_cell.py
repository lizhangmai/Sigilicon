from pathlib import Path

import pytest

from sigilicon.domain.verification_cell import (
    load_verification_cell,
)
from sigilicon.project import Project

from conftest import write_component_owner


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _contract(root: Path) -> Path:
    cell = "ip/demo/verification/calibration/tb_demo"
    _write(root, f"{cell}/testbench.sv", "module tb_demo; endmodule\n")
    _write(root, "ip/demo/integration/dut.sv", "module dut; endmodule\n")
    contract = _write(
        root,
        f"{cell}/cell.toml",
        """schema = 2
contract_kind = "verification-cell"
path_scope = "cell"
owner = "demo"

cell = "tb_demo"
role = "rtl-testbench"
canonical_source = "testbench.sv"
dut = "dut"
simulator = "xcelium"
compile_sources = ["../../../integration/dut.sv"]
success_marker = "TB_DEMO_SUMMARY failures=0"
""",
    )
    write_component_owner(
        root,
        "demo",
        filesets={
            "verification": (
                f"{cell}/cell.toml",
                f"{cell}/testbench.sv",
                "ip/demo/integration/dut.sv",
            ),
        },
    )
    return contract


def test_verification_cell_loads_its_owned_source_boundary(tmp_path: Path) -> None:
    contract = _contract(tmp_path)
    project = Project.open(tmp_path)

    spec = load_verification_cell(contract, project=project)

    assert spec.cell == "tb_demo"
    assert spec.canonical_source == contract.parent / "testbench.sv"
    assert spec.compile_sources == (tmp_path / "ip/demo/integration/dut.sv",)
    assert spec.support_files == ()
    assert spec.contracts == ()
    assert spec.runner is None
    assert spec.success_marker == "TB_DEMO_SUMMARY failures=0"
    assert spec.repository.project_root == project.project_root
    assert spec.owner == "demo"


def test_verification_cell_preserves_declared_contract_documents(
    tmp_path: Path,
) -> None:
    contract = _contract(tmp_path)
    declared = _write(
        tmp_path,
        "ip/demo/configs/qualification.toml",
        '''schema = 1
contract_kind = "ip-qualification"
path_scope = "owner"
owner = "demo"
''',
    )
    contract.write_text(
        contract.read_text(encoding="utf-8")
        + 'contracts = ["../../../configs/qualification.toml"]\n',
        encoding="utf-8",
    )

    spec = load_verification_cell(contract, project=Project.open(tmp_path))

    assert set(spec.source_documents) == {contract.resolve(), declared.resolve()}
    with pytest.raises(TypeError):
        spec.source_documents[declared.resolve()]["owner"] = "other"


def test_verification_cell_rejects_canonical_source_outside_cell(tmp_path: Path) -> None:
    contract = _contract(tmp_path)
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            'canonical_source = "testbench.sv"',
            'canonical_source = "../../../integration/dut.sv"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="canonical_source must stay inside"):
        load_verification_cell(contract, project=Project.open(tmp_path))


def _as_xcelium_ams(contract: Path, *, stop: str = "1u") -> None:
    source = contract.read_text(encoding="utf-8").replace(
        'simulator = "xcelium"',
        'simulator = "xcelium-ams"',
    )
    contract.write_text(
        source
        + f'''
[ams]
platform = "testpdk"
model_set = "nominal"
transient_stop = "{stop}"
ie_voltage = 0.9

[ams.circuit]
kind = "ip-release"
contract = "../../../component.toml"
variant = "no-recovery"
fileset = "ams"
dependency = "native-provider"
view = "circuit_netlist"
''',
        encoding="utf-8",
    )


def test_verification_cell_loads_typed_xcelium_ams_inputs(tmp_path: Path) -> None:
    contract = _contract(tmp_path)
    _as_xcelium_ams(contract)

    spec = load_verification_cell(contract, project=Project.open(tmp_path))

    assert spec.ams is not None
    assert spec.ams.platform == "testpdk"
    assert spec.ams.circuit.contract == tmp_path / "ip/demo/component.toml"
    assert spec.ams.circuit.view == "circuit_netlist"
    assert spec.ams.transient_stop == "1u"
    assert spec.ams.ie_voltage == 0.9
    assert spec.ams.circuit.contract in spec.source_inputs
    assert spec.ams.circuit.contract in spec.source_documents


def test_verification_cell_accepts_a_standalone_ams_circuit_source(
    tmp_path: Path,
) -> None:
    contract = _contract(tmp_path)
    circuit = _write(
        tmp_path,
        "ip/demo/design/standalone.scs",
        "simulator lang=spectre\nsubckt ANALOG_TOP A VSS\nends ANALOG_TOP\n",
    )
    _as_xcelium_ams(contract)
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            '''kind = "ip-release"
contract = "../../../component.toml"
variant = "no-recovery"
fileset = "ams"
dependency = "native-provider"
view = "circuit_netlist"''',
            '''kind = "source"
path = "../../../design/standalone.scs"
cell = "ANALOG_TOP"''',
        ),
        encoding="utf-8",
    )

    spec = load_verification_cell(contract, project=Project.open(tmp_path))

    assert spec.ams is not None
    assert spec.ams.circuit.path == circuit.resolve()
    assert spec.ams.circuit.cell == "ANALOG_TOP"
    assert circuit.resolve() in spec.source_inputs
    assert circuit.resolve() not in spec.source_documents


def test_verification_cell_requires_ams_only_for_xcelium_ams(tmp_path: Path) -> None:
    missing = _contract(tmp_path)
    missing.write_text(
        missing.read_text(encoding="utf-8").replace(
            'simulator = "xcelium"',
            'simulator = "xcelium-ams"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must declare ams"):
        load_verification_cell(missing, project=Project.open(tmp_path))

    _as_xcelium_ams(missing)
    missing.write_text(
        missing.read_text(encoding="utf-8").replace(
            'simulator = "xcelium-ams"',
            'simulator = "xcelium"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="valid only"):
        load_verification_cell(missing, project=Project.open(tmp_path))


@pytest.mark.parametrize("stop", ["0", "0.0", "0.0u", "1u; alter"])
def test_verification_cell_rejects_unsafe_ams_stop_time(
    tmp_path: Path,
    stop: str,
) -> None:
    contract = _contract(tmp_path)
    _as_xcelium_ams(contract, stop=stop)

    with pytest.raises(ValueError, match="positive Spectre time token"):
        load_verification_cell(contract, project=Project.open(tmp_path))


def test_verification_cell_rejects_incomplete_ams_table(tmp_path: Path) -> None:
    contract = _contract(tmp_path)
    _as_xcelium_ams(contract)
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            'view = "circuit_netlist"\n',
            'unexpected = "value"\n',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="fields must be exactly"):
        load_verification_cell(contract, project=Project.open(tmp_path))
