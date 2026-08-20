from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

import pytest

from sigilicon.artifacts import ArtifactRecord, load_manifest
from sigilicon.domain.design import load_design_spec
from sigilicon.paths import ProjectContext
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
        self.info = SimpleNamespace(path=path, technology_library=kwargs["technology_library"])
        return self.info

    def get(self, _name, **_kwargs):
        return self.info


class FakeSchematic:
    def __init__(self) -> None:
        self.calls = []

    def import_netlist(self, *args, **kwargs):
        self.calls.append((args, kwargs))


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
        self.fingerprint = ""

    def execute_skill(self, source, **_kwargs):
        self.skill.append(source)
        fingerprint_match = re.search(
            r'dbReplaceProp\(cv "flowDesignFingerprint" "string" "([0-9a-f]{64})"\)',
            source,
        )
        if fingerprint_match is not None:
            self.fingerprint = fingerprint_match.group(1)
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
        if 'sprintf(nil "F|%s|%L' in source:
            rows = []
            for view in ("schematic", "symbol"):
                rows.extend(
                    (
                        f"F|{view}|{self.fingerprint}",
                        f"T|{view}|IN|input",
                        f"T|{view}|OUT|output",
                        f"T|{view}|VDD|inputOutput",
                        f"T|{view}|VSS|inputOutput",
                    )
                )
            return Response("\n".join(rows))
        if "actualFingerprint = cv~>flowDesignFingerprint" in source:
            return Response(self.fingerprint)
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
                f"{library}/{cell}/{view}\n", encoding="utf-8"
            )

    def generate_view(client, library, cell, **kwargs):
        client.symbol.generate_from_schematic(library, cell, **kwargs)
        directory = client.workdir / library / cell / "symbol"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "data.dm").write_text(
            f"{library}/{cell}/symbol\n", encoding="utf-8"
        )

    monkeypatch.setattr(
        "sigilicon.virtuoso.importer._import_netlist",
        import_view,
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.importer._generate_symbol",
        generate_view,
    )


def _import_artifact(tmp_path: Path, identity: str = "1" * 32) -> ArtifactRecord:
    return ArtifactRecord.begin(
        ProjectContext.from_project_root(tmp_path).artifacts.import_attempt(
            "designLib", "source", identity
        ),
        entities={"library": "designLib", "source": "source"},
        operation="test-import",
        backend="offline",
    )


def test_sync_design_consumes_source_and_pdk_config(
    monkeypatch, project_factory
) -> None:
    root, path = project_factory()
    spec = SimpleNamespace(design=load_design_spec(path, project_root=root))
    client = FakeClient(root / "virtuoso")
    _patch_fake_import(monkeypatch)

    result = sync_design(spec.design, client, overwrite=True)

    name, library_path, create_kwargs = client.library.create_call
    assert name == "designLib"
    assert library_path == str(root / "virtuoso" / "designLib")
    assert create_kwargs["technology_library"] == "techLib"
    import_args, import_kwargs = client.schematic.calls[0]
    assert import_args[2].read_text(encoding="utf-8") == spec.design.netlist_snapshot.text
    assert import_kwargs["ref_libs"] == ("designLib", "deviceLib", "analogLib", "basic")
    assert import_kwargs["dev_map_file"].read_text(encoding="utf-8") == (
        "devselect := resistor res\n"
        "devselect := capacitor cap\n"
    )
    assert result.imported_cells == ("inv",)
    assert result.attempt_dir.parent.name == "attempts"
    assert result.attempt_dir.parent.parent.name == "sync"
    manifest = load_manifest(result.manifest_path)
    assert manifest["status"] == "succeeded"
    assert set(manifest["details"]["oa_view_sha256"]["inv"]) == {
        "netlist",
        "schematic",
        "symbol",
    }
    assert "DEFINE designLib ./designLib" in (root / "virtuoso" / "cds.lib").read_text()
    assert any("term~>direction" in source for source in client.skill)


def test_target_only_sync_reuses_bridge_import_without_touching_cds_lib(
    monkeypatch, project_factory
) -> None:
    root, path = project_factory()
    spec = SimpleNamespace(design=load_design_spec(path, project_root=root))
    client = FakeClient(root / "virtuoso")
    library_path = root / "virtuoso" / "designLib"
    library_path.mkdir()
    client.library.info = SimpleNamespace(
        path=str(library_path), technology_library="techLib"
    )
    _patch_fake_import(monkeypatch)
    before_cds_lib = (root / "virtuoso" / "cds.lib").read_text(encoding="utf-8")

    result = sync_existing_design_target_only(spec.design, client, overwrite=True)

    assert client.library.create_call is None
    assert result.imported_cells == ("inv",)
    assert result.library_path == library_path
    assert result.technology_library == "techLib"
    assert load_manifest(result.manifest_path)["status"] == "succeeded"
    assert (root / "virtuoso" / "cds.lib").read_text(encoding="utf-8") == before_cds_lib
    import_args, import_kwargs = client.schematic.calls[0]
    assert import_args[:2] == ("designLib", "inv")
    assert import_kwargs["ref_libs"] == (
        "designLib",
        "deviceLib",
        "analogLib",
        "basic",
    )
    assert import_kwargs["dev_map_file"].read_text(encoding="utf-8") == (
        "devselect := resistor res\n"
        "devselect := capacitor cap\n"
    )


def test_target_only_sync_rejects_multicell_source_before_oa_mutation(
    project_factory,
) -> None:
    root, path = project_factory()
    spec = SimpleNamespace(design=load_design_spec(path, project_root=root))
    spec.design.source_netlist.write_text(
        """subckt leaf A Y\nends leaf\nsubckt inv IN OUT VDD VSS\n"
        "MP0 (OUT IN VDD VDD) pch_mac l=30n w=200n\n"
        "MN0 (OUT IN VSS VSS) nch_mac l=30n w=100n\n"
        "ends inv\n""",
        encoding="utf-8",
    )
    multi = load_design_spec(spec.design.path, project_root=root)
    client = FakeClient(root / "virtuoso")

    with pytest.raises(ValueError, match="exactly one canonical subckt"):
        sync_existing_design_target_only(multi, client)

    assert client.library.create_call is None


def test_real_spec_to_sync_chain_reads_and_parses_netlist_once(
    monkeypatch,
    project_factory,
) -> None:
    import sigilicon.domain.design as design_domain

    root, path = project_factory()
    original = design_domain.load_netlist_snapshot
    calls = 0

    def load_once(source):
        nonlocal calls
        calls += 1
        return original(source)

    monkeypatch.setattr(design_domain, "load_netlist_snapshot", load_once)
    spec = SimpleNamespace(design=load_design_spec(path, project_root=root))
    client = FakeClient(root / "virtuoso")
    _patch_fake_import(monkeypatch)

    sync_design(spec.design, client, overwrite=True)

    assert calls == 1


def test_import_hierarchy_passes_explicit_device_map(
    monkeypatch, project_factory, tmp_path, workspace_factory
) -> None:
    from sigilicon.workflows.hierarchy_import import import_hierarchy, plan_hierarchy

    root, path = project_factory()
    spec = SimpleNamespace(design=load_design_spec(path, project_root=root))
    client = FakeClient(root / "virtuoso")
    _patch_fake_import(monkeypatch)
    device_map = tmp_path / "spiceIn.devmap"
    device_map.write_text("devselect := capacitor cap\n", encoding="utf-8")

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
            dev_map_file=device_map,
            overwrite=True,
            artifact=_import_artifact(tmp_path),
            source_role="source",
            work_role="cells",
            timeout=30,
            operation=operation,
        )

    assert client.schematic.calls[0][1]["dev_map_file"] == device_map


def test_import_hierarchy_preserves_unowned_leaked_handle(
    monkeypatch, project_factory, tmp_path, workspace_factory
) -> None:
    from sigilicon.workflows.hierarchy_import import (
        HierarchyImportError,
        import_hierarchy,
        plan_hierarchy,
    )

    root, path = project_factory()
    spec = SimpleNamespace(design=load_design_spec(path, project_root=root))
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
                source_role="source",
                work_role="cells",
                timeout=30,
                operation=operation,
            )

    assert operation is not None
    assert operation.uncertain_reason is not None


def test_import_conversion_cleanup_uses_exact_handle_identity() -> None:
    from sigilicon.virtuoso.bridge import (
        schematic_import_netlist_skill,
    )
    from sigilicon.virtuoso.importer import _protect_import_conversion_skill

    source = schematic_import_netlist_skill(
        "lib",
        "cell",
        overwrite=True,
    )
    protected = _protect_import_conversion_skill(
        source,
        library="lib",
        cell="cell",
    )

    assert "flowBeforeViews = dbGetOpenCellViews()" in protected
    assert "member(flowCv flowBeforeViews)" in protected
    assert (
        "vbConnBeforeViews = dbGetOpenCellViews() unwindProtect(progn(vbConnOk"
        in protected
    )
    assert "member(vbConnCv vbConnBeforeViews)" in protected
    assert "vbConnCloseAttempt = errset(dbClose(vbConnCv) t)" in protected
    assert "vbConnCloseAttempt && car(vbConnCloseAttempt)" in protected
    assert "conn2Sch exact handle close failed" in protected
    assert "vbTempOpenBeforeViews = dbGetOpenCellViews()" in protected
    assert "member(vbTempOpenCv vbTempOpenBeforeViews)" in protected
    assert "equal(vbTempOpenCv vbTempCv)" in protected
    assert (
        "vbTempOpenCloseAttempt = errset(dbClose(vbTempOpenCv) t)"
        in protected
    )
    assert (
        "vbTempOpenCloseAttempt && car(vbTempOpenCloseAttempt)" in protected
    )
    assert "temporary schematic open exact handle close failed" in protected
    assert "vbCopyBeforeViews = dbGetOpenCellViews()" in protected
    assert "member(vbCopyCv vbCopyBeforeViews)" in protected
    assert "vbCopyCloseAttempt = errset(dbClose(vbCopyCv) t)" in protected
    assert "vbCopyCloseAttempt && car(vbCopyCloseAttempt)" in protected
    assert "dbCopyCellView exact handle close failed" in protected
    assert "unless(ddReleaseObj(vbNetlistObj)" in protected
    assert "source netlist DD handle release failed" in protected
    assert protected.count("unwindProtect(") >= 6
    assert "preserved exact dbIds" in protected
    assert "flowSyncBefore = dbGetOpenCellViews()" in protected
    assert "flowSyncCloseAttempt = errset(dbClose(flowSyncCv) t)" in protected
    assert "netlist import synchronous conversion" in protected


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


def test_owned_spicein_adapter_contract_is_fully_offline(
    monkeypatch,
    tmp_path,
) -> None:
    from sigilicon.virtuoso import importer

    class OfflineClient:
        ssh_runner = None

        def __init__(self) -> None:
            self.skill: list[str] = []
            self.library = SimpleNamespace(
                get=lambda name, **_kwargs: SimpleNamespace(path=tmp_path / name)
            )

        def execute_skill(self, source, **_kwargs):
            self.skill.append(source)
            if source.startswith("list(getWorkingDir()"):
                return {
                    "status": "success",
                    "output": f'(\"{tmp_path}\" \"\" \"\" \"\" \"\" \"/tools\")',
                }
            return {"status": "success", "output": "t"}

    netlist = tmp_path / "owned.scs"
    netlist.write_text("subckt cell a b\nends cell\n", encoding="utf-8")
    netlist.chmod(0o444)
    dev_map = tmp_path / "device.map"
    dev_map.write_text("devMap := nil\n", encoding="utf-8")
    (tmp_path / "cds.lib").write_text("# project\n", encoding="utf-8")
    for library in ("lib", "analogLib", "basic"):
        (tmp_path / library).mkdir()
    run_dir = tmp_path / "run"
    executable = tmp_path / "spiceIn"
    executable.write_text("offline test sentinel\n", encoding="utf-8")
    observed: dict[str, object] = {}
    phases: list[str] = []

    def run(command, **kwargs):
        proc_fd_prefix = f"/proc/{os.getpid()}/fd/"
        observed["command"] = tuple(command)
        observed["cwd"] = kwargs["cwd"]
        observed["pass_fds"] = kwargs["pass_fds"]
        kwargs["before_spawn"]()
        assert all(os.fstat(fd) for fd in kwargs["pass_fds"])
        parameter_text = Path(command[2]).read_text(encoding="utf-8")
        assert parameter_text.count(proc_fd_prefix) == 3
        staged_cds = (run_dir / "cds.lib").read_text(encoding="utf-8")
        assert [line.split()[1] for line in staged_cds.splitlines()] == [
            "lib",
            "analogLib",
            "basic",
        ]
        assert all(proc_fd_prefix in line for line in staged_cds.splitlines())
        log_match = re.search(
            rf"'logFile \"({re.escape(proc_fd_prefix)}\d+)\"",
            parameter_text,
        )
        assert log_match is not None
        Path(log_match.group(1)).write_text("owned log\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "stdout", "stderr")

    monkeypatch.setattr(importer.shutil, "which", lambda _name: str(executable))
    monkeypatch.setattr(importer, "run_process_group_capture", run)

    @contextmanager
    def own_netlist():
        descriptor = os.open(
            netlist,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    client = OfflineClient()
    importer._import_netlist(
        client,
        "lib",
        "cell",
        netlist,
        operation=SimpleNamespace(
            root=tmp_path,
            require_active_mutation=lambda *_args, **kwargs: phases.append(
                kwargs["phase"]
            ),
        ),
        own_netlist=own_netlist,
        language="Spectre",
        ref_libs=("lib", "analogLib", "basic"),
        overwrite=False,
        dev_map_file=dev_map,
        run_dir=run_dir,
        timeout=12,
    )

    assert str(observed["cwd"]).startswith(f"/proc/{os.getpid()}/fd/")
    assert (run_dir / "spiceIn.log").read_text(encoding="utf-8") == "owned log\n"
    assert (run_dir / "spiceIn.stdout").read_text(
        encoding="utf-8"
    ) == "stdoutstderr"
    assert any("conn2Sch(" in source for source in client.skill)
    assert "unwindProtect(" in client.skill[0]
    assert client.skill[0].count("ddReleaseObj(") == 2
    assert phases == [
        "spiceIn target-view preflight",
        "spiceIn process launch",
        "conn2Sch conversion",
    ]


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
        {"status": "partial", "errors": []},
        SimpleNamespace(status="failure", errors=[], ok=False),
        SimpleNamespace(status=None, errors=[], ok=None),
    ):
        with pytest.raises(RuntimeError, match="unconfirmed bridge status"):
            _require_skill_result(result, "import confirmation")

    success = SimpleNamespace(status="success", errors=[], ok=True)
    assert _require_skill_result(success, "import confirmation") is success


def test_symbol_generation_cleanup_uses_exact_handle_identity() -> None:
    from sigilicon.virtuoso.importer import _SymbolCleanupClient

    client = FakeClient(Path("/workspace/virtuoso"))
    scoped = _SymbolCleanupClient(
        client,
        operation=SimpleNamespace(
            require_active_mutation=lambda *_args, **_kwargs: None
        ),
        library="lib",
        cell="cell",
    )
    scoped.execute_skill('schSchemToPinList("lib" "cell" "schematic")')

    source = client.skill[-1]
    assert "flowBeforeViews = dbGetOpenCellViews()" in source
    assert "member(flowCv flowBeforeViews)" in source
    assert "preserved exact dbIds" in source


def test_sync_refuses_an_existing_library_with_wrong_technology(project_factory) -> None:
    root, path = project_factory()
    spec = SimpleNamespace(design=load_design_spec(path, project_root=root))
    client = FakeClient(root / "virtuoso")
    library_path = root / "virtuoso" / "designLib"
    library_path.mkdir()
    client.library.info = SimpleNamespace(path=str(library_path), technology_library="wrongTech")

    with pytest.raises(RuntimeError, match="wrongTech"):
        sync_design(spec.design, client, overwrite=True)


def test_sync_refuses_to_overwrite_an_open_cell(project_factory) -> None:
    root, path = project_factory()
    spec = SimpleNamespace(design=load_design_spec(path, project_root=root))
    client = FakeClient(root / "virtuoso")
    client.list_windows = lambda: [{"name": "Schematic Editing: designLib inv schematic"}]

    with pytest.raises(RuntimeError, match="open Virtuoso windows"):
        sync_design(spec.design, client, overwrite=True)


def test_port_direction_write_rechecks_quiescence_after_hierarchy(
    monkeypatch,
    project_factory,
) -> None:
    root, path = project_factory()
    spec = SimpleNamespace(design=load_design_spec(path, project_root=root))
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
