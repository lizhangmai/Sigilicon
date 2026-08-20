from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Callable
from pathlib import Path
import subprocess
from typing import Any

import pytest

from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation


def write_project_context(root: Path) -> Path:
    """Write the explicit caller-owned layout contract used by offline tests."""

    root.mkdir(parents=True, exist_ok=True)
    contract = root / "sigilicon.toml"
    contract.write_text(
        """schema = 1
contract_kind = "sigilicon-project"
path_scope = "repository"
owner = "test"

[paths]
project_root = "."
ip_root = "ip"
legacy_ip_root = "ip/legacy"
ip_config_dir = "configs"
config_root = "configs"
platform_root = "configs/platform"
workspace_root = "virtuoso"
artifact_root = "artifacts"
result_root = "artifacts"
""",
        encoding="utf-8",
    )
    return contract


@pytest.fixture(autouse=True)
def explicit_tmp_project_context(tmp_path: Path) -> None:
    write_project_context(tmp_path)


@pytest.fixture(autouse=True)
def forbid_real_eda_processes(monkeypatch):
    """Make every offline test fail before a real EDA executable can start."""

    original = subprocess.Popen
    forbidden = {
        "cdsTextTo5x",
        "spiceIn",
        "spectre",
        "virtuoso",
        "xrun",
    }

    def guarded(command, *args, **kwargs):
        first = command[0] if not isinstance(command, str) else command.split()[0]
        if Path(str(first)).name in forbidden:
            raise AssertionError(
                f"offline tests must mock EDA process launch: {first}"
            )
        return original(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded)


@pytest.fixture
def workspace_factory(monkeypatch, tmp_path):
    """Issue real short-lived capabilities without contacting Virtuoso."""

    root = tmp_path / "capability-workspace"
    root.mkdir(exist_ok=True)
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.virtuoso_workdir",
        lambda _client: root,
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.assert_no_active_maestro_sessions",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.require_project_library_path",
        lambda _operation, library: root / library,
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.open_cell_views",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.assert_cell_has_no_open_windows",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.assert_cell_has_no_open_views",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.require_clean_oa_cell",
        lambda *_args, **_kwargs: (),
    )

    @contextmanager
    def create(
        client: Any,
        *,
        name: str = "test-operation",
        library: str | None = None,
        policy: OperationPolicy = OperationPolicy.DIRECT_MUTATION,
    ):
        with workspace_operation(client, root, name, policy=policy) as operation:
            if library is None:
                yield operation
            else:
                with operation.view_lease(library, require_quiescent=False):
                    yield operation

    return create


@pytest.fixture
def project_factory(tmp_path: Path) -> Callable[..., tuple[Path, Path]]:
    def create(*, port_order: str = '"IN", "OUT", "VDD", "VSS"') -> tuple[Path, Path]:
        root = tmp_path / "project"
        write_project_context(root)
        design_dir = root / "ip/legacy" / "inv"
        pdk_dir = root / "configs" / "platform" / "testpdk"
        virtuoso_dir = root / "virtuoso"
        design_dir.mkdir(parents=True)
        pdk_dir.mkdir(parents=True)
        virtuoso_dir.mkdir(parents=True)
        (virtuoso_dir / "cds.lib").write_text("# test cds.lib\n", encoding="utf-8")
        model = root / "model.scs"
        model.write_text("// model\n", encoding="utf-8")
        (design_dir / "circuit.scs").write_text(
            """subckt inv IN OUT VDD VSS
MP0 (OUT IN VDD VDD) pch_mac l=30n w=200n
MN0 (OUT IN VSS VSS) nch_mac l=30n w=100n
ends inv
""",
            encoding="utf-8",
        )
        (pdk_dir / "pdk.toml").write_text(
            f"""name = "Test PDK"
technology_library = "techLib"
reference_libraries = ["deviceLib"]
model_file = "{model}"
model_section = "tt"
""",
            encoding="utf-8",
        )
        (design_dir / "design.toml").write_text(
            f"""[design]
library = "designLib"
cell = "inv"
source_netlist = "circuit.scs"
pdk = "testpdk"

[ports]
inputs = ["IN"]
outputs = ["OUT"]
supplies = ["VDD", "VSS"]
order = [{port_order}]

[ports.directions]
IN = "input"
OUT = "output"
VDD = "inputOutput"
VSS = "inputOutput"
""",
            encoding="utf-8",
        )
        spec = design_dir / "ams.toml"
        spec.write_text(
            """[test]
design = "design.toml"
testbench = "tb_inv"

[simulation.checker]
settle = "5ns"

[simulation.timing]
stop = "16n"
maxstep = "20p"

[simulation.interface]
vdd = 0.9
load_cap = "2f"
rise_time = "20p"
vthi = 0.5
vtlo = 0.3
connect_rules = "full"

[simulation.backends.standalone]
dump_vcd = true

[simulation.backends.ade]
errpreset = "conservative"

[[vectors]]
inputs = [0]
expected = [1]

[[vectors]]
inputs = [1]
expected = [0]
""",
            encoding="utf-8",
        )
        return root, spec

    return create
