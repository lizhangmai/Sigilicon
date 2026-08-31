from __future__ import annotations

import json
from pathlib import Path

import pytest
import sigilicon.domain.repository as repository_module

from sigilicon.cli.flow_core import main as flow_cli_main
from sigilicon.domain.repository import Project
from sigilicon.flow import ExecutionEnvironment
from sigilicon.workflows.project_flow import (
    FlowRunSelection,
    ProjectFlow,
    RunRequest,
    resolve_project_flow_plan,
)

from conftest import write_component_owner


def _write_extension(
    project_root: Path,
    *,
    register: bool = True,
) -> Path:
    source = project_root / "ip/example/tools/flow_extension.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    body = '''from sigilicon.flow import ActionContract, AdapterResult


class QualificationAdapter:
    def run(self, context):
        scope = context.require_project_scope()
        if scope.owner != "example" or scope.owner_root.name != "example":
            raise ValueError("wrong project owner scope")
        return AdapterResult.succeeded()
'''
    if register:
        body += '''

def register_flow_adapters(registry, owner_root):
    assert owner_root.name == "example"
    registry.register_action(
        ActionContract(
            kind="example.owner-check",
            adapters=("example-owner-check",),
        )
    )
    registry.register_adapter("example-owner-check", QualificationAdapter())
    registry.register_action_adapter(
        "asic.electrical-qualification",
        "example-electrical-qualification",
        QualificationAdapter(),
    )
'''
    source.write_text(body, encoding="utf-8")
    return source


def _declare_extension(project_root: Path, owner: str, source: Path) -> None:
    contract = project_root / "sigilicon.toml"
    contract.write_text(
        contract.read_text(encoding="utf-8")
        + f'''\n[flow.registry_extensions]\n{owner} = "{source.relative_to(project_root).as_posix()}"\n''',
        encoding="utf-8",
    )


def _write_owner_flow(project_root: Path, owner: str = "example") -> Path:
    owner_root = project_root / f"ip/{owner}"
    (owner_root / "flow.toml").write_text(
        f'''schema = 1
contract_kind = "flow"
path_scope = "owner"
owner = "{owner}"
name = "owner-flow"

[[nodes]]
id = "check"
action = "example.owner-check"

[[targets]]
name = "all"
goals = ["check"]
''',
        encoding="utf-8",
    )
    (owner_root / "profile.toml").write_text(
        f'''schema = 1
contract_kind = "execution-profile"
path_scope = "owner"
owner = "{owner}"
name = "local"

[actions."example.owner-check"]
adapter = "example-owner-check"
''',
        encoding="utf-8",
    )
    catalog = owner_root / "catalog.toml"
    catalog.write_text(
        f'''schema = 1
contract_kind = "flow-catalog"
path_scope = "owner"
owner = "{owner}"

[flows.owner-flow]
contract = "flow.toml"
default_profile = "local"

[flows.owner-flow.profiles]
local = "profile.toml"
''',
        encoding="utf-8",
    )
    return catalog


def _owner_flow_files(project_root: Path, source: Path, owner: str = "example") -> tuple[str, ...]:
    return (
        source.relative_to(project_root).as_posix(),
        f"ip/{owner}/catalog.toml",
        f"ip/{owner}/flow.toml",
        f"ip/{owner}/profile.toml",
    )


def test_project_flow_reads_its_canonical_catalog_once_per_operation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_extension(tmp_path)
    write_component_owner(
        tmp_path,
        "example",
        filesets={"flow": _owner_flow_files(tmp_path, source)},
    )
    _declare_extension(tmp_path, "example", source)
    catalog_path = _write_owner_flow(tmp_path).resolve()
    original_read_toml = repository_module.read_toml_record
    catalog_reads = 0

    def counted_read_toml(path: Path):
        nonlocal catalog_reads
        if Path(path).resolve() == catalog_path:
            catalog_reads += 1
        return original_read_toml(path)

    monkeypatch.setattr(repository_module, "read_toml_record", counted_read_toml)
    project = Project.from_project_root(tmp_path)
    project_flow = ProjectFlow(project, "example")

    assert project_flow.catalog().owner == "example"
    assert catalog_reads == 1

    catalog_reads = 0
    request = RunRequest.flow("owner-flow", "all")
    assert request.selection == FlowRunSelection("owner-flow", "all")
    assert RunRequest.oa_simulation("tb_NATIVE").selection.testbench == "tb_NATIVE"
    planned = project_flow.plan(request)

    assert planned.plan_identity == "example:owner-flow:all:local"
    assert not hasattr(planned, "engine")
    assert not hasattr(planned, "plan")
    assert catalog_reads == 1

    with pytest.raises(ValueError, match="requires a RunRequest"):
        project_flow.plan("owner-flow")  # type: ignore[arg-type]

    catalog_reads = 0
    resolved = resolve_project_flow_plan(
        project,
        "example:owner-flow:all:local",
    )

    assert resolved.plan_identity == "example:owner-flow:all:local"
    assert catalog_reads == 1

    catalog_reads = 0
    assert flow_cli_main(
        [
            "show",
            "--project-root",
            str(tmp_path),
            "--owner",
            "example",
            "--flow",
            "owner-flow",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["flow"] == "owner-flow"
    assert catalog_reads == 1


def test_plan_identity_resolution_does_not_read_unrelated_owner_flows(
    tmp_path: Path,
) -> None:
    source = _write_extension(tmp_path)
    write_component_owner(
        tmp_path,
        "example",
        filesets={"flow": _owner_flow_files(tmp_path, source)},
    )
    _declare_extension(tmp_path, "example", source)
    _write_owner_flow(tmp_path)
    unrelated_flow = tmp_path / "ip/unrelated/broken.toml"
    unrelated_flow.parent.mkdir(parents=True)
    unrelated_flow.write_text("not valid toml = [", encoding="utf-8")
    write_component_owner(
        tmp_path,
        "unrelated",
        filesets={"flow": ("ip/unrelated/broken.toml",)},
    )
    project = Project.from_project_root(tmp_path)

    resolved = resolve_project_flow_plan(
        project,
        "example:owner-flow:all:local",
    )

    assert resolved.plan_identity == "example:owner-flow:all:local"
    for identity in (
        "",
        "example:owner-flow:all",
        "example:owner-flow:all:local:extra",
    ):
        with pytest.raises(ValueError, match="Flow Plan identity"):
            resolve_project_flow_plan(project, identity)


def test_project_extension_content_is_bound_to_plan_and_preflight(
    tmp_path: Path,
) -> None:
    source = _write_extension(tmp_path)
    _write_owner_flow(tmp_path)
    write_component_owner(
        tmp_path,
        "example",
        filesets={"flow": _owner_flow_files(tmp_path, source)},
    )
    _declare_extension(tmp_path, "example", source)
    project = Project.from_project_root(tmp_path)
    project_flow = ProjectFlow(project, "example")
    planned = project_flow.plan(RunRequest.flow("owner-flow", "all"))

    approved_record = planned.record
    source_record = approved_record["implementation_sources"][0]
    assert source_record["path"] == "ip/example/tools/flow_extension.py"
    assert len(source_record["sha256"]) == 64
    assert project_flow.preflight(planned, ExecutionEnvironment()).status == "ready"

    source.write_text(source.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")

    preflight = project_flow.preflight(planned, ExecutionEnvironment())
    assert preflight.status == "blocked"
    assert preflight.checks[0].requirement_kind == "implementation-source"
    assert preflight.checks[0].status == "changed"

    replanned = ProjectFlow(project, "example").plan(
        RunRequest.flow("owner-flow", "all"),
    )
    assert replanned.record != approved_record


def test_project_extension_binds_python_helpers_from_other_owner_filesets(
    tmp_path: Path,
) -> None:
    source = _write_extension(tmp_path)
    helper = tmp_path / "ip/example/tools/helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    _write_owner_flow(tmp_path)
    write_component_owner(
        tmp_path,
        "example",
        filesets={
            "flow": _owner_flow_files(tmp_path, source),
            "support": (helper.relative_to(tmp_path).as_posix(),),
        },
    )
    _declare_extension(tmp_path, "example", source)
    project = Project.from_project_root(tmp_path)
    project_flow = ProjectFlow(project, "example")
    planned = project_flow.plan(RunRequest.flow("owner-flow", "all"))

    paths = {
        item["path"] for item in planned.record["implementation_sources"]
    }
    assert helper.relative_to(tmp_path).as_posix() in paths

    helper.write_text("VALUE = 2\n", encoding="utf-8")
    assert project_flow.preflight(planned, ExecutionEnvironment()).status == "blocked"


def test_project_flow_hides_owner_paths_and_registry_assembly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _write_extension(tmp_path)
    write_component_owner(
        tmp_path,
        "example",
        filesets={
            "flow": _owner_flow_files(tmp_path, source)
        },
    )
    _declare_extension(tmp_path, "example", source)
    _write_owner_flow(tmp_path)

    project_flow = ProjectFlow(Project.from_project_root(tmp_path), "example")
    planned = project_flow.plan(RunRequest.flow("owner-flow", "all"))

    assert planned.plan_identity == "example:owner-flow:all:local"
    assert planned.record["nodes"][0]["adapter"] == "example-owner-check"
    assert str(tmp_path.resolve()) not in json.dumps(planned.record)
    assert project_flow.preflight(
        planned,
        ExecutionEnvironment(),
    ).status == "ready"
    independently_assembled = ProjectFlow(
        Project.from_project_root(tmp_path), "example"
    ).plan(RunRequest.flow("owner-flow", "all"))
    with pytest.raises(ValueError, match="exact project owner binding"):
        project_flow.preflight(independently_assembled, ExecutionEnvironment())

    result = flow_cli_main(
        [
            "plan",
            "--owner",
            "example",
            "--flow",
            "owner-flow",
            "--target",
            "all",
            "--project-root",
            str(tmp_path),
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == planned.record

    result = flow_cli_main(
        [
            "run",
            "--owner",
            "example",
            "--flow",
            "owner-flow",
            "--target",
            "all",
            "--project-root",
            str(tmp_path),
            "--run-id",
            "2" * 32,
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out)["status"] == "accepted"
    assert project_flow.read_result(
        flow="owner-flow",
        target="all",
        run_id="2" * 32,
    )["status"] == "accepted"
    project_flow.clean_run(
        flow="owner-flow",
        target="all",
        run_id="2" * 32,
    )
    assert not tuple((tmp_path / "artifacts").rglob("flow_result.json"))


def test_semantic_flow_cli_discovers_and_parses_the_project_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_extension(tmp_path)
    write_component_owner(
        tmp_path,
        "example",
        filesets={"flow": _owner_flow_files(tmp_path, source)},
    )
    _declare_extension(tmp_path, "example", source)
    _write_owner_flow(tmp_path)
    contract = (tmp_path / "sigilicon.toml").resolve()
    original = repository_module.read_toml
    manifest_reads = 0

    def counted(path: Path):
        nonlocal manifest_reads
        if path.resolve() == contract:
            manifest_reads += 1
        return original(path)

    monkeypatch.setattr(repository_module, "read_toml", counted)
    monkeypatch.chdir(tmp_path)

    result = flow_cli_main(
        [
            "plan",
            "--owner",
            "example",
            "--flow",
            "owner-flow",
            "--target",
            "all",
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out)["owner"] == "example"
    assert manifest_reads == 1


def test_project_flow_registry_requires_the_single_extension_interface(
    tmp_path: Path,
) -> None:
    source = _write_extension(tmp_path, register=False)
    _write_owner_flow(tmp_path)
    write_component_owner(
        tmp_path,
        "example",
        filesets={"flow": _owner_flow_files(tmp_path, source)},
    )
    _declare_extension(tmp_path, "example", source)

    project = Project.from_project_root(tmp_path)
    with pytest.raises(ValueError, match="register_flow_adapters"):
        ProjectFlow(project, "example").plan(
            RunRequest.flow("owner-flow", "all")
        )


def test_project_flow_registry_reports_registration_failure_as_contract_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _write_extension(tmp_path)
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "def register_flow_adapters(registry, owner_root):",
            "def register_flow_adapters(registry, owner_root):\n    raise RuntimeError('broken owner registration')",
        ),
        encoding="utf-8",
    )
    write_component_owner(
        tmp_path,
        "example",
        filesets={"flow": _owner_flow_files(tmp_path, source)},
    )
    _declare_extension(tmp_path, "example", source)
    _write_owner_flow(tmp_path)

    result = flow_cli_main(
        [
            "plan",
            "--owner",
            "example",
            "--flow",
            "owner-flow",
            "--target",
            "all",
            "--project-root",
            str(tmp_path),
        ]
    )

    assert result == 2
    error = capsys.readouterr().err
    assert "cannot register Flow extension" in error
    assert "Sigilicon defect" not in error
