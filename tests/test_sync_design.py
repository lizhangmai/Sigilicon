from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from virtuoso_bridge import ExecutionStatus, VirtuosoResult

from sigilicon.artifacts import RunRecord
from sigilicon.domain.design import load_design_spec
from sigilicon.project import Project
from sigilicon.paths import ArtifactLayout
from sigilicon.virtuoso.workspace import OperationPolicy
from sigilicon.workflows.design_sync import (
    sync_design,
    sync_existing_design_target_only,
)


class Response:
    def __init__(self, output: str = "t") -> None:
        self.output = output
        self.errors: list[str] = []


class FakeLibrary:
    def __init__(self) -> None:
        self.info = None
        self.create_call = None

    def list(self, **_kwargs):
        return [] if self.info is None else ["designLib"]

    def create(self, name, path, **kwargs):
        self.create_call = (name, path, kwargs)
        self.info = SimpleNamespace(
            path=path,
            technology_library=kwargs["technology_library"],
        )
        return self.info

    def get(self, _name, **_kwargs):
        return self.info


class FakeSchematic:
    def __init__(self) -> None:
        self.calls = []
        self.netlist_texts: list[str] = []
        self.device_map_texts: list[str] = []

    def import_netlist(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.netlist_texts.append(args[2].read_text(encoding="utf-8"))
        device_map = kwargs.get("dev_map_file")
        if device_map is not None:
            self.device_map_texts.append(device_map.read_text(encoding="utf-8"))


class FakeSymbol:
    def __init__(self) -> None:
        self.calls = []

    def generate_from_schematic(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class FakeClient:
    def __init__(self, workdir) -> None:
        self.library = FakeLibrary()
        self.schematic = FakeSchematic()
        self.symbol = FakeSymbol()
        self.skill = []
        self.workdir = workdir
        self.ssh_runner = None

    def execute_skill(self, source, **_kwargs):
        self.skill.append(source)
        if source == "getWorkingDir()":
            return Response(f'"{self.workdir}"')
        if source == "maeGetSessions()":
            return Response("nil")
        if source == "ipcGetPid()":
            return Response("1234")
        if "setof(cv dbGetOpenCellViews()" in source:
            return Response("nil")
        if "foreach(cv dbGetOpenCellViews()" in source:
            return Response('""')
        if "count = 0" in source and "dbFindOpenCellViewByName" in source:
            return Response("0")
        if 'sprintf(nil "T|%s|%s|%s' in source:
            rows = []
            for view in ("schematic", "symbol"):
                rows.extend(
                    (
                        f"T|{view}|IN|input",
                        f"T|{view}|OUT|output",
                        f"T|{view}|VDD|inputOutput",
                        f"T|{view}|VSS|inputOutput",
                    )
                )
            return Response("\n".join(rows))
        return Response()

    def list_windows(self):
        return []


def _patch_fake_import(monkeypatch) -> None:
    def import_view(client, library, cell, netlist, **kwargs):
        client.schematic.import_netlist(library, cell, netlist, **kwargs)
        for view in ("netlist", "schematic"):
            directory = client.workdir / library / cell / view
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "data.dm").write_text(
                f"{library}/{cell}/{view}\n",
                encoding="utf-8",
            )

    def generate_view(client, library, cell, **kwargs):
        client.symbol.generate_from_schematic(library, cell, **kwargs)
        directory = client.workdir / library / cell / "symbol"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "data.dm").write_text(
            f"{library}/{cell}/symbol\n",
            encoding="utf-8",
        )

    monkeypatch.setattr("sigilicon.virtuoso.importer._import_netlist", import_view)
    monkeypatch.setattr("sigilicon.virtuoso.importer._generate_symbol", generate_view)


def _import_artifact(tmp_path: Path, identity: str = "1" * 32) -> RunRecord:
    return RunRecord.begin(
        ArtifactLayout(tmp_path / "artifacts").operation_run(
            owner="designLib",
            operation="netlist-import",
            variant="hierarchy",
            run_id=identity,
        ),
        backend="offline",
    )


def test_sync_design_consumes_source_and_pdk_config(
    monkeypatch,
    project_factory,
) -> None:
    root, path = project_factory()
    spec = SimpleNamespace(
        design=load_design_spec(path, project=Project.open(root))
    )
    client = FakeClient(root / "virtuoso")
    _patch_fake_import(monkeypatch)

    result = sync_design(spec.design, client, overwrite=True)

    name, library_path, create_kwargs = client.library.create_call
    assert name == "designLib"
    assert library_path == str(root / "virtuoso" / "designLib")
    assert create_kwargs["technology_library"] == "techLib"
    import_args, import_kwargs = client.schematic.calls[0]
    assert client.schematic.netlist_texts == [spec.design.netlist_snapshot.text]
    assert import_kwargs["ref_libs"] == (
        "designLib",
        "deviceLib",
        "analogLib",
        "basic",
    )
    assert client.schematic.device_map_texts == [
        "devselect := resistor res\n"
        "devselect := capacitor cap\n"
    ]
    assert result.imported_cells == ("inv",)
    assert "DEFINE designLib ./designLib" in (
        root / "virtuoso" / "cds.lib"
    ).read_text()
    assert any("term~>direction" in source for source in client.skill)


def test_target_only_sync_reuses_bridge_import_without_touching_cds_lib(
    monkeypatch,
    project_factory,
) -> None:
    root, path = project_factory()
    spec = SimpleNamespace(
        design=load_design_spec(path, project=Project.open(root))
    )
    client = FakeClient(root / "virtuoso")
    library_path = root / "virtuoso" / "designLib"
    library_path.mkdir()
    client.library.info = SimpleNamespace(
        path=str(library_path),
        technology_library="techLib",
    )
    _patch_fake_import(monkeypatch)
    before_cds_lib = (root / "virtuoso" / "cds.lib").read_text(encoding="utf-8")

    result = sync_existing_design_target_only(
        spec.design,
        client,
        design_source=spec.design.path,
        overwrite=True,
    )

    assert client.library.create_call is None
    assert result.imported_cells == ("inv",)
    assert result.library_path == library_path
    assert result.technology_library == "techLib"
    assert (root / "virtuoso" / "cds.lib").read_text(encoding="utf-8") == before_cds_lib
    import_args, _import_kwargs = client.schematic.calls[0]
    assert import_args[:2] == ("designLib", "inv")


def test_target_only_sync_rejects_multicell_source_before_oa_mutation(
    project_factory,
) -> None:
    root, path = project_factory()
    spec = SimpleNamespace(
        design=load_design_spec(path, project=Project.open(root))
    )
    spec.design.source_netlist.write_text(
        """subckt leaf A Y
ends leaf
subckt inv IN OUT VDD VSS
MP0 (OUT IN VDD VDD) pch_mac l=30n w=200n
MN0 (OUT IN VSS VSS) nch_mac l=30n w=100n
ends inv
""",
        encoding="utf-8",
    )
    multi = load_design_spec(
        spec.design.path,
        project=Project.open(root),
    )
    client = FakeClient(root / "virtuoso")

    with pytest.raises(ValueError, match="exactly one canonical subckt"):
        sync_existing_design_target_only(
            multi,
            client,
            design_source=multi.path,
        )
    assert client.library.create_call is None


def test_import_hierarchy_passes_explicit_device_map(
    monkeypatch,
    project_factory,
    tmp_path,
    workspace_factory,
) -> None:
    from sigilicon.workflows.hierarchy_import import import_hierarchy, plan_hierarchy

    root, path = project_factory()
    spec = SimpleNamespace(
        design=load_design_spec(path, project=Project.open(root))
    )
    client = FakeClient(root / "virtuoso")
    _patch_fake_import(monkeypatch)
    device_map = tmp_path / "spiceIn.devmap"
    device_map.write_text("devselect := capacitor cap\n", encoding="utf-8")

    with workspace_factory(
        client,
        library=spec.design.library,
        policy=OperationPolicy.RECURSIVE_OA,
    ) as operation:
        import_hierarchy(
            client,
            plan=plan_hierarchy(spec.design.netlist_snapshot, top=spec.design.cell),
            library=spec.design.library,
            reference_libraries=spec.design.pdk.oa.reference_libraries,
            dev_map_file=device_map,
            overwrite=True,
            artifact=_import_artifact(tmp_path),
            source_role="inputs",
            work_role="work",
            timeout=30,
            operation=operation,
        )

    assert client.schematic.calls[0][1]["dev_map_file"] == device_map


def test_import_hierarchy_preserves_unowned_leaked_handle(
    monkeypatch,
    project_factory,
    tmp_path,
    workspace_factory,
) -> None:
    from sigilicon.workflows.hierarchy_import import (
        HierarchyImportError,
        import_hierarchy,
        plan_hierarchy,
    )

    root, path = project_factory()
    spec = SimpleNamespace(
        design=load_design_spec(path, project=Project.open(root))
    )
    client = FakeClient(root / "virtuoso")
    _patch_fake_import(monkeypatch)
    inventories = 0
    original_execute = client.execute_skill

    def execute(source, **kwargs):
        nonlocal inventories
        if "setof(cv dbGetOpenCellViews()" in source:
            inventories += 1
            return Response("nil" if inventories == 1 else "(openView)")
        return original_execute(source, **kwargs)

    client.execute_skill = execute
    operation = None
    with pytest.raises(HierarchyImportError, match="hidden open database views"):
        with workspace_factory(
            client,
            library=spec.design.library,
            policy=OperationPolicy.RECURSIVE_OA,
        ) as operation:
            plan = plan_hierarchy(spec.design.netlist_snapshot, top=spec.design.cell)
            import_hierarchy(
                client,
                plan=plan,
                library=spec.design.library,
                reference_libraries=spec.design.pdk.oa.reference_libraries,
                overwrite=True,
                artifact=_import_artifact(tmp_path),
                source_role="inputs",
                work_role="work",
                timeout=30,
                operation=operation,
            )

    assert operation is not None
    assert operation.uncertain_reason is not None


def test_import_adapter_rejects_unowned_remote_process_launch(tmp_path) -> None:
    from sigilicon.virtuoso import importer

    with pytest.raises(RuntimeError, match="remote spiceIn is unsupported"):
        importer._import_netlist(
            SimpleNamespace(ssh_runner=object()),
            "lib",
            "cell",
            tmp_path / "cell.scs",
            operation=SimpleNamespace(
                require_active_mutation=lambda *_args, **_kwargs: None
            ),
            own_netlist=lambda: None,
            run_dir=tmp_path / "run",
            timeout=12,
        )


def test_spicein_preflight_failure_prevents_process_launch(
    monkeypatch,
    tmp_path,
) -> None:
    from sigilicon.virtuoso import importer

    executable = tmp_path / "spiceIn"
    executable.write_text("offline test sentinel\n", encoding="utf-8")
    launches: list[object] = []
    monkeypatch.setattr(importer.shutil, "which", lambda _name: str(executable))
    monkeypatch.setattr(
        importer,
        "run_process_group_capture",
        lambda *args, **kwargs: launches.append((args, kwargs)),
    )
    client = SimpleNamespace(
        ssh_runner=None,
        execute_skill=lambda *_args, **_kwargs: SimpleNamespace(
            errors=["target schematic exists"]
        ),
    )

    with pytest.raises(RuntimeError, match="target schematic exists"):
        importer._import_netlist(
            client,
            "lib",
            "cell",
            tmp_path / "cell.scs",
            operation=SimpleNamespace(
                root=tmp_path,
                require_active_mutation=lambda *_args, **_kwargs: None,
            ),
            own_netlist=lambda: None,
            run_dir=tmp_path / "run",
            timeout=12,
        )
    assert launches == []


def test_import_skill_result_requires_explicit_success() -> None:
    from sigilicon.virtuoso.importer import _require_skill_result

    for result in (
        VirtuosoResult(status=ExecutionStatus.PARTIAL),
        VirtuosoResult(status=ExecutionStatus.FAILURE),
        VirtuosoResult(status=ExecutionStatus.ERROR),
    ):
        with pytest.raises(RuntimeError, match="unconfirmed bridge status"):
            _require_skill_result(result, "import confirmation")
    success = VirtuosoResult(status=ExecutionStatus.SUCCESS)
    assert _require_skill_result(success, "import confirmation") is success


def test_sync_refuses_an_existing_library_with_wrong_technology(project_factory) -> None:
    root, path = project_factory()
    spec = SimpleNamespace(
        design=load_design_spec(path, project=Project.open(root))
    )
    client = FakeClient(root / "virtuoso")
    library_path = root / "virtuoso" / "designLib"
    library_path.mkdir()
    client.library.info = SimpleNamespace(
        path=str(library_path),
        technology_library="wrongTech",
    )

    with pytest.raises(RuntimeError, match="wrongTech"):
        sync_design(spec.design, client, overwrite=True)


def test_sync_refuses_to_overwrite_an_open_cell(project_factory) -> None:
    root, path = project_factory()
    spec = SimpleNamespace(
        design=load_design_spec(path, project=Project.open(root))
    )
    client = FakeClient(root / "virtuoso")
    client.list_windows = lambda: [{"name": "Schematic Editing: designLib inv schematic"}]

    with pytest.raises(RuntimeError, match="open Virtuoso windows"):
        sync_design(spec.design, client, overwrite=True)


def test_port_direction_write_rechecks_quiescence_after_hierarchy(
    monkeypatch,
    project_factory,
) -> None:
    root, path = project_factory()
    spec = SimpleNamespace(
        design=load_design_spec(path, project=Project.open(root))
    )
    client = FakeClient(root / "virtuoso")
    _patch_fake_import(monkeypatch)
    direction_writes: list[str] = []
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.require_quiescent_project_cell",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("concurrent view before port update")
        ),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.design_sync.set_cell_port_directions",
        lambda *_args, **_kwargs: direction_writes.append("write"),
    )

    with pytest.raises(RuntimeError, match="concurrent view before port update"):
        sync_design(spec.design, client, overwrite=True)
    assert direction_writes == []
