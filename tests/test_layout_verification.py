from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.adapters.mentor import physical_verification
from sigilicon.adapters.cadence import oa_export
import sys
from sigilicon.execution._workspace import ExecutionWorkspace
from sigilicon.execution._resources import Resources


def _drc_summary(*, violation_count: int = 0) -> str:
    return (
        "RULECHECK CONFIG_WARNING .... TOTAL Result Count = 2 (2)\n"
        f"RULECHECK M1.SPACING .... TOTAL Result Count = {violation_count} "
        f"({violation_count})\n"
        f"TOTAL DRC Results Generated: {violation_count + 2} "
        f"({violation_count + 2})\n"
        "LAYER SRAMDMY .... TOTAL Original Geometry Count = 0 (0)\n"
        "LAYER SRM_3 .... TOTAL Original Geometry Count = 0 (0)\n"
    )


def test_drc_parser_separates_warnings_violations_and_waiver_layers() -> None:
    clean = physical_verification.parse_drc_summary(
        _drc_summary(),
        configuration_warnings=("CONFIG_WARNING",),
        waiver_layers=("SRAMDMY", "SRM_3"),
    )
    violated = physical_verification.parse_drc_summary(
        _drc_summary(violation_count=3),
        configuration_warnings=("CONFIG_WARNING",),
        waiver_layers=("SRAMDMY", "SRM_3"),
    )

    assert clean["passed"] is True
    assert clean["configuration_warning_count"] == 2
    assert violated["passed"] is False
    assert violated["violation_count"] == 3


def test_lvs_parser_requires_the_exact_top_cell_comparison() -> None:
    assert physical_verification.parse_lvs_report(
        " CORRECT TOP TOP\n", primary="TOP"
    ) == {"passed": True, "comparison_result": "CORRECT"}
    assert physical_verification.parse_lvs_report(
        " INCORRECT TOP TOP\n", primary="TOP"
    ) == {"passed": False, "comparison_result": "INCORRECT"}
    with pytest.raises(RuntimeError, match="top-cell"):
        physical_verification.parse_lvs_report("CORRECT OTHER OTHER\n", primary="TOP")


def test_calibre_environment_uses_the_resource_snapshot(tmp_path: Path) -> None:
    executable = tmp_path / "calibre/bin/calibre"
    executable.parent.mkdir(parents=True)
    executable.write_text("fixture\n", encoding="utf-8")

    environment = physical_verification.calibre_environment(
        executable,
        {"PATH": "/snapshot/bin", "LM_LICENSE_FILE": "wrong", "MGLS_LICENSE_FILE": "ok"},
    )

    assert environment["PATH"] == "/snapshot/bin"
    assert "LM_LICENSE_FILE" not in environment
    assert environment["MGLS_LICENSE_FILE"] == "ok"
    assert environment["CALIBRE_HOME"] == str(executable.parent.parent)


@pytest.mark.parametrize("fault", [None, "mapping", "during-export"])
def test_oa_export_binds_before_lease_and_commits_stream(tmp_path: Path, monkeypatch, fault: str | None) -> None:
    from sigilicon.project import Project

    project = _manual_oa_project(tmp_path)
    plan = project.plan("example:export")
    bound = []

    class Operation:
        uncertain_reason = None

        def __init__(self, operation_id):
            self.operation_id = operation_id
            self.commits = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            if exc_type is None:
                for deferred in self.commits:
                    deferred.callback()
                    deferred.completed = True
            return False

        def register_artifact(self, record):
            bound.append(self)

        def view_lease(self, *_args, **_kwargs):
            assert bound == [self]
            return nullcontext()

        def require_project_library_target(self, *_args, **_kwargs):
            return project.workspace_root / ("another-library" if fault == "mapping" else "example")

        def defer_commit(self, callback):
            deferred = SimpleNamespace(callback=callback, completed=False)
            self.commits.append(deferred)
            return deferred

    client = SimpleNamespace(library=SimpleNamespace(get=lambda *_args, **_kwargs: SimpleNamespace(technology_library="techLib")))
    monkeypatch.setattr(oa_export, "workspace_operation", lambda *_args, **kwargs: Operation(kwargs["operation_id"]))
    monkeypatch.setattr("sigilicon.adapters.cadence.oa_client.get_client", lambda resources: client)
    gds = tmp_path / "layout.gds"
    gds.write_bytes(b"exported-stream")
    log = tmp_path / "strmout.log"
    log.write_text("offline export boundary\n")
    def export(*_args, **_kwargs):
        if fault == "during-export":
            (project.workspace_root / "cds.lib").write_text("DEFINE example ./another\n")
        return SimpleNamespace(gds_path=gds, stdout="", stderr="", native_log_path=log, summary_path=log)

    monkeypatch.setattr(oa_export, "run_xstream_export", export)
    if fault:
        with pytest.raises(RuntimeError, match="expected workspace directory|changed"):
            project.run(plan)
        return
    result = project.run(plan)
    assert result.status == "succeeded"
    stream = next(artifact for artifact in result.outcomes[0].result.artifacts if artifact.role == "layout-stream")
    assert stream.kind == "layout.gds"
    assert stream.read_bytes() == b"exported-stream"


def _verification_project(root: Path, *, origin: str, check: str = "drc") -> Path:
    from conftest import write_component_owner, write_test_layout_platform

    write_test_layout_platform(root)
    platform = root / "configs/platform/testpdk/platform.toml"
    platform.write_text(platform.read_text().split("[contracts]")[0] + '[contracts]\nverification = "verification.toml"\n')
    owner = root / "ip/example"
    owner.mkdir(parents=True)
    (owner / "layout.gds").write_bytes(b"source-stream")
    (owner / "source.cdl").write_text(".SUBCKT TOP a\n.ENDS TOP\n")
    (owner / "policy.toml").write_text('schema = 1\ncontract_kind = "physical-verification-policy"\npath_scope = "owner"\nowner = "example"\n[drc]\n')
    component = write_component_owner(root, "example", filesets={"verify": (
        "ip/example/policy.toml", "ip/example/layout.gds", "ip/example/source.cdl",
    )})
    component.write_text(component.read_text().replace('[sources]', 'operation_catalog = "operations"\n\n[sources]\noperations = "ip/example/operations.toml"'))
    prefix = '''schema = 5
contract_kind = "owner-operations"
path_scope = "owner"
owner = "example"

[operations.verify]
'''
    emit = '''[[operations.verify.steps]]
id = "export"
uses = "fake.stream"
filesets = [{ component = "example", fileset = "verify" }]
config = {}

''' if origin != "source" else ""
    selection = 'source = "layout.gds"' if origin == "source" else 'step = "export", role = "layout-stream"'
    (owner / "operations.toml").write_text(prefix + emit + f'''[[operations.verify.steps]]
id = "verify"
uses = "mentor.calibre"
needs = {['export'] if origin != 'source' else []}
filesets = [{{ component = "example", fileset = "verify" }}]
evidence = {{ role = "diagnostic", level = "l1", scope = "offline" }}
config = {{ owner = "example", cell = "TOP", check = "{check}", platform = "testpdk", policy = "policy.toml", layout = {{ {selection} }}, source = {{ source = "source.cdl" }}, timeout_seconds = 10 }}
''')
    project = root / "sigilicon.toml"
    with project.open("a") as stream:
        stream.write(f'\n[runtime.tools]\n"mentor.calibre" = "{sys.executable}"\n')
    return project


@pytest.mark.parametrize("origin", ["source", "artifact", "wrong-kind"])
@pytest.mark.parametrize("check", ["drc", "lvs"])
def test_verifier_accepts_stream_producers_and_requires_real_reports(tmp_path: Path, monkeypatch, origin: str, check: str) -> None:
    import json
    from sigilicon.project import Project
    from sigilicon.execution import AdapterPreparation
    from sigilicon.execution._result import (Artifact, StepResult)
    from sigilicon.adapters.mentor.calibre_adapter import CalibreAdapter
    from sigilicon.external_tools import ProcessResult

    _verification_project(tmp_path, origin=origin, check=check)

    class StreamAdapter:
        name = "fake.stream"

        def prepare(self, project, step, resources):
            return AdapterPreparation()

        def preflight(self, step, resources):
            return ()

        def run(self, context):
            path = context.write_text("layout-stream", "layout.gds", "upstream-stream")
            return StepResult.succeeded(artifacts=(Artifact("layout-stream", "report.text" if origin == "wrong-kind" else "layout.gds", path),))

    monkeypatch.setattr("sigilicon.adapters.trusted_adapters", lambda: (StreamAdapter(), CalibreAdapter()))
    launched = []

    def no_reports(request):
        launched.append(request.argv)
        return ProcessResult(0, "offline process returned without a report", "")

    monkeypatch.setattr(physical_verification.managed_process, "run", no_reports)
    project = Project.open(tmp_path)
    plan = project.plan("example:verify")
    assert project.preflight(plan).ready
    if origin == "wrong-kind":
        with pytest.raises(RuntimeError, match="declared format"):
            project.run(plan)
        assert not launched
        return
    result = project.run(plan)
    assert len(launched) == 1
    assert result.status == "failed"
    evidence = next(artifact for artifact in result.outcomes[-1].result.artifacts if artifact.path.name == "typed-evidence.json")
    record = json.loads(evidence.read_text())
    assert record["status"] == "execution_failed"
    assert record["completion"]["report_parsed"] is False
    assert record["layout"]["owner"] == "example"
    assert record["layout"]["format"] == "gdsii"
    if check == "lvs":
        assert record["source"]["name"] == "TOP"


def test_verifier_rejects_changed_foundry_template(tmp_path: Path, monkeypatch) -> None:
    from sigilicon.project import Project

    _verification_project(tmp_path, origin="source")
    (tmp_path / "configs/platform/testpdk/drc.deck").write_text("another foundry revision\n")
    monkeypatch.setattr(physical_verification.managed_process, "run", lambda request: pytest.fail("changed deck must fail before launch"))
    project = Project.open(tmp_path)
    with pytest.raises(ValueError, match="foundry deck identity changed"):
        project.plan("example:verify")


@pytest.mark.parametrize("operation,relative,before,after", [
    ("export", "configs/platform/testpdk/layout.toml", "", "\nxstream_flatten_pcells = false\n"),
    ("export", "ip/example/cells/TOP/cell.toml", 'role = "design"', 'role = "changed"'),
    ("verify", "configs/platform/testpdk/verification.toml", 'replacement = "fixture ', 'replacement = "CHANGED '),
])
def test_planning_rejects_document_changes_between_parse_and_capture(
    tmp_path: Path, monkeypatch, operation: str, relative: str, before: str, after: str,
) -> None:
    from sigilicon.project import Project
    from sigilicon.execution._source import Source

    if operation == "export":
        project = _manual_oa_project(tmp_path)
    else:
        _verification_project(tmp_path, origin="source")
        project = Project.open(tmp_path)
    target = tmp_path / relative
    original = Source.capture
    changed = False

    def capture(cls, path, **kwargs):
        nonlocal changed
        if path == target and not changed:
            text = target.read_text()
            target.write_text(text.replace(before, after) if before else text + after)
            changed = True
        return original(path, **kwargs)

    monkeypatch.setattr(Source, "capture", classmethod(capture))
    with pytest.raises(ValueError, match="source document snapshot drift"):
        project.plan(f"example:{operation}")


@pytest.mark.parametrize("cell", ["/tmp/outside", "../outside", "svdb/child", "bad\\name", "bad\nname"])
def test_verifier_rejects_cell_paths_before_execution(tmp_path: Path, cell: str) -> None:
    import json
    from sigilicon.project import Project

    _verification_project(tmp_path, origin="source", check="lvs")
    catalog = tmp_path / "ip/example/operations.toml"
    catalog.write_text(catalog.read_text().replace('cell = "TOP"', f'cell = {json.dumps(cell)}'))
    with pytest.raises(ValueError, match="Calibre cell"):
        Project.open(tmp_path).plan("example:verify")


@pytest.mark.parametrize("indirection", [None, "directory", "file"])
def test_verifier_collects_only_owned_extracted_netlists(tmp_path: Path, monkeypatch, indirection: str | None) -> None:
    from sigilicon.project import Project
    from sigilicon.external_tools import ProcessResult

    _verification_project(tmp_path, origin="source", check="lvs")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "TOP.sp").write_text("external content\n")

    def calibre(request):
        work = Path(request.cwd)
        for name in ("lvs.rep", "lvs.rep.ext", "calibre_erc.db", "calibre_erc.sum"):
            (work / name).write_text(" INCORRECT TOP TOP\n")
        if indirection == "directory":
            (work / "svdb").symlink_to(outside, target_is_directory=True)
        elif indirection == "file":
            (work / "svdb").mkdir()
            (work / "svdb/TOP.sp").symlink_to(outside / "TOP.sp")
        else:
            (work / "svdb").mkdir()
            (work / "svdb/TOP.sp").write_text("owned extraction\n")
        return ProcessResult(0, "offline LVS mismatch report\n", "")

    monkeypatch.setattr(physical_verification.managed_process, "run", calibre)
    project = Project.open(tmp_path)
    result = project.run(project.plan("example:verify"))
    assert result.status == "failed"
    extracted = [artifact for artifact in result.outcomes[0].result.artifacts if artifact.path.name == "extracted.sp"]
    if indirection is None:
        assert len(extracted) == 1
        assert extracted[0].read_text() == "owned extraction\n"
    else:
        assert not extracted


def _manual_oa_project(tmp_path: Path):
    from sigilicon.project import Project

    _verification_project(tmp_path, origin="source")
    platform = tmp_path / "configs/platform/testpdk/platform.toml"
    platform.write_text(platform.read_text() + 'oa = "oa.toml"\nlayout = "layout.toml"\n')
    owner = tmp_path / "ip/example"
    cell = owner / "cells/TOP"
    cell.mkdir(parents=True)
    (cell / "layout.toml").write_text('description = "manually authored OA layout"\n')
    (cell / "circuit.scs").write_text("subckt TOP A\nends TOP\n")
    (cell / "design.toml").write_text('[ports]\norder = ["A"]\n')
    (cell / "cell.toml").write_text('''schema = 1
contract_kind = "oa-cell"
path_scope = "cell"
owner = "example"
cell = "TOP"
role = "design"
canonical_source = "circuit.scs"
views = [
  { name = "layout", kind = "layout", source = "layout.toml", dependencies = [] },
  { name = "netlist", kind = "spectre_netlist", source = "circuit.scs", dependencies = [] },
  { name = "schematic", kind = "schematic", source = "design.toml", dependencies = ["TOP/netlist"] },
  { name = "symbol", kind = "symbol", source = "design.toml", dependencies = ["TOP/schematic"] },
]
''')
    (owner / "oa.toml").write_text('''schema = 1
contract_kind = "oa-assembly"
path_scope = "owner"
owner = "example"
name = "example"
pdk = "testpdk"
primitive_masters = []
cell_roots = ["cells"]
''')
    component = owner / "component.toml"
    component.write_text(component.read_text().replace("[sources]", '''[sources]
oa = "ip/example/oa.toml"
cell = "ip/example/cells/TOP/cell.toml"
design = "ip/example/cells/TOP/design.toml"
netlist = "ip/example/cells/TOP/circuit.scs"
layout = "ip/example/cells/TOP/layout.toml"''').replace("[filesets]", '[filesets]\noa_source = ["oa"]'))
    with (owner / "operations.toml").open("a") as stream:
        stream.write('''
[operations.export]
uses = "cadence.oa-export"
filesets = [{ component = "example", fileset = "oa_source" }]
config = { owner = "example", cell = "TOP", view = "layout", timeout_seconds = 60 }
''')
    manifest = tmp_path / "sigilicon.toml"
    manifest.write_text(manifest.read_text().replace("[runtime.values]", '[runtime]\ncapabilities = ["license.cadence-oa", "tool.virtuoso-bridge"]\n[runtime.values]').replace("[runtime.tools]", f'[runtime.tools]\n"cadence.xstream" = "{sys.executable}"'))
    (tmp_path / "virtuoso").mkdir()
    (tmp_path / "virtuoso/cds.lib").write_text("DEFINE example ./example\n")
    project = Project.open(tmp_path)
    return project


def test_manual_oa_export_plans_without_a_generator_and_binds_workspace(tmp_path: Path) -> None:
    project = _manual_oa_project(tmp_path)
    plan = project.plan("example:export")
    assert project.preflight(plan).ready
    assert [step.uses for step in plan.steps] == ["cadence.oa-export"]
    (tmp_path / "virtuoso/cds.lib").write_text("DEFINE example ./another\n")
    with pytest.raises((ValueError, RuntimeError), match="drift|changed|blocked"):
        project.run(plan)
