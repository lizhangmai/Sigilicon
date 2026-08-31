from __future__ import annotations

import json
from pathlib import Path

import pytest

from sigilicon.flow import (
    ActionBinding,
    ActionContext,
    ActionContract,
    AdapterExecution,
    CollectedActionResult,
    ExecutionEnvironment,
    FlowContractError,
    FlowEngine,
    FlowNode,
    FlowRegistry,
    FlowTarget,
    PlatformAssetRequirement,
    ResolvedCapability,
    ResolvedPlatformAsset,
    ResolvedPlatformAssetMember,
    compile_flow_spec,
    load_execution_recipe,
)

from conftest import StagedAdapterFixture


class RequirementAdapter(StagedAdapterFixture):
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


def _write_recipe(root: Path, *, schema: int = 1, extra: str = "") -> Path:
    recipe = root / "recipe.toml"
    recipe.write_text(
        f'''schema = {schema}
contract_kind = "execution-recipe"
path_scope = "owner"
owner = "example"
name = "pipeline"
{extra}
[actions."fake.requirements"]
adapter = "fake-requirements"
requires = ["runtime.fake-license"]

[actions."fake.requirements".platform_assets]
logic-lib = "fake-platform:logic-lib@1"

[actions."fake.requirements".config]
mode = "local"

[[nodes]]
id = "check"
action = "fake.requirements"
''',
        encoding="utf-8",
    )
    return recipe


def test_execution_recipe_loads_action_bindings_and_compiles_target(
    tmp_path: Path,
) -> None:
    recipe = load_execution_recipe(_write_recipe(tmp_path), owner_root=tmp_path)

    assert recipe.owner == "example"
    assert recipe.recipe_id == "pipeline"
    binding = recipe.action_binding("fake.requirements")
    assert binding.adapter == "fake-requirements"
    assert binding.requires == ("runtime.fake-license",)
    assert binding.platform_assets == {
        "logic-lib": "fake-platform:logic-lib@1"
    }
    assert binding.config["mode"] == "local"

    compiled = compile_flow_spec(
        recipe,
        flow_id="operation",
        targets=(FlowTarget("all", ("check",)),),
    )
    assert compiled.recipe_id == "pipeline"
    assert compiled.target("all").goals == ("check",)
    assert compiled.action_binding("fake.requirements") == binding


@pytest.mark.parametrize("field", ["targets", "expand"])
def test_execution_recipe_rejects_target_selection_and_expansion(
    tmp_path: Path,
    field: str,
) -> None:
    if field == "targets":
        extra = 'targets = [{ name = "all", goals = ["check"] }]\n'
    else:
        extra = 'expand = { kind = "legacy" }\n'
    recipe = _write_recipe(tmp_path, extra=extra)

    with pytest.raises(FlowContractError, match="cannot declare"):
        load_execution_recipe(recipe)


def test_execution_recipe_supports_only_current_schema(tmp_path: Path) -> None:
    recipe = _write_recipe(tmp_path, schema=2)

    with pytest.raises(FlowContractError, match="current schema 1"):
        load_execution_recipe(recipe)


def test_flow_engine_uses_bindings_from_compiled_spec_and_writes_recipe_identity(
    tmp_path: Path,
) -> None:
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
    from sigilicon.flow import ExecutionRecipe

    recipe = ExecutionRecipe(
        owner="example",
        recipe_id="local",
        nodes=(FlowNode("check", "fake.requirements"),),
        action_bindings=(
            ActionBinding(
                "fake.requirements",
                "fake-requirements",
                config={"mode": "local"},
                requires=("runtime.fake-license",),
                platform_assets={"logic-lib": "fake-platform:logic-lib@1"},
            ),
        ),
        owner_root=tmp_path,
    )
    spec = compile_flow_spec(
        recipe,
        flow_id="requirements",
        targets=(FlowTarget("all", ("check",)),),
    )
    engine = FlowEngine(registry)
    plan = engine.plan(spec, "all")
    missing = engine.preflight(plan, ExecutionEnvironment())

    assert plan.nodes[0].adapter == "fake-requirements"
    assert missing.status == "blocked"
    assert {check.requirement for check in missing.checks if check.status == "missing"} == {
        "runtime.action-capability",
        "runtime.fake-license",
        "logic-lib",
    }
    assert engine.plan_id(plan) == "example:requirements:all"
    assert engine.plan_record(plan)["recipe"] == "local"
    assert "execution_profile" not in engine.plan_record(plan)
    assert engine.preflight_record(plan, missing)["recipe"] == "local"
    assert "execution_profile" not in engine.preflight_record(plan, missing)
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
    result = engine.run(
        plan,
        artifact_root=tmp_path / "artifacts",
        environment=environment,
        run_id="8" * 32,
    )

    assert result.status == "accepted"
    assert adapter.executions == 1
    assert adapter.platform_location == installed_library.resolve()
    payload = json.loads(
        (result.run_root / "inputs/preflight.json").read_text(encoding="utf-8")
    )
    assert payload["recipe"] == "local"


def test_action_binding_requires_declared_action_and_adapter_contract() -> None:
    binding = ActionBinding("fake.requirements", "fake-requirements")
    with pytest.raises(FlowContractError, match="Action binding"):
        from sigilicon.flow import ExecutionRecipe

        ExecutionRecipe(
            owner="example",
            recipe_id="empty-binding",
            nodes=(FlowNode("check", "fake.requirements"),),
            action_bindings=(),
        )
    assert binding.platform_assets == {}
