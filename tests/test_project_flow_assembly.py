from __future__ import annotations

import json
from pathlib import Path

import pytest

from sigilicon.cli.flow_core import main as flow_cli_main
from sigilicon.domain.repository import RepositoryContext
from sigilicon.workflows.project_flow import project_workflow_registry

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
) -> None:
    source = _write_extension(tmp_path)
    write_component_owner(
        tmp_path,
        "example",
        filesets={"flow": (source.relative_to(tmp_path).as_posix(),)},
    )
    _declare_extension(tmp_path, "example", source)
    owner_root = tmp_path / "ip/example"
    (owner_root / "flow.toml").write_text(
        '''schema = 1
contract_kind = "flow"
path_scope = "owner"
owner = "example"
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
        '''schema = 1
contract_kind = "execution-profile"
path_scope = "owner"
owner = "example"
name = "local"

[actions."example.owner-check"]
adapter = "example-owner-check"
''',
        encoding="utf-8",
    )
    catalog = owner_root / "catalog.toml"
    catalog.write_text(
        '''schema = 1
contract_kind = "flow-catalog"
path_scope = "owner"
owner = "example"

[flows.owner-flow]
contract = "flow.toml"
default_profile = "local"

[flows.owner-flow.profiles]
local = "profile.toml"
''',
        encoding="utf-8",
    )

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

    assert result == 0
    assert json.loads(capsys.readouterr().out)["nodes"][0]["adapter"] == (
        "example-owner-check"
    )


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
