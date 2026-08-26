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
    ExecutionEnvironment,
    ExecutionProfile,
    FlowContractError,
    FlowEngine,
    FlowExecutionError,
    FlowNode,
    FlowRegistry,
    FlowSpec,
    FlowTarget,
    PlatformAssetRequirement,
    ResolvedCapability,
    ResolvedPlatformAsset,
    ResolvedPlatformAssetMember,
    load_catalog_selection,
    load_execution_profile,
)


class RequirementAdapter:
    version = "1"

    def __init__(self) -> None:
        self.executions = 0
        self.platform_location: Path | None = None

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        return ()

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        self.executions += 1
        asset = context.platform_assets.get("logic-lib")
        member = None if asset is None else asset.member("library")
        self.platform_location = None if member is None else member.location
        return AdapterExecution.succeeded(
            details={"mode": context.adapter_config.get("mode", "default")}
        )

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        return CollectedActionResult()


def _write_catalog_sources(owner_root: Path) -> Path:
    (owner_root / "flows").mkdir()
    (owner_root / "profiles").mkdir()
    (owner_root / "flows/pipeline.toml").write_text(
        '''schema = 1
contract_kind = "flow"
path_scope = "owner"
owner = "example"
name = "pipeline"

[[nodes]]
id = "check"
action = "fake.requirements"

[[targets]]
name = "all"
goals = ["check"]
''',
        encoding="utf-8",
    )
    (owner_root / "profiles/local.toml").write_text(
        '''schema = 1
contract_kind = "execution-profile"
path_scope = "owner"
owner = "example"
name = "local"

[actions."fake.requirements"]
adapter = "fake-requirements"
requires = ["runtime.fake-license"]

[actions."fake.requirements".platform_assets]
logic-lib = "fake-platform:logic-lib@1"

[actions."fake.requirements".config]
mode = "local"
''',
        encoding="utf-8",
    )
    catalog = owner_root / "catalog.toml"
    catalog.write_text(
        '''schema = 1
contract_kind = "flow-catalog"
path_scope = "owner"
owner = "example"

[flows.pipeline]
contract = "flows/pipeline.toml"
default_profile = "local"

[flows.pipeline.profiles]
local = "profiles/local.toml"
''',
        encoding="utf-8",
    )
    return catalog


def test_catalog_resolves_owner_scoped_flow_and_default_profile(
    tmp_path: Path,
) -> None:
    catalog = _write_catalog_sources(tmp_path)

    selection = load_catalog_selection(
        catalog,
        owner_root=tmp_path,
        flow_id="pipeline",
    )

    assert selection.spec.flow_id == "pipeline"
    assert selection.profile.profile_id == "local"
    assert selection.profile.selection("fake.requirements").adapter == "fake-requirements"
    assert selection.profile.selection("fake.requirements").config["mode"] == "local"
    assert selection.profile.selection(
        "fake.requirements"
    ).platform_asset_identities == {"logic-lib": "fake-platform:logic-lib@1"}


def test_catalog_rejects_paths_outside_the_explicit_owner_root(tmp_path: Path) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    outside = tmp_path / "outside.toml"
    outside.write_text("not a Flow", encoding="utf-8")
    catalog = owner_root / "catalog.toml"
    catalog.write_text(
        '''schema = 1
contract_kind = "flow-catalog"
path_scope = "owner"
owner = "example"

[flows.pipeline]
contract = "../outside.toml"
default_profile = "local"

[flows.pipeline.profiles]
local = "../outside.toml"
''',
        encoding="utf-8",
    )

    with pytest.raises(FlowContractError, match="owner root"):
        load_catalog_selection(
            catalog,
            owner_root=owner_root,
            flow_id="pipeline",
        )


def test_execution_profile_supports_only_its_current_schema(tmp_path: Path) -> None:
    profile = tmp_path / "profile.toml"
    profile.write_text(
        '''schema = 2
contract_kind = "execution-profile"
path_scope = "owner"
owner = "example"
name = "local"

[actions."fake.requirements"]
adapter = "fake-requirements"
''',
        encoding="utf-8",
    )

    with pytest.raises(FlowContractError, match="current schema 1"):
        load_execution_profile(profile)


def test_flow_catalog_supports_only_its_current_schema(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.toml"
    catalog.write_text(
        '''schema = 2
contract_kind = "flow-catalog"
path_scope = "owner"
owner = "example"
flows = {}
''',
        encoding="utf-8",
    )

    with pytest.raises(FlowContractError, match="current schema 1"):
        load_catalog_selection(
            catalog,
            owner_root=tmp_path,
            flow_id="pipeline",
        )


def test_plan_uses_profile_selection_and_preflight_is_pure(tmp_path: Path) -> None:
    adapter = RequirementAdapter()
    registry = FlowRegistry()
    registry.register_action(
        ActionContract(
            kind="fake.requirements",
            adapters=("fake-requirements",),
            required_capabilities=("runtime.action-capability",),
            platform_assets=(
                PlatformAssetRequirement(
                    "logic-lib",
                    "library.liberty",
                    members=("library",),
                ),
            ),
        )
    )
    registry.register_adapter("fake-requirements", adapter)
    spec = FlowSpec(
        owner="example",
        flow_id="requirements",
        nodes=(FlowNode("check", "fake.requirements"),),
        targets=(FlowTarget("all", ("check",)),),
    )
    profile = ExecutionProfile(
        owner="example",
        profile_id="local",
        selections=(
            AdapterSelection(
                action_kind="fake.requirements",
                adapter="fake-requirements",
                config={"mode": "local"},
                required_capabilities=("runtime.fake-license",),
                platform_asset_identities={
                    "logic-lib": "fake-platform:logic-lib@1"
                },
            ),
        ),
    )
    engine = FlowEngine(registry)

    plan = engine.plan(spec, "all", profile)
    missing = engine.preflight(plan, ExecutionEnvironment())

    assert plan.nodes[0].adapter == "fake-requirements"
    assert missing.status == "blocked"
    assert {check.requirement for check in missing.checks if check.status == "missing"} == {
        "runtime.action-capability",
        "runtime.fake-license",
        "logic-lib",
    }
    assert not (tmp_path / "artifacts").exists()
    with pytest.raises(FlowExecutionError, match="preflight"):
        engine.run(
            plan,
            artifact_root=tmp_path / "artifacts",
            environment=ExecutionEnvironment(),
            run_id="8" * 32,
        )
    assert not (tmp_path / "artifacts").exists()

    installed_library = tmp_path / "installed/logic.lib"
    installed_library.parent.mkdir()
    installed_library.write_text("library fixture\n", encoding="utf-8")
    environment = ExecutionEnvironment(
        capabilities={
            "runtime.action-capability": ResolvedCapability("fake-action@1"),
            "runtime.fake-license": ResolvedCapability("fake-license@1"),
        },
        platform_assets=(
            ResolvedPlatformAsset(
                role="logic-lib",
                kind="library.liberty",
                identity="fake-platform:logic-lib@1",
                members=(
                    ResolvedPlatformAssetMember(
                        role="library",
                        location=installed_library,
                    ),
                ),
            ),
        ),
    )
    incompatible_environment = ExecutionEnvironment(
        capabilities=environment.capabilities,
        platform_assets=(
            ResolvedPlatformAsset(
                role="logic-lib",
                kind="library.liberty",
                identity="fake-platform:logic-lib@wrong",
                members=environment.platform_assets[0].members,
            ),
        ),
    )
    incompatible = engine.preflight(plan, incompatible_environment)
    assert incompatible.status == "blocked"
    assert next(
        check
        for check in incompatible.checks
        if check.requirement == "logic-lib"
    ).status == "incompatible"
    ready = engine.preflight(plan, environment)
    assert ready.status == "ready"
    result = engine.run(
        plan,
        artifact_root=tmp_path / "artifacts",
        environment=environment,
        run_id="8" * 32,
    )

    assert result.status == "accepted"
    assert adapter.executions == 1
    assert adapter.platform_location == (tmp_path / "installed/logic.lib").resolve()
    preflight = json.loads((result.run_root / "inputs/preflight.json").read_text())
    request = json.loads(
        (result.run_root / "inputs/check/action_request.json").read_text()
    )
    assert preflight["status"] == "ready"
    platform_check = next(
        check
        for check in preflight["checks"]
        if check["requirement"] == "logic-lib"
    )
    assert request["execution_environment"]["capabilities"] == {
        "runtime.action-capability": "fake-action@1",
        "runtime.fake-license": "fake-license@1",
    }
    assert request["execution_environment"]["platform_assets"]["logic-lib"] == {
        "kind": "library.liberty",
        "identity": "fake-platform:logic-lib@1",
    }
    assert str(tmp_path / "installed/logic.lib") not in json.dumps(preflight)
    assert str(tmp_path / "installed/logic.lib") not in json.dumps(request)


def test_public_environment_identity_rejects_absolute_site_paths(
    tmp_path: Path,
) -> None:
    with pytest.raises(FlowContractError, match="absolute site path"):
        ExecutionEnvironment(
            capabilities={
                "tool.fake": ResolvedCapability(str(tmp_path / "fake")),
            }
        )
    with pytest.raises(FlowContractError, match="absolute site path"):
        ResolvedPlatformAsset(
            role="logic-lib",
            kind="library.liberty",
            identity=str(tmp_path / "logic.lib"),
        )
