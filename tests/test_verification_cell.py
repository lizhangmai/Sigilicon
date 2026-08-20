from pathlib import Path

import pytest

from sigilicon.domain.verification_cell import load_verification_cell


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _contract(root: Path) -> Path:
    cell = "ip/demo/verification/calibration/tb_demo"
    _write(root, f"{cell}/testbench.sv", "module tb_demo; endmodule\n")
    _write(root, "ip/demo/integration/dut.sv", "module dut; endmodule\n")
    return _write(
        root,
        f"{cell}/cell.toml",
        """schema = 1
contract_kind = "verification-cell"
path_scope = "cell"
owner = "demo"

cell = "tb_demo"
role = "rtl-testbench"
canonical_source = "testbench.sv"
dut = "dut"
simulator = "xcelium"
dependencies = ["../../../integration/dut.sv"]
""",
    )


def test_verification_cell_loads_its_owned_source_boundary(tmp_path: Path) -> None:
    contract = _contract(tmp_path)

    spec = load_verification_cell(contract, project_root=tmp_path)

    assert spec.cell == "tb_demo"
    assert spec.canonical_source == contract.parent / "testbench.sv"
    assert spec.dependencies == (tmp_path / "ip/demo/integration/dut.sv",)
    assert spec.contracts == ()
    assert spec.runner is None


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
        load_verification_cell(contract, project_root=tmp_path)


def test_verification_cell_rejects_unknown_fields(tmp_path: Path) -> None:
    contract = _contract(tmp_path)
    contract.write_text(
        contract.read_text(encoding="utf-8") + 'unexpected = "field"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="contains unknown fields"):
        load_verification_cell(contract, project_root=tmp_path)
