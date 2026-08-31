from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from conftest import StagedAdapterFixture, write_component_owner, write_project_context
from sigilicon.flow import (
    ActionBinding,
    ActionContract,
    AdapterExecution,
    ArtifactBinding,
    ArtifactPort,
    CollectedActionResult,
    ExecutionEnvironment,
    FlowContractError,
    FlowEngine,
    FlowNode,
    FlowRegistry,
    FlowSpec,
    FlowTarget,
    ProducedArtifact,
    ResolvedCapability,
    ResolvedPlatformAsset,
    ResolvedPlatformAssetMember,
)
from sigilicon.flow.physical_design import (
    OA_XSTREAM_MATERIALIZATION_ADAPTER,
    PHYSICAL_DESIGN_JOB_KIND,
    PHYSICAL_DESIGN_RESULT_KIND,
    PHYSICAL_MATERIALIZATION_EXECUTION_ACTION,
    PHYSICAL_MATERIALIZATION_PLAN_KIND,
    register_physical_design_actions,
)
from sigilicon.layout.materialization import (
    MaterializationTarget,
    compile_materialization_plan,
)
from sigilicon.layout.materialization_execution import (
    MaterializationExecutionStatus,
    canonicalize_gdsii_timestamps,
    materialization_receipt_from_json,
)
from sigilicon.layout.pnr import (
    CutSpacingRule,
    EnclosureRule,
    GridlessRoutingResource,
    LayerKind,
    MinimumSpacingRule,
    MinimumWidthRule,
    PhysicalDesign,
    PhysicalDesignJob,
    PhysicalInstance,
    PhysicalLayer,
    PhysicalMaster,
    PhysicalNet,
    PhysicalPort,
    PhysicalTechnology,
    PinAccess,
    PinReference,
    Placement,
    PnrExecutionPolicy,
    PnrRequest,
    PnrStage,
    Point,
    Rect,
    RoutingDirection,
    ViaDefinition,
    run,
)
from sigilicon.virtuoso.xstream import XStreamExportError, XStreamExportResult
from sigilicon.workflows.oa_materialization import OaXStreamMaterializationAdapter
from sigilicon.domain.repository import Project


_INPUT_ACTION = "fixture.oa-materialization-inputs"
_INPUT_ADAPTER = "fixture-oa-materialization-inputs"


def _job(*, maximum_route_states: int = 200_000) -> PhysicalDesignJob:
    return PhysicalDesignJob(
        PhysicalTechnology(
            "oa-materialization-neutral",
            1000,
            1,
            layers=(
                PhysicalLayer("route", LayerKind.ROUTING, RoutingDirection.ANY),
            ),
            routing_resources=(GridlessRoutingResource("route-domain", "route"),),
            rules=(
                MinimumWidthRule("route-width", "route", 2),
                MinimumSpacingRule("route-spacing", "route", 1),
            ),
        ),
        PhysicalDesign(
            "oa-materialization-neutral",
            Rect(0, 0, 20, 10),
            (),
            (),
            ports=(
                PhysicalPort("source", (PinAccess("route", Rect(1, 1, 3, 3)),)),
                PhysicalPort("sink", (PinAccess("route", Rect(17, 1, 19, 3)),)),
            ),
            nets=(
                PhysicalNet(
                    "signal",
                    (PinReference("source"), PinReference("sink")),
                ),
            ),
        ),
        request=PnrRequest(stages=(PnrStage.PLACEMENT, PnrStage.ROUTING)),
        execution_policy=PnrExecutionPolicy(maximum_route_states=maximum_route_states),
    )


def _artifacts(*, maximum_route_states: int = 200_000):
    job = _job(maximum_route_states=maximum_route_states)
    result = run(job)
    plan = compile_materialization_plan(
        job,
        result,
        MaterializationTarget("benchmark", "neutral_layout"),
    )
    return job, result, plan


def _artifacts_with_instance():
    job = _job()
    master = PhysicalMaster("unit", 2, 2)
    job = replace(
        job,
        design=replace(
            job.design,
            masters=(master,),
            instances=(
                PhysicalInstance("fixed", master.name, Placement(Point(8, 8))),
            ),
        ),
    )
    result = run(job)
    plan = compile_materialization_plan(
        job,
        result,
        MaterializationTarget("benchmark", "neutral_layout"),
    )
    return job, result, plan


def _artifacts_with_via():
    technology = PhysicalTechnology(
        "oa-materialization-multilayer",
        1000,
        1,
        layers=(
            PhysicalLayer("m1", LayerKind.ROUTING, RoutingDirection.ANY),
            PhysicalLayer("v1", LayerKind.CUT),
            PhysicalLayer("m2", LayerKind.ROUTING, RoutingDirection.ANY),
        ),
        routing_resources=(
            GridlessRoutingResource("m1-domain", "m1"),
            GridlessRoutingResource("m2-domain", "m2"),
        ),
        via_definitions=(
            ViaDefinition(
                "via12",
                "m1",
                "v1",
                "m2",
                lower_shapes=(Rect(-2, -2, 2, 2),),
                cut_shapes=(Rect(-1, -1, 1, 1),),
                upper_shapes=(Rect(-2, -2, 2, 2),),
            ),
        ),
        rules=(
            MinimumWidthRule("m1-width", "m1", 2),
            MinimumSpacingRule("m1-spacing", "m1", 1),
            MinimumWidthRule("m2-width", "m2", 2),
            MinimumSpacingRule("m2-spacing", "m2", 1),
            EnclosureRule("m1-v1", "m1", "v1", 1, 1),
            EnclosureRule("m2-v1", "m2", "v1", 1, 1),
            CutSpacingRule("v1-spacing", "v1", 2, 2),
        ),
    )
    job = PhysicalDesignJob(
        technology,
        PhysicalDesign(
            "oa-materialization-multilayer",
            Rect(0, 0, 20, 10),
            (),
            (),
            ports=(
                PhysicalPort("source", (PinAccess("m1", Rect(1, 1, 3, 3)),)),
                PhysicalPort("sink", (PinAccess("m2", Rect(17, 1, 19, 3)),)),
            ),
            nets=(
                PhysicalNet(
                    "signal",
                    (PinReference("source"), PinReference("sink")),
                ),
            ),
        ),
        request=PnrRequest(stages=(PnrStage.PLACEMENT, PnrStage.ROUTING)),
    )
    result = run(job)
    plan = compile_materialization_plan(
        job,
        result,
        MaterializationTarget("benchmark", "neutral_layout"),
    )
    return job, result, plan


class _InputsAdapter(StagedAdapterFixture):
    def __init__(self, job, result, plan) -> None:
        self._values = (job, result, plan)

    def validate_inputs(self, _context):
        return ()

    def prepare(self, _context):
        pass

    def execute(self, context):
        for role, filename, value in zip(
            ("job", "result", "plan"),
            ("job.json", "result.json", "plan.json"),
            self._values,
            strict=True,
        ):
            context.output_path(role, filename).write_text(
                value.canonical_json(),
                encoding="utf-8",
            )
        return AdapterExecution.succeeded()

    def collect_result(self, context, _execution):
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "job",
                    PHYSICAL_DESIGN_JOB_KIND,
                    context.output_path("job", "job.json"),
                ),
                ProducedArtifact(
                    "result",
                    PHYSICAL_DESIGN_RESULT_KIND,
                    context.output_path("result", "result.json"),
                ),
                ProducedArtifact(
                    "plan",
                    PHYSICAL_MATERIALIZATION_PLAN_KIND,
                    context.output_path("plan", "plan.json"),
                ),
            )
        )


class _Library:
    def __init__(self, path: Path) -> None:
        self._info = SimpleNamespace(
            path=str(path),
            technology_library="techLib",
        )

    def get(self, _library, **_kwargs):
        return self._info


class _Client:
    def __init__(self, library_path: Path, *, write_error: str | None = None) -> None:
        self.library = _Library(library_path)
        self.write_error = write_error
        self.sources: list[str] = []

    def execute_skill(self, source: str, **_kwargs):
        self.sources.append(source)
        if self.write_error and "sigiliconPlanIdentity" in source:
            return SimpleNamespace(output="nil", errors=[self.write_error])
        return SimpleNamespace(output="t", errors=[])


class _Operation:
    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id

    def register_artifact(self, record) -> None:
        record.bind_operation(self.operation_id)

    @contextmanager
    def view_lease(self, *_args, **_kwargs):
        yield

    @contextmanager
    def mutation_scope(self, *_args, **_kwargs):
        yield

    def require_active_mutation(self, *_args, **_kwargs):
        return None


def _patch_workspace(monkeypatch) -> None:
    @contextmanager
    def operation(*_args, operation_id, **_kwargs):
        yield _Operation(operation_id)

    monkeypatch.setattr(
        "sigilicon.workflows.oa_materialization.workspace_operation",
        operation,
    )
    def execute_skill(client, _operation, source, **_kwargs):
        result = client.execute_skill(source)
        if result.errors:
            raise RuntimeError(result.errors[0])
        return result.output

    monkeypatch.setattr(
        "sigilicon.workflows.oa_materialization.execute_owned_cellview_skill",
        execute_skill,
    )

    def ensure_library(_client, **kwargs):
        kwargs["path"].mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(action="created")

    monkeypatch.setattr(
        "sigilicon.workflows.oa_materialization.ensure_project_library",
        ensure_library,
    )


def _record(record_type: int, data_type: int = 0, data: bytes = b"") -> bytes:
    assert len(data) % 2 == 0
    return struct.pack(">HBB", len(data) + 4, record_type, data_type) + data


def _gds(timestamp: int) -> bytes:
    date = struct.pack(">12H", *((timestamp, 1, 1, 0, 0, 0) * 2))
    xy = struct.pack(">iiii", 1, 2, 17, 2)
    return b"".join(
        (
            _record(0x00, 0x02, struct.pack(">H", 600)),
            _record(0x01, 0x02, date),
            _record(0x02, 0x06, b"TESTLIB\0"),
            _record(0x03, 0x05, bytes(16)),
            _record(0x05, 0x02, date),
            _record(0x06, 0x06, b"NEUTRAL\0"),
            _record(0x09),
            _record(0x0D, 0x02, struct.pack(">H", 1)),
            _record(0x0E, 0x02, struct.pack(">H", 0)),
            _record(0x0F, 0x03, struct.pack(">i", 2)),
            _record(0x10, 0x03, xy),
            _record(0x11),
            _record(0x07),
            _record(0x04),
        )
    )


def _fake_xstream(timestamp: int = 2026):
    def run(request):
        request.work_root.mkdir(parents=True, exist_ok=True)
        gds = request.work_root / "layout.gds"
        native = request.work_root / "strmout.log"
        summary = request.work_root / "strmout.sum"
        gds.write_bytes(_gds(timestamp))
        native.write_text(
            "Translation completed. '0' error(s) and '0' warning(s) found.\n",
            encoding="utf-8",
        )
        summary.write_text("complete\n", encoding="utf-8")
        return XStreamExportResult(
            command=(str(request.executable), "-library", request.library),
            exit_code=0,
            stdout="complete\n",
            gds_path=gds,
            native_log_path=native,
            summary_path=summary,
        )

    return run


def _write_assets(
    root: Path,
    *,
    include_route: bool = True,
    create_library: bool = True,
) -> tuple[ResolvedPlatformAsset, Path]:
    workspace = root / "virtuoso"
    library = workspace / "managed_materialization"
    workspace.mkdir(exist_ok=True)
    (workspace / "cds.lib").write_text("# managed test workspace\n", encoding="utf-8")
    if create_library:
        library.mkdir()
    target = root / "target.toml"
    target.write_text(
        '''schema = 1
contract_kind = "physical-materialization-target"
path_scope = "owner"
owner = "benchmark"

name = "neutral_layout"
library = "managed_materialization"
cell = "neutral_layout"
view = "layout"
library_path = "virtuoso/managed_materialization"
managed_scratch = true
replace_existing = true

[masters]
''',
        encoding="utf-8",
    )
    technology = root / "oa.toml"
    technology.write_text(
        '''schema = 1
contract_kind = "platform-oa"
path_scope = "platform"
owner = "test-platform"
technology_library = "techLib"
reference_libraries = ["techLib"]
[primitive_subcircuits]
''',
        encoding="utf-8",
    )
    layout = root / "layout.toml"
    layer_table = (
        '''[oa_materialization.layers.route]
layer = "M1"
drawing_purpose = "drawing"
pin_purpose = "pin"
blockage_purpose = "drawing"
'''
        if include_route
        else "[oa_materialization.layers]\n"
    )
    layout.write_text(
        f'''schema = 1
contract_kind = "platform-layout"
path_scope = "platform"
owner = "test-platform"
dbu_per_micron = 1000

[oa_materialization]
{layer_table}
[oa_materialization.vias]
''',
        encoding="utf-8",
    )
    verification = root / "verification.toml"
    verification.write_text(
        '''schema = 1
contract_kind = "platform-verification"
path_scope = "platform"
owner = "test-platform"
layermap = "unused"
drc_deck = "unused"
lvs_deck = "unused"
xstream_flatten_pcells = false
xstream_suppressed_warnings = ["XSTRM-35"]
''',
        encoding="utf-8",
    )
    xstream_map = root / "xstream.map"
    xstream_map.write_text("M1 drawing 1 0\n", encoding="utf-8")
    return (
        ResolvedPlatformAsset(
            "physical-layout",
            "platform.layout-view-set",
            "benchmark.oa-xstream-assets",
            (
                ResolvedPlatformAssetMember("oa-target", target),
                ResolvedPlatformAssetMember("technology-library", technology),
                ResolvedPlatformAssetMember("layer-map", layout),
                ResolvedPlatformAssetMember("master-layouts", target),
                ResolvedPlatformAssetMember("via-map", layout),
                ResolvedPlatformAssetMember("xstream-layer-map", xstream_map),
                ResolvedPlatformAssetMember("xstream-options", verification),
            ),
        ),
        library,
    )


def _environment(root: Path, asset: ResolvedPlatformAsset) -> ExecutionEnvironment:
    xstream = root / "strmout"
    xstream.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    xstream.chmod(0o755)
    return ExecutionEnvironment(
        capabilities={
            "tool.layout-materializer": ResolvedCapability("test.layout-materializer"),
            "tool.virtuoso-bridge": ResolvedCapability("test.virtuoso-bridge"),
            "tool.xstream": ResolvedCapability("test.xstream", xstream),
            "license.cadence-oa": ResolvedCapability("test.cadence-license"),
        },
        platform_assets=(asset,),
    )


def _engine(root: Path | None, job, result, plan, adapter) -> FlowEngine:
    registry = FlowRegistry()
    register_physical_design_actions(registry)
    registry.register_action(
        ActionContract(
            _INPUT_ACTION,
            outputs=(
                ArtifactPort("job", PHYSICAL_DESIGN_JOB_KIND),
                ArtifactPort("result", PHYSICAL_DESIGN_RESULT_KIND),
                ArtifactPort("plan", PHYSICAL_MATERIALIZATION_PLAN_KIND),
            ),
            adapters=(_INPUT_ADAPTER,),
        )
    )
    registry.register_adapter(_INPUT_ADAPTER, _InputsAdapter(job, result, plan))
    registry.register_adapter(OA_XSTREAM_MATERIALIZATION_ADAPTER, adapter)
    scope = None
    if root is not None:
        if not (root / "ip/benchmark/component.toml").is_file():
            write_component_owner(root, "benchmark", filesets={})
        project = Project.from_project_root(root)
        scope = project.scope("benchmark")
    return FlowEngine(registry, project_scope=scope)


def _plan(engine: FlowEngine, owner_root: Path):
    spec = FlowSpec(
        owner="benchmark",
        flow_id="oa-xstream-materialization",
        recipe_id="oa-xstream-materialization-recipe",
        nodes=(
            FlowNode("inputs", _INPUT_ACTION),
            FlowNode(
                "materialize",
                PHYSICAL_MATERIALIZATION_EXECUTION_ACTION,
                bindings=(
                    ArtifactBinding("job", "inputs", "job"),
                    ArtifactBinding("result", "inputs", "result"),
                    ArtifactBinding("plan", "inputs", "plan"),
                ),
                config={
                    "target": {
                        "owner": "benchmark",
                        "name": "neutral_layout",
                        "format": "gdsii",
                    }
                },
            ),
        ),
        targets=(FlowTarget("materialized", ("materialize",)),),
        action_bindings=(
            ActionBinding(_INPUT_ACTION, _INPUT_ADAPTER),
            ActionBinding(
                PHYSICAL_MATERIALIZATION_EXECUTION_ACTION,
                OA_XSTREAM_MATERIALIZATION_ADAPTER,
                requires=(
                    "tool.virtuoso-bridge",
                    "tool.xstream",
                    "license.cadence-oa",
                ),
                platform_assets={
                    "physical-layout": "benchmark.oa-xstream-assets"
                },
            ),
        ),
        owner_root=owner_root,
    )
    return engine.plan(spec, "materialized")


def test_flow_plan_scope_rejects_same_owner_from_another_project(
    tmp_path: Path,
) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    write_project_context(project_a)
    write_project_context(project_b)
    write_component_owner(project_b, "benchmark", filesets={})
    job, result, plan = _artifacts()
    engine = _engine(
        project_a,
        job,
        result,
        plan,
        OaXStreamMaterializationAdapter(_client_factory=lambda: None),
    )

    with pytest.raises(FlowContractError, match="owner root"):
        _plan(engine, project_b / "ip/benchmark")


def _receipt(flow_result):
    return materialization_receipt_from_json(
        flow_result.nodes["materialize"]
        .artifacts["receipt"]
        .path.read_text(encoding="utf-8")
    )


def test_production_adapter_materializes_real_gds_contract_through_flow(
    monkeypatch,
    tmp_path: Path,
) -> None:
    write_project_context(tmp_path)
    asset, library = _write_assets(tmp_path)
    client = _Client(library)
    adapter = OaXStreamMaterializationAdapter(_client_factory=lambda: client)
    job, result, plan = _artifacts()
    engine = _engine(tmp_path, job, result, plan, adapter)
    monkeypatch.setattr(
        Project,
        "from_project_root",
        classmethod(
            lambda cls, root, **kwargs: (_ for _ in ()).throw(
                AssertionError("Adapter must reuse the injected project scope")
            )
        ),
    )
    _patch_workspace(monkeypatch)
    monkeypatch.setattr(
        "sigilicon.workflows.oa_materialization.run_xstream_export",
        _fake_xstream(),
    )

    flow_result = engine.run(
        _plan(engine, tmp_path / "ip/benchmark"),
        artifact_root=tmp_path / "artifacts",
        environment=_environment(tmp_path, asset),
        run_id="1" * 32,
    )

    receipt = _receipt(flow_result)
    assert flow_result.status == "accepted"
    assert receipt.status is MaterializationExecutionStatus.MATERIALIZED
    assert receipt.materialized
    assert receipt.layout is not None
    assert receipt.layout.run_id == "1" * 32
    operation = json.loads(
        (flow_result.run_root / "inputs/materialize/operation.json").read_text(
            encoding="utf-8"
        )
    )
    assert operation["operation_id"] == (
        flow_result.nodes["materialize"].operation_id
    )
    assert operation["incident_reference"] is None
    write_source = next(source for source in client.sources if "dbCreatePath" in source)
    assert "dbCreateTerm" in write_source
    assert "sigiliconJobIdentity" in write_source
    assert '"maskLayout" "w"' in write_source
    assert "ddDeleteObj" not in write_source
    assert write_source.count('list("M1" "drawing")') >= 1
    assert write_source.count('list("M1" "pin")') >= 1
    assert "dbCreatePin(net pinFig)" in write_source
    layout_identity = receipt.provenance.layout_identity
    assert layout_identity is not None
    assert layout_identity == receipt.layout.content_identity


def test_production_adapter_requires_explicit_project_scope(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    asset, _library = _write_assets(tmp_path)
    job, result, plan = _artifacts()
    engine = _engine(
        None,
        job,
        result,
        plan,
        OaXStreamMaterializationAdapter(_client_factory=lambda: None),
    )

    flow_result = engine.run(
        _plan(engine, tmp_path / "ip/benchmark"),
        artifact_root=tmp_path / "artifacts",
        environment=_environment(tmp_path, asset),
        run_id="9" * 32,
    )

    outcome = flow_result.nodes["materialize"]
    assert outcome.status == "failed"
    assert "requires an explicit project owner scope" in str(outcome.reason)


def test_repeated_materialization_canonicalizes_xstream_timestamps(
    monkeypatch,
    tmp_path: Path,
) -> None:
    write_project_context(tmp_path)
    asset, library = _write_assets(tmp_path)
    job, result, plan = _artifacts()
    _patch_workspace(monkeypatch)
    identities = []
    for index, timestamp in enumerate((2025, 2026), start=2):
        adapter = OaXStreamMaterializationAdapter(
            _client_factory=lambda: _Client(library),
        )
        engine = _engine(tmp_path, job, result, plan, adapter)
        monkeypatch.setattr(
            "sigilicon.workflows.oa_materialization.run_xstream_export",
            _fake_xstream(timestamp),
        )
        flow_result = engine.run(
            _plan(engine, tmp_path / "ip/benchmark"),
            artifact_root=tmp_path / "artifacts",
            environment=_environment(tmp_path, asset),
            run_id=str(index) * 32,
        )
        identities.append(_receipt(flow_result).provenance.layout_identity)

    assert identities[0] == identities[1]
    assert canonicalize_gdsii_timestamps(_gds(2025)) == canonicalize_gdsii_timestamps(
        _gds(2026)
    )


@pytest.mark.parametrize(
    "missing",
    ("tool.virtuoso-bridge", "tool.xstream", "license.cadence-oa"),
)
def test_flow_preflight_requires_backend_and_license_capabilities(
    tmp_path: Path,
    missing: str,
) -> None:
    write_project_context(tmp_path)
    asset, _library = _write_assets(tmp_path)
    job, result, plan = _artifacts()
    engine = _engine(
        tmp_path,
        job,
        result,
        plan,
        OaXStreamMaterializationAdapter(_client_factory=lambda: None),
    )
    environment = _environment(tmp_path, asset)
    capabilities = dict(environment.capabilities)
    capabilities.pop(missing)

    preflight = engine.preflight(
        _plan(engine, tmp_path / "ip/benchmark"),
        ExecutionEnvironment(capabilities, environment.platform_assets),
    )

    assert preflight.status == "blocked"
    assert (missing, "missing") in {
        (check.requirement, check.status) for check in preflight.checks
    }


def test_adapter_preflight_requires_every_explicit_layout_asset(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    asset, _library = _write_assets(tmp_path)
    incomplete = replace(
        asset,
        members=tuple(
            member for member in asset.members if member.role != "via-map"
        ),
    )
    job, result, plan = _artifacts()
    calls = 0

    def client_factory():
        nonlocal calls
        calls += 1
        raise AssertionError("incomplete assets must not contact OA")

    engine = _engine(
        tmp_path,
        job,
        result,
        plan,
        OaXStreamMaterializationAdapter(_client_factory=client_factory),
    )

    flow_result = engine.run(
        _plan(engine, tmp_path / "ip/benchmark"),
        artifact_root=tmp_path / "artifacts",
        environment=_environment(tmp_path, incomplete),
        run_id="4" * 32,
    )

    assert flow_result.nodes["materialize"].status == "failed"
    assert "physical-layout platform view omitted members: ['via-map']" in str(
        flow_result.nodes["materialize"].reason
    )
    assert calls == 0


def test_missing_managed_library_is_created_inside_managed_workspace(
    monkeypatch,
    tmp_path: Path,
) -> None:
    write_project_context(tmp_path)
    asset, library = _write_assets(tmp_path, create_library=False)
    _patch_workspace(monkeypatch)
    monkeypatch.setattr(
        "sigilicon.workflows.oa_materialization.run_xstream_export",
        _fake_xstream(),
    )

    job, result, plan = _artifacts()
    engine = _engine(
        tmp_path,
        job,
        result,
        plan,
        OaXStreamMaterializationAdapter(
            _client_factory=lambda: _Client(library),
        ),
    )
    flow_result = engine.run(
        _plan(engine, tmp_path / "ip/benchmark"),
        artifact_root=tmp_path / "artifacts",
        environment=_environment(tmp_path, asset),
        run_id="5" * 32,
    )

    assert _receipt(flow_result).status is MaterializationExecutionStatus.MATERIALIZED
    assert library.is_dir()


def test_managed_library_symlink_is_rejected_before_bridge_connection(
    tmp_path: Path,
) -> None:
    write_project_context(tmp_path)
    asset, library = _write_assets(tmp_path, create_library=False)
    outside = tmp_path / "outside-library"
    outside.mkdir()
    library.symlink_to(outside, target_is_directory=True)
    calls = 0

    def client_factory():
        nonlocal calls
        calls += 1
        raise AssertionError("symlink target must not contact OA")

    job, result, plan = _artifacts()
    engine = _engine(
        tmp_path,
        job,
        result,
        plan,
        OaXStreamMaterializationAdapter(_client_factory=client_factory),
    )

    flow_result = engine.run(
        _plan(engine, tmp_path / "ip/benchmark"),
        artifact_root=tmp_path / "artifacts",
        environment=_environment(tmp_path, asset),
        run_id="c" * 32,
    )

    assert flow_result.nodes["materialize"].status == "failed"
    assert "managed OA target library must not be a symlink" in str(
        flow_result.nodes["materialize"].reason
    )
    assert calls == 0


def test_unmapped_plan_feature_is_typed_unsupported_before_oa(
    tmp_path: Path,
) -> None:
    write_project_context(tmp_path)
    asset, _library = _write_assets(tmp_path, include_route=False)
    calls = 0

    def client_factory():
        nonlocal calls
        calls += 1
        raise AssertionError("unsupported plan must not contact OA")

    job, result, plan = _artifacts()
    engine = _engine(
        tmp_path,
        job,
        result,
        plan,
        OaXStreamMaterializationAdapter(_client_factory=client_factory),
    )
    flow_result = engine.run(
        _plan(engine, tmp_path / "ip/benchmark"),
        artifact_root=tmp_path / "artifacts",
        environment=_environment(tmp_path, asset),
        run_id="6" * 32,
    )

    receipt = _receipt(flow_result)
    assert receipt.status is MaterializationExecutionStatus.UNSUPPORTED
    assert receipt.layout is None
    assert "layer-purpose mappings are missing" in receipt.message
    assert calls == 0


def test_unmapped_master_is_typed_unsupported_before_oa(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    asset, _library = _write_assets(tmp_path)
    calls = 0

    def client_factory():
        nonlocal calls
        calls += 1
        raise AssertionError("unmapped master must not contact OA")

    job, result, plan = _artifacts_with_instance()
    engine = _engine(
        tmp_path,
        job,
        result,
        plan,
        OaXStreamMaterializationAdapter(_client_factory=client_factory),
    )

    flow_result = engine.run(
        _plan(engine, tmp_path / "ip/benchmark"),
        artifact_root=tmp_path / "artifacts",
        environment=_environment(tmp_path, asset),
        run_id="d" * 32,
    )

    receipt = _receipt(flow_result)
    assert receipt.status is MaterializationExecutionStatus.UNSUPPORTED
    assert receipt.layout is None
    assert "master layouts are unmapped: ['unit']" in receipt.message
    assert calls == 0


def test_unmapped_via_is_typed_unsupported_before_oa(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    asset, _library = _write_assets(tmp_path)
    layout_member = asset.member("layer-map")
    assert layout_member is not None
    layout_member.location.write_text(
        layout_member.location.read_text(encoding="utf-8")
        + '''
[oa_materialization.layers.m1]
layer = "M1"
drawing_purpose = "drawing"
pin_purpose = "pin"
blockage_purpose = "drawing"

[oa_materialization.layers.m2]
layer = "M2"
drawing_purpose = "drawing"
pin_purpose = "pin"
blockage_purpose = "drawing"
''',
        encoding="utf-8",
    )
    calls = 0

    def client_factory():
        nonlocal calls
        calls += 1
        raise AssertionError("unmapped via must not contact OA")

    job, result, plan = _artifacts_with_via()
    assert plan.route_vias
    engine = _engine(
        tmp_path,
        job,
        result,
        plan,
        OaXStreamMaterializationAdapter(_client_factory=client_factory),
    )

    flow_result = engine.run(
        _plan(engine, tmp_path / "ip/benchmark"),
        artifact_root=tmp_path / "artifacts",
        environment=_environment(tmp_path, asset),
        run_id="e" * 32,
    )

    receipt = _receipt(flow_result)
    assert receipt.status is MaterializationExecutionStatus.UNSUPPORTED
    assert receipt.layout is None
    assert "OA via mappings are missing: ['via12']" in receipt.message
    assert calls == 0


def test_diagnostic_plan_is_identity_rejected_before_oa(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    asset, _library = _write_assets(tmp_path)
    calls = 0

    def client_factory():
        nonlocal calls
        calls += 1
        raise AssertionError("invalid plan must not contact OA")

    job, result, plan = _artifacts(maximum_route_states=1)
    engine = _engine(
        tmp_path,
        job,
        result,
        plan,
        OaXStreamMaterializationAdapter(_client_factory=client_factory),
    )
    flow_result = engine.run(
        _plan(engine, tmp_path / "ip/benchmark"),
        artifact_root=tmp_path / "artifacts",
        environment=_environment(tmp_path, asset),
        run_id="7" * 32,
    )

    receipt = _receipt(flow_result)
    assert receipt.status is MaterializationExecutionStatus.INVALID_PLAN_IDENTITY
    assert not receipt.completion.executed
    assert receipt.layout is None
    assert calls == 0


def test_bridge_unavailability_is_not_execution_failure(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    asset, _library = _write_assets(tmp_path)
    job, result, plan = _artifacts()
    engine = _engine(
        tmp_path,
        job,
        result,
        plan,
        OaXStreamMaterializationAdapter(
            _client_factory=lambda: (_ for _ in ()).throw(RuntimeError("offline")),
        ),
    )
    flow_result = engine.run(
        _plan(engine, tmp_path / "ip/benchmark"),
        artifact_root=tmp_path / "artifacts",
        environment=_environment(tmp_path, asset),
        run_id="8" * 32,
    )

    receipt = _receipt(flow_result)
    assert receipt.status is MaterializationExecutionStatus.BACKEND_UNAVAILABLE
    assert not receipt.completion.executed
    assert receipt.layout is None


@pytest.mark.parametrize("failure", ("oa-save", "xstream-nonzero", "malformed-gds"))
def test_attempted_backend_failures_never_publish_layout(
    monkeypatch,
    tmp_path: Path,
    failure: str,
) -> None:
    write_project_context(tmp_path)
    asset, library = _write_assets(tmp_path)
    client = _Client(library, write_error="save failed" if failure == "oa-save" else None)
    job, result, plan = _artifacts()
    engine = _engine(
        tmp_path,
        job,
        result,
        plan,
        OaXStreamMaterializationAdapter(
            _client_factory=lambda: client,
        ),
    )
    _patch_workspace(monkeypatch)
    if failure == "xstream-nonzero":
        monkeypatch.setattr(
            "sigilicon.workflows.oa_materialization.run_xstream_export",
            lambda _request: (_ for _ in ()).throw(
                XStreamExportError("exited 9", executed=True, exit_code=9)
            ),
        )
    elif failure == "malformed-gds":
        def malformed(request):
            result_value = _fake_xstream()(request)
            result_value.gds_path.write_bytes(b"not-gds")
            return result_value

        monkeypatch.setattr(
            "sigilicon.workflows.oa_materialization.run_xstream_export",
            malformed,
        )
    else:
        monkeypatch.setattr(
            "sigilicon.workflows.oa_materialization.run_xstream_export",
            lambda _request: pytest.fail("XStream must not run after OA save failure"),
        )

    flow_result = engine.run(
        _plan(engine, tmp_path / "ip/benchmark"),
        artifact_root=tmp_path / "artifacts",
        environment=_environment(tmp_path, asset),
        run_id={
            "oa-save": "9" * 32,
            "xstream-nonzero": "a" * 32,
            "malformed-gds": "b" * 32,
        }[failure],
    )

    receipt = _receipt(flow_result)
    assert receipt.status is MaterializationExecutionStatus.EXECUTION_FAILED
    assert receipt.completion.executed
    assert receipt.layout is None
    assert "layout" not in flow_result.nodes["materialize"].artifacts
    if failure == "xstream-nonzero":
        assert receipt.completion.exit_code == 9
