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

[project]
artifact_namespace = "test-project"

[catalogs]
ip = "catalogs/ip.toml"
soc = "catalogs/soc.toml"
platform = "configs/platform/catalog.toml"

[python]
owned_module_prefixes = ["soc."]

[paths]
project_root = "."
ip_root = "ip"
managed_ip_roots = ["ip/alpha", "ip/beta", "ip/compute"]
ip_config_dir = "configs"
config_root = "configs"
workspace_root = "virtuoso"
artifact_root = "artifacts"
result_root = "artifacts"
""",
        encoding="utf-8",
    )
    return contract


def write_test_platform(root: Path, key: str = "testpdk") -> Path:
    """Write a minimal cataloged simulation/OA platform for offline tests."""

    platform_root = root / "configs/platform"
    platform = platform_root / key
    platform.mkdir(parents=True, exist_ok=True)
    (platform_root / "catalog.toml").write_text(
        f'''schema = 1
contract_kind = "platform-catalog"
path_scope = "repository"
owner = "test"

[platforms]
{key} = "{key}/platform.toml"
''',
        encoding="utf-8",
    )
    (platform / "platform.toml").write_text(
        f'''schema = 1
contract_kind = "platform-definition"
path_scope = "platform"
owner = "test-platform"

key = "{key}"
name = "Test PDK"

[contracts]
simulation = "simulation.toml"
oa = "oa.toml"
''',
        encoding="utf-8",
    )
    (platform / "simulation.toml").write_text(
        '''schema = 1
contract_kind = "platform-simulation"
path_scope = "platform"
owner = "test-platform"

default_model_set = "nominal"

[model_sets.nominal]
file = "model.scs"
sections = ["tt"]
''',
        encoding="utf-8",
    )
    (platform / "oa.toml").write_text(
        '''schema = 1
contract_kind = "platform-oa"
path_scope = "platform"
owner = "test-platform"

technology_library = "techLib"
reference_libraries = ["deviceLib"]
''',
        encoding="utf-8",
    )
    model = platform / "model.scs"
    model.write_text("// model\n", encoding="utf-8")
    return model


def write_test_layout_platform(root: Path, key: str = "testpdk") -> None:
    """Extend the minimal platform with offline layout/verification contracts."""

    write_test_platform(root, key)
    platform = root / "configs/platform" / key
    manifest = platform / "platform.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + 'layout = "layout.toml"\nverification = "verification.toml"\n',
        encoding="utf-8",
    )
    (platform / "layout.toml").write_text(
        '''schema = 1
contract_kind = "platform-layout"
path_scope = "platform"
owner = "test-platform"
dbu_per_micron = 1000

[generation]
profile = "geometry.toml"

[generation.model_polarities]
nch = "nmos"

[generation.layers]
routing1 = "M1"
routing2 = "M2"
routing3 = "M3"
diffusion = "OD"
p_implant = "PP"
n_implant = "NP"
n_well = "NW"

[generation.vias.substrate_tap]
definition = "SUB"
landing_half_sizes = { routing1 = [1, 1] }

[generation.vias.well_tap]
definition = "WELL"
landing_half_sizes = { routing1 = [1, 1] }

[generation.vias.routing1_routing2]
definition = "V12"
landing_half_sizes = { routing1 = [1, 1], routing2 = [1, 1] }

[generation.vias.routing2_routing3]
definition = "V23"
landing_half_sizes = { routing2 = [1, 1], routing3 = [1, 1] }

[generation.mos_pcell]
length_parameter = "l"
width_parameter = "w"
gate_contact_selection_parameter = "gateContact"
gate_contact_parameters = []

[generation.mos_pcell.polarity_parameters]
nmos = []
pmos = []
''',
        encoding="utf-8",
    )
    (platform / "geometry.toml").write_text(
        '''schema = 1
contract_kind = "platform-layout-profile"
path_scope = "platform"
owner = "test-platform"

[placement]
template_bbox = [1, 1]
default_pitch = [1, 1]
routed_pitch = [1, 1]

[access]
wire_half_width = 1
source_offset_x = 1
drain_offset_x = 1
gate_offset = [1, 1]
gate_landing_half_size = [1, 1]
''',
        encoding="utf-8",
    )
    (platform / "verification.toml").write_text(
        '''schema = 1
contract_kind = "platform-verification"
path_scope = "platform"
owner = "test-platform"
layermap = "layermap"
drc_deck = "drc.deck"
lvs_deck = "lvs.deck"
qrc_tech_file = "qrc.tech"
''',
        encoding="utf-8",
    )
    for name in ("layermap", "drc.deck", "lvs.deck", "qrc.tech"):
        (platform / name).write_text("test\n", encoding="utf-8")


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
        design_dir = root / "ip/example" / "inv"
        virtuoso_dir = root / "virtuoso"
        design_dir.mkdir(parents=True)
        virtuoso_dir.mkdir(parents=True)
        (virtuoso_dir / "cds.lib").write_text("# test cds.lib\n", encoding="utf-8")
        write_test_platform(root)
        (design_dir / "circuit.scs").write_text(
            """subckt inv IN OUT VDD VSS
MP0 (OUT IN VDD VDD) pch_mac l=30n w=200n
MN0 (OUT IN VSS VSS) nch_mac l=30n w=100n
ends inv
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
        return root, design_dir / "design.toml"

    return create
