from __future__ import annotations

import json
from pathlib import Path

import pytest

from sigilicon.flow import (
    ActionContext,
    ActionContract,
    AdapterExecution,
    AdapterSelection,
    CollectedActionResult,
    ExecutionProfile,
    FlowContractError,
    FlowEngine,
    FlowNode,
    FlowRegistry,
    FlowSpec,
    FlowTarget,
    PlatformAssetRequirement,
    load_execution_environment,
)


class PlanningAdapter:
    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        return ()

    def prepare(self, context: ActionContext) -> None:
        raise AssertionError("preflight must not prepare")

    def execute(self, context: ActionContext) -> AdapterExecution:
        raise AssertionError("preflight must not execute")

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        raise AssertionError("preflight must not collect")


def _write_environment(tmp_path: Path, *, schema: int = 1) -> tuple[Path, dict[str, Path]]:
    executable = tmp_path / "bin/dc_shell"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    members: dict[str, Path] = {}
    for role in ("rvt", "hvt", "lvt"):
        member = tmp_path / f"timing/{role}.db"
        member.parent.mkdir(exist_ok=True)
        member.write_text(f"{role} timing\n", encoding="utf-8")
        members[role] = member
    contract = tmp_path / "environment.toml"
    contract.write_text(
        f'''schema = {schema}
contract_kind = "execution-environment"
path_scope = "site"
owner = "fixture-site"
name = "synopsys-fixture"

[capabilities."tool.synopsys-dc"]
identity = "synopsys-dc@fixture"
executable = "{executable}"

[[platform_assets]]
role = "standard-cell-timing"
kind = "library.synopsys-db-set"
identity = "fixture:tt"

[[platform_assets.members]]
role = "rvt"
path = "{members['rvt']}"

[[platform_assets.members]]
role = "hvt"
path = "{members['hvt']}"

[[platform_assets.members]]
role = "lvt"
path = "{members['lvt']}"
''',
        encoding="utf-8",
    )
    return contract, {"executable": executable, **members}


def _plan() -> tuple[FlowEngine, object]:
    registry = FlowRegistry()
    registry.register_action(
        ActionContract(
            kind="fake.dc",
            required_capabilities=("tool.synopsys-dc",),
            platform_assets=(
                PlatformAssetRequirement(
                    "standard-cell-timing",
                    "library.synopsys-db-set",
                    members=("rvt", "hvt", "lvt"),
                ),
            ),
            adapters=("fake-dc",),
        )
    )
    registry.register_adapter("fake-dc", PlanningAdapter())
    engine = FlowEngine(registry)
    spec = FlowSpec(
        owner="fixture",
        flow_id="environment",
        nodes=(FlowNode("synthesis", "fake.dc"),),
        targets=(FlowTarget("synthesis", ("synthesis",)),),
    )
    profile = ExecutionProfile(
        owner="fixture",
        profile_id="local",
        selections=(AdapterSelection("fake.dc", "fake-dc"),),
    )
    return engine, engine.plan(spec, "synthesis", profile)


def test_explicit_environment_resolves_private_files_and_public_identity(
    tmp_path: Path,
) -> None:
    contract, paths = _write_environment(tmp_path)

    environment = load_execution_environment(contract)
    engine, plan = _plan()
    result = engine.preflight(plan, environment)
    record = engine.preflight_record(plan, result)

    assert result.status == "ready"
    assert environment.capabilities["tool.synopsys-dc"].executable == paths[
        "executable"
    ]
    timing = environment.platform_asset("standard-cell-timing")
    assert timing is not None
    assert {member.role for member in timing.members} == {"rvt", "hvt", "lvt"}
    assert str(tmp_path) not in json.dumps(record)


def test_execution_environment_accepts_bounded_directory_member(
    tmp_path: Path,
) -> None:
    support = tmp_path / "pex-support"
    support.mkdir()
    (support / "rules").write_text("private rules\n", encoding="utf-8")
    contract = tmp_path / "environment.toml"
    contract.write_text(
        f'''schema = 1
contract_kind = "execution-environment"
path_scope = "site"
owner = "fixture-site"
name = "pex-fixture"

[[platform_assets]]
role = "physical-pex"
kind = "platform.pex"
identity = "fixture:pex"

[[platform_assets.members]]
role = "pex-support-root"
path = "{support}"
''',
        encoding="utf-8",
    )

    environment = load_execution_environment(contract)

    asset = environment.platform_asset("physical-pex")
    assert asset is not None
    member = asset.member("pex-support-root")
    assert member is not None and member.location == support


def test_execution_environment_preserves_multicall_launcher_symlink(
    tmp_path: Path,
) -> None:
    target = tmp_path / "bin/snps_shell"
    target.parent.mkdir()
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o755)
    launcher = target.with_name("dc_shell")
    launcher.symlink_to(target.name)
    contract = tmp_path / "environment.toml"
    contract.write_text(
        f'''schema = 1
contract_kind = "execution-environment"
path_scope = "site"
owner = "fixture-site"
name = "multicall-launcher"

[capabilities."tool.synopsys-dc"]
identity = "synopsys-dc@fixture"
executable = "{launcher}"
''',
        encoding="utf-8",
    )

    environment = load_execution_environment(contract)

    resolved = environment.capabilities["tool.synopsys-dc"].executable
    assert resolved == launcher
    assert resolved is not None and resolved.is_symlink()


def test_preflight_rechecks_capability_availability(tmp_path: Path) -> None:
    contract, paths = _write_environment(tmp_path)
    environment = load_execution_environment(contract)
    engine, plan = _plan()

    paths["executable"].chmod(0o644)
    unavailable = engine.preflight(plan, environment)
    assert next(
        check
        for check in unavailable.checks
        if check.requirement == "tool.synopsys-dc"
    ).status == "unavailable"


def test_environment_contract_is_current_only_and_requires_site_paths(
    tmp_path: Path,
) -> None:
    contract, _ = _write_environment(tmp_path, schema=2)
    with pytest.raises(FlowContractError, match="current schema 1"):
        load_execution_environment(contract)

    contract.write_text(
        '''schema = 1
contract_kind = "execution-environment"
path_scope = "site"
owner = "fixture-site"
name = "invalid"

[capabilities."tool.synopsys-dc"]
identity = "fixture"
executable = "relative/dc_shell"
''',
        encoding="utf-8",
    )
    with pytest.raises(FlowContractError, match="absolute current-site path"):
        load_execution_environment(contract)
