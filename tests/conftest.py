from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Callable
from pathlib import Path
import subprocess
from typing import Any

import pytest

from sigilicon.execution._workspace import ExecutionWorkspace
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation


def write_file(
    path: Path,
    text: str = "fixture\n",
    *,
    executable: bool = False,
) -> Path:
    """Write one test-owned text file and optionally make it executable."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)
    return path


def managed_execution_workspace(root: Path) -> ExecutionWorkspace:
    """Return the canonical managed-run layout used by adapter tests."""

    run = root / "run"
    return ExecutionWorkspace(
        run_id="managed-run",
        root=run,
        input_root=run / "work/action/inputs",
        work_root=run / "work/action/tool",
        output_root=run / "outputs/action/evidence",
        log_root=run / "logs/action",
        source={},
    )


def write_project_context(root: Path) -> Path:
    """Write the explicit caller-owned layout contract used by offline tests."""

    root.mkdir(parents=True, exist_ok=True)
    contract = root / "sigilicon.toml"
    contract.write_text(
        f"""schema = 1
contract_kind = "sigilicon-project"
path_scope = "repository"
owner = "test"

[catalogs]
ip = "catalogs/ip.toml"
platform = "configs/platform/catalog.toml"

[paths]
project_root = "."
workspace_root = "virtuoso"
artifact_root = "artifacts"

[runtime.values]
"virtuoso-bridge.host" = "127.0.0.1"
"virtuoso-bridge.port" = "50051"

[runtime.destinations]
"release-store.fixture" = "{root / 'artifacts/release-store'}"
"release-store.fixture-ip" = "{root / 'artifacts/release-store'}"
"release-store.native-fixture" = "{root / 'artifacts/release-store'}"
"release-store.native-provider" = "{root / 'artifacts/release-store'}"
"release-store.rtl-fixture" = "{root / 'artifacts/release-store'}"
""",
        encoding="utf-8",
    )
    catalogs = root / "catalogs"
    catalogs.mkdir(exist_ok=True)
    (catalogs / "ip.toml").write_text(
        '''schema = 2
contract_kind = "ip-catalog"
path_scope = "repository"
owner = "test"

[components]
''',
        encoding="utf-8",
    )
    platform_root = root / "configs/platform"
    platform_root.mkdir(parents=True, exist_ok=True)
    (platform_root / "catalog.toml").write_text(
        '''schema = 1
contract_kind = "platform-catalog"
path_scope = "repository"
owner = "test"

[platforms]
''',
        encoding="utf-8",
    )
    return contract


def write_component_owner(
    root: Path,
    owner: str,
    *,
    filesets: dict[str, tuple[str, ...]],
) -> Path:
    """Catalog one test owner with explicit fileset inventory."""

    owner_root = root / "ip" / owner
    owner_root.mkdir(parents=True, exist_ok=True)
    component = owner_root / "component.toml"
    source_ids: dict[str, str] = {}
    for values in filesets.values():
        for value in values:
            source_ids.setdefault(value, f"source_{len(source_ids)}")
    source_lines = [
        f'{source_id} = "{path}"' for path, source_id in source_ids.items()
    ]
    fileset_lines: list[str] = []
    for name, values in filesets.items():
        rendered = ", ".join(f'"{source_ids[value]}"' for value in values)
        fileset_lines.append(f"{name} = [{rendered}]")
    component.write_text(
        f'''schema = 6
contract_kind = "ip-component"
path_scope = "owner"
owner = "{owner}"
root = "ip/{owner}"

name = "{owner}"
kind = "rtl-ip"

[sources]
{chr(10).join(source_lines)}

[filesets]
{chr(10).join(fileset_lines)}
''',
        encoding="utf-8",
    )
    catalog = root / "catalogs/ip.toml"
    source = catalog.read_text(encoding="utf-8")
    catalog.write_text(
        source
        + f'''\n[components.{owner}]
contract = "ip/{owner}/component.toml"
''',
        encoding="utf-8",
    )
    return component


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
owner = "{key}"

name = "Test PDK"

[contracts]
simulation = "simulation.toml"
oa = "oa.toml"
''',
        encoding="utf-8",
    )
    (platform / "simulation.toml").write_text(
        f'''schema = 1
contract_kind = "platform-simulation"
path_scope = "platform"
owner = "{key}"

default_model_set = "nominal"

[model_sets.nominal]
file = "model.scs"
sections = ["tt"]
''',
        encoding="utf-8",
    )
    (platform / "oa.toml").write_text(
        f'''schema = 1
contract_kind = "platform-oa"
path_scope = "platform"
owner = "{key}"

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
        f'''schema = 2
contract_kind = "platform-layout"
path_scope = "platform"
owner = "{key}"
dbu_per_micron = 1000
layermap = "layermap"
''',
        encoding="utf-8",
    )
    (platform / "verification.toml").write_text(
        f'''schema = 2
contract_kind = "platform-verification"
path_scope = "platform"
owner = "{key}"
qrc_tech_file = "qrc.tech"
[drc]
deck = "drc.deck"
[[drc.substitutions]]
match = "test"
replacement = "fixture ${{primary}} ${{layout_path}} ${{results_path}} ${{summary_path}}"
count = 1
[lvs]
deck = "lvs.deck"
[[lvs.substitutions]]
match = "test"
replacement = "fixture ${{primary}} ${{layout_path}} ${{source_path}} ${{work_dir}}"
count = 1
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
        "calibre",
        "spiceIn",
        "spectre",
        "strmout",
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
            f"""schema = 1
contract_kind = "cell-design"
path_scope = "cell"
owner = "example"

[design]
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


class FixtureAdapter:
    """Test process boundaries without inter-step artifact inputs."""
    def contract(self, project, step):
        from sigilicon.execution.artifact_reference import StepContract
        return StepContract()
