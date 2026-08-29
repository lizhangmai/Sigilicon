from __future__ import annotations

import json
from pathlib import Path

import pytest

from sigilicon.cli.flow_core import main as flow_cli_main
from sigilicon.domain.repository import RepositoryContext
from sigilicon.flow import ExecutionEnvironment, FlowEngine, load_catalog_selection
from sigilicon.workflows.project_flow import ProjectFlow, project_workflow_registry

from conftest import write_component_owner


def _write_extension(
    project_root: Path,
    *,
    owner: str = "example",
    register: bool = True,
) -> Path:
    source = project_root / f"ip/{owner}/tools/flow_extension.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    body = '''from sigilicon.flow import ActionContract, AdapterExecution, CollectedActionResult


class QualificationAdapter:
    def validate_inputs(self, context):
        return ()

    def prepare(self, context):
        pass

    def execute(self, context):
        return AdapterExecution.succeeded()

    def collect_result(self, context, execution):
        return CollectedActionResult()
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


def _write_other_flow_source(project_root: Path, owner: str) -> str:
    source = project_root / f"ip/{owner}/tools/other_flow.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# separately owned flow source\n", encoding="utf-8")
    return source.relative_to(project_root).as_posix()


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


def test_project_flow_registry_applies_the_selected_owner_extension(
    tmp_path: Path,
) -> None:
    source = _write_extension(tmp_path)
    write_component_owner(
        tmp_path,
        "example",
        filesets={"flow": (source.relative_to(tmp_path).as_posix(),)},
    )
    _declare_extension(tmp_path, "example", source)

    registry = project_workflow_registry(
        RepositoryContext.from_project_root(tmp_path),
        tmp_path / "ip/example",
    )

    assert registry.has_adapter("example-electrical-qualification")
    assert "example-electrical-qualification" in registry.action(
        "asic.electrical-qualification"
    ).adapters


def test_public_flow_cli_uses_explicit_project_assembly(
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
    owner_root = tmp_path / "ip/example"
    catalog = _write_owner_flow(tmp_path)
    unrelated = tmp_path / "unrelated-project"
    unrelated.mkdir()
    (unrelated / "sigilicon.toml").write_text(
        '''schema = 1
contract_kind = "sigilicon-project"
path_scope = "repository"
owner = "unrelated"

[catalogs]
ip = "missing-ip.toml"
platform = "missing-platform.toml"

[paths]
project_root = "."
workspace_root = "workspace"
artifact_root = "artifacts"
''',
        encoding="utf-8",
    )
    monkeypatch.chdir(unrelated)

    result = flow_cli_main(
        [
            "plan",
            str(catalog),
            "owner-flow",
            "all",
            "--owner-root",
            str(owner_root),
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out)["nodes"][0]["adapter"] == (
        "example-owner-check"
    )


def test_project_extension_content_is_bound_to_plan_and_preflight(
    tmp_path: Path,
) -> None:
    source = _write_extension(tmp_path)
    write_component_owner(
        tmp_path,
        "example",
        filesets={"flow": (source.relative_to(tmp_path).as_posix(),)},
    )
    _declare_extension(tmp_path, "example", source)
    owner_root = tmp_path / "ip/example"
    catalog = _write_owner_flow(tmp_path)
    registry = project_workflow_registry(tmp_path, owner_root)
    engine = FlowEngine(registry)
    selection = load_catalog_selection(
        catalog,
        owner_root=owner_root,
        flow_id="owner-flow",
    )
    plan = engine.plan(selection.spec, "all", selection.profile)

    approved_record = engine.plan_record(plan)
    source_record = approved_record["implementation_sources"][0]
    assert source_record["path"] == "ip/example/tools/flow_extension.py"
    assert len(source_record["sha256"]) == 64
    assert engine.preflight(plan, ExecutionEnvironment()).status == "ready"

    source.write_text(source.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")

    preflight = engine.preflight(plan, ExecutionEnvironment())
    assert preflight.status == "blocked"
    assert preflight.checks[0].requirement_kind == "implementation-source"
    assert preflight.checks[0].status == "changed"

    replanned_engine = FlowEngine(project_workflow_registry(tmp_path, owner_root))
    replanned = replanned_engine.plan(selection.spec, "all", selection.profile)
    assert replanned_engine.plan_record(replanned) != approved_record


def test_project_extension_binds_python_helpers_from_other_owner_filesets(
    tmp_path: Path,
) -> None:
    source = _write_extension(tmp_path)
    helper = tmp_path / "ip/example/tools/helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    write_component_owner(
        tmp_path,
        "example",
        filesets={
            "flow": (source.relative_to(tmp_path).as_posix(),),
            "support": (helper.relative_to(tmp_path).as_posix(),),
        },
    )
    _declare_extension(tmp_path, "example", source)
    owner_root = tmp_path / "ip/example"
    registry = project_workflow_registry(tmp_path, owner_root)
    engine = FlowEngine(registry)
    catalog = _write_owner_flow(tmp_path)
    selection = load_catalog_selection(
        catalog,
        owner_root=owner_root,
        flow_id="owner-flow",
    )
    plan = engine.plan(selection.spec, "all", selection.profile)

    paths = {
        item["path"] for item in engine.plan_record(plan)["implementation_sources"]
    }
    assert helper.relative_to(tmp_path).as_posix() in paths

    helper.write_text("VALUE = 2\n", encoding="utf-8")
    assert engine.preflight(plan, ExecutionEnvironment()).status == "blocked"


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

    project_flow = ProjectFlow.from_project_root(tmp_path, owner="example")
    planned = project_flow.plan(flow="owner-flow", target="all")

    assert planned.plan_identity == "example:owner-flow:all:local"
    assert planned.record["nodes"][0]["adapter"] == "example-owner-check"
    assert project_flow.preflight(
        planned,
        ExecutionEnvironment(),
    ).status == "ready"
    independently_assembled = ProjectFlow.from_project_root(
        tmp_path,
        owner="example",
    ).plan(flow="owner-flow", target="all")
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
    assert (tmp_path / "artifacts").is_dir()


def test_project_path_selection_rejects_noncanonical_catalog_and_option_mixing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _write_extension(tmp_path)
    write_component_owner(
        tmp_path,
        "example",
        filesets={"flow": _owner_flow_files(tmp_path, source)},
    )
    _declare_extension(tmp_path, "example", source)
    canonical = _write_owner_flow(tmp_path)
    owner_root = tmp_path / "ip/example"
    alternate = owner_root / "alternate-catalog.toml"
    alternate.write_text(canonical.read_text(encoding="utf-8"), encoding="utf-8")

    result = flow_cli_main(
        [
            "plan",
            str(alternate),
            "owner-flow",
            "all",
            "--owner-root",
            str(owner_root),
        ]
    )

    assert result == 2
    assert "canonical catalog" in capsys.readouterr().err

    result = flow_cli_main(
        [
            "plan",
            str(canonical),
            "owner-flow",
            "all",
            "--owner-root",
            str(owner_root),
            "--flow",
            "different-flow",
            "--target",
            "different-target",
        ]
    )

    assert result == 2
    assert "requires positional flow" in capsys.readouterr().err


def test_project_flow_registry_rejects_extension_outside_owner_flow_fileset(
    tmp_path: Path,
) -> None:
    source = _write_extension(tmp_path)
    write_component_owner(
        tmp_path,
        "example",
        filesets={"flow": (_write_other_flow_source(tmp_path, "example"),)},
    )
    _declare_extension(tmp_path, "example", source)

    with pytest.raises(ValueError, match="owner flow fileset"):
        project_workflow_registry(
            RepositoryContext.from_project_root(tmp_path),
            tmp_path / "ip/example",
        )


def test_project_flow_registry_requires_the_single_extension_interface(
    tmp_path: Path,
) -> None:
    source = _write_extension(tmp_path, register=False)
    write_component_owner(
        tmp_path,
        "example",
        filesets={"flow": (source.relative_to(tmp_path).as_posix(),)},
    )
    _declare_extension(tmp_path, "example", source)

    with pytest.raises(ValueError, match="register_flow_adapters"):
        project_workflow_registry(
            RepositoryContext.from_project_root(tmp_path),
            tmp_path / "ip/example",
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
    owner_root = tmp_path / "ip/example"
    catalog = _write_owner_flow(tmp_path)

    result = flow_cli_main(
        [
            "plan",
            str(catalog),
            "owner-flow",
            "all",
            "--owner-root",
            str(owner_root),
            "--project-root",
            str(tmp_path),
        ]
    )

    assert result == 2
    error = capsys.readouterr().err
    assert "cannot register Flow extension" in error
    assert "Sigilicon defect" not in error

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


def test_project_flow_registry_rejects_an_extension_owned_by_another_ip(
    tmp_path: Path,
) -> None:
    source = _write_extension(tmp_path, owner="other")
    write_component_owner(
        tmp_path,
        "example",
        filesets={"flow": (_write_other_flow_source(tmp_path, "example"),)},
    )
    write_component_owner(
        tmp_path,
        "other",
        filesets={"flow": (source.relative_to(tmp_path).as_posix(),)},
    )
    _declare_extension(tmp_path, "example", source)

    with pytest.raises(ValueError, match="must stay inside its owner root"):
        project_workflow_registry(
            RepositoryContext.from_project_root(tmp_path),
            tmp_path / "ip/example",
        )
