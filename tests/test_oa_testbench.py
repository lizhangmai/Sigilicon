from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

from sigilicon.domain.netlist import NetlistSnapshot
from sigilicon.domain.oa_simulation import OANativeSetup, OASimulationSpec
from sigilicon.domain.repository import Project
from sigilicon.domain.source import TextSourceSnapshot
from sigilicon.workflows.oa_testbench import _sync_oa_testbench_impl
from conftest import write_project_context


class _Work:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, role: str, *parts: str) -> Path:
        path = self.root / role / Path(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_text(self, role: str, parts: tuple[str, ...], text: str) -> Path:
        path = self.path(role, *parts)
        path.write_text(text, encoding="utf-8")
        return path

    def directory(self, role: str, *parts: str) -> Path:
        path = self.root / role / Path(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path


class _Operation:
    def __init__(self, library_path: Path) -> None:
        self.library_path = library_path

    def view_lease(self, *_args, **_kwargs):
        return nullcontext()

    def mutation_scope(self, *_args, **_kwargs):
        return nullcontext()

    def require_project_library_target(self, *_args, **_kwargs) -> Path:
        return self.library_path


def test_testbench_schematic_is_finalized_after_all_generated_views(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    write_project_context(project_root)
    library_path = project_root / "virtuoso" / "lib"
    library_path.mkdir(parents=True)
    setup = tmp_path / "setup.il"
    setup.write_text("; source-owned setup\n", encoding="utf-8")
    canonical = tmp_path / "testbench.scs"
    canonical.write_text("subckt tb VSS\nends tb\n", encoding="utf-8")
    project = Project.from_project_root(project_root)
    setup_snapshot = TextSourceSnapshot(
        source_path=setup.resolve(),
        text=setup.read_text(encoding="utf-8"),
    )
    spec = OASimulationSpec(
        path=(tmp_path / "simulation.toml").resolve(),
        project=project,
        library="lib",
        cell="tb",
        dut="dut",
        top_view="schematic",
        simulator="spectre",
        native_setup=OANativeSetup(
            pdk=SimpleNamespace(
                oa=SimpleNamespace(reference_libraries=())
            ),
            source_snapshot=setup_snapshot,
            config_procedure="fixtureConfig",
            maestro_procedure="fixtureMaestro",
        ),
    )
    snapshot = NetlistSnapshot(
        source_path=canonical.resolve(),
        text=canonical.read_text(encoding="utf-8"),
        interfaces=MappingProxyType({"tb": ("VSS",)}),
    )
    materialized = SimpleNamespace(path=canonical, open_fd=lambda: nullcontext(3))
    operation = _Operation(library_path)
    client = SimpleNamespace(
        library=SimpleNamespace(list=lambda timeout: ["lib"]),
    )
    events: list[str] = []
    adapter_setup_texts: list[str] = []
    measurement_texts: list[str] = []

    monkeypatch.setattr(
        "sigilicon.workflows.oa_testbench.load_netlist_snapshot",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("plan-owned netlist snapshot must not be reloaded")
        ),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_testbench.plan_hierarchy",
        lambda *_args, **_kwargs: SimpleNamespace(ordered_cells=("tb",)),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_testbench.materialize_netlist_snapshot",
        lambda *_args, **_kwargs: materialized,
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_testbench.workspace_operation",
        lambda *_args, **_kwargs: nullcontext(operation),
    )
    monkeypatch.setattr("sigilicon.workflows.oa_testbench.cell_exists", lambda *_args: False)
    monkeypatch.setattr(
        "sigilicon.workflows.oa_testbench.cell_view_exists", lambda *_args: True
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_testbench.import_schematic",
        lambda *_args, **_kwargs: events.append("schematic"),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_testbench._materialize_inline_pwl_tables",
        lambda *_args, **_kwargs: events.append("pwl"),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_testbench.create_oa_native_config_view",
        lambda _client, adapter_spec, **_kwargs: (
            adapter_setup_texts.append(
                adapter_spec.native_setup.source_snapshot.text
            ),
            events.append("config"),
        ),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_testbench.import_oa_text_view",
        lambda *_args, **kwargs: (
            measurement_texts.append(kwargs["source"].text),
            events.append("measurement"),
        ),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_testbench.create_oa_native_maestro_view",
        lambda *_args, **_kwargs: events.append("maestro"),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_testbench.check_and_save_schematic",
        lambda *_args, **_kwargs: events.append("final-check-and-save"),
    )
    setup.write_text("; changed after planning\n", encoding="utf-8")

    _sync_oa_testbench_impl(
        spec,
        snapshot,
        client,
        overwrite=False,
        _work=_Work(tmp_path / "work"),
    )

    assert events == [
        "schematic",
        "pwl",
        "config",
        "measurement",
        "maestro",
        "final-check-and-save",
    ]
    assert adapter_setup_texts == ["; source-owned setup\n"]
    assert measurement_texts == ["; source-owned setup\n"]
