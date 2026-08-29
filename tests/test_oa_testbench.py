from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from sigilicon.domain.repository import Project
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
    spec = SimpleNamespace(
        project=Project.from_project_root(project_root),
        project_root=project_root,
        library="lib",
        cell="tb",
        native_setup=SimpleNamespace(
            source=setup,
            pdk=SimpleNamespace(
                oa=SimpleNamespace(reference_libraries=())
            ),
        ),
    )
    snapshot = SimpleNamespace()
    materialized = SimpleNamespace(path=canonical, open_fd=lambda: nullcontext(3))
    operation = _Operation(library_path)
    client = SimpleNamespace(
        library=SimpleNamespace(list=lambda timeout: ["lib"]),
    )
    events: list[str] = []

    monkeypatch.setattr(
        "sigilicon.workflows.oa_testbench.load_netlist_snapshot", lambda _path: snapshot
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
        lambda *_args, **_kwargs: events.append("config"),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_testbench.import_oa_text_view",
        lambda *_args, **_kwargs: events.append("measurement"),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_testbench.create_oa_native_maestro_view",
        lambda *_args, **_kwargs: events.append("maestro"),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_testbench.check_and_save_schematic",
        lambda *_args, **_kwargs: events.append("final-check-and-save"),
    )

    _sync_oa_testbench_impl(
        spec,
        canonical,
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
