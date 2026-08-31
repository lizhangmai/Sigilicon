from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import struct
import subprocess

import pytest

from conftest import StagedAdapterFixture
from sigilicon.domain.physical_verification import (
    PhysicalVerificationStatus,
    drc_evidence_from_json,
    lvs_evidence_from_json,
)
from sigilicon.flow import (
    ActionBinding,
    ActionContract,
    AdapterExecution,
    ArtifactBinding,
    ArtifactPort,
    CollectedActionResult,
    ExecutionEnvironment,
    FactSet,
    FactSource,
    FlowEngine,
    FlowExecutionError,
    FlowNode,
    FlowSpec,
    FlowTarget,
    ProducedArtifact,
    ResolvedCapability,
    ResolvedPlatformAsset,
    ResolvedPlatformAssetMember,
)
from sigilicon.flow.physical_design import (
    MATERIALIZED_GDS_KIND,
    MATERIALIZATION_RECEIPT_KIND,
)
from sigilicon.flow.physical_verification import (
    CALIBRE_PHYSICAL_VERIFICATION_ADAPTER,
    CANONICAL_SOURCE_NETLIST_KIND,
    DRC_ACTION,
    LVS_ACTION,
    OFFLINE_PHYSICAL_VERIFICATION_ADAPTER,
    PHYSICAL_VERIFICATION_POLICY_KIND,
    PHYSICAL_VERIFICATION_SOURCE_ACTION,
    RECEIPT_BOUND_VERIFICATION_SOURCE_ADAPTER,
)
from sigilicon.layout.materialization import (
    MaterializationTarget,
    compile_materialization_plan,
)
from sigilicon.layout.materialization_execution import (
    LayoutArtifactFormat,
    MaterializationCompletion,
    MaterializationExecutionStatus,
    MaterializationExecutionTarget,
    identify_managed_layout,
    issue_materialization_receipt,
    materialization_receipt_from_json,
    materialization_receipt_id,
)
from sigilicon.layout.physical_design import (
    GridlessRoutingResource,
    LayerKind,
    MinimumSpacingRule,
    MinimumWidthRule,
    PhysicalDesign,
    PhysicalLayer,
    PhysicalNet,
    PhysicalPort,
    PhysicalTechnology,
    PinAccess,
    PinReference,
    PhysicalDesignRequest,
    PhysicalDesignStage,
    Rect,
    RoutingDirection,
)
from sigilicon.experimental.reference_pnr import (
    ReferencePnrJob,
    run,
)
from sigilicon.workflows.builtin import build_flow_registry
from sigilicon.workflows.physical_verification import (
    OfflinePhysicalVerificationAdapter,
)


_INPUT_ACTION = "contract-fixture.receipt-bound-inputs"
_INPUT_ADAPTER = "contract-receipt-bound-inputs"
_TARGET = MaterializationExecutionTarget(
    "benchmark",
    "receipt_bound_top",
    LayoutArtifactFormat.GDSII,
)


def _identity(path: Path) -> str:
    return f"fixture:{path.name}"


def _job() -> ReferencePnrJob:
    technology = PhysicalTechnology(
        "receipt-verification-gridless",
        1000,
        1,
        layers=(PhysicalLayer("route", LayerKind.ROUTING, RoutingDirection.ANY),),
        routing_resources=(GridlessRoutingResource("route-domain", "route"),),
        rules=(
            MinimumWidthRule("route-width", "route", 2),
            MinimumSpacingRule("route-spacing", "route", 1),
        ),
    )
    return ReferencePnrJob(
        technology,
        PhysicalDesign(
            "receipt-bound-verification",
            Rect(0, 0, 20, 10),
            (),
            (),
            ports=(
                PhysicalPort("a", (PinAccess("route", Rect(1, 1, 3, 3)),)),
                PhysicalPort("b", (PinAccess("route", Rect(17, 1, 19, 3)),)),
            ),
            nets=(PhysicalNet("signal", (PinReference("a"), PinReference("b"))),),
        ),
        request=PhysicalDesignRequest(stages=(PhysicalDesignStage.PLACEMENT, PhysicalDesignStage.ROUTING)),
    )


def _record(record_type: int, data_type: int = 0, data: bytes = b"") -> bytes:
    assert len(data) % 2 == 0
    return struct.pack(">HBB", len(data) + 4, record_type, data_type) + data


def _gds_string(value: str) -> bytes:
    payload = value.encode("ascii")
    return payload if len(payload) % 2 == 0 else payload + b"\0"


def _contract_gds(plan) -> bytes:
    """A plan-derived GDSII PATH used only as benchmark contract evidence."""

    segment = plan.route_segments[0]
    return b"".join(
        (
            _record(0x00, 0x02, struct.pack(">H", 600)),
            _record(0x01, 0x02, bytes(24)),
            _record(0x02, 0x06, _gds_string("CONTRACT-FIXTURE")),
            _record(0x03, 0x05, bytes(16)),
            _record(0x05, 0x02, bytes(24)),
            _record(0x06, 0x06, _gds_string(_TARGET.name)),
            _record(0x09),
            _record(0x0D, 0x02, struct.pack(">H", 1)),
            _record(0x0E, 0x02, struct.pack(">H", 0)),
            _record(0x0F, 0x03, struct.pack(">i", segment.width_dbu)),
            _record(
                0x10,
                0x03,
                struct.pack(
                    ">iiii",
                    segment.start.x,
                    segment.start.y,
                    segment.end.x,
                    segment.end.y,
                ),
            ),
            _record(0x11),
            _record(0x07),
            _record(0x04),
        )
    )


def _policy() -> str:
    return '''schema = 1
contract_kind = "physical-verification-policy"
path_scope = "owner"
owner = "benchmark"

[drc]
disabled_defines = {}
configuration_warnings = []
waiver_layers = []
'''


class _ReceiptBoundInputsAdapter(StagedAdapterFixture):
    """Creates upstream contract artifacts without claiming product closure."""

    def __init__(self, *, corrupt: str | None = None) -> None:
        self.corrupt = corrupt
        self.job = _job()
        self.result = run(self.job)
        self.plan = compile_materialization_plan(
            self.job,
            self.result,
            MaterializationTarget(_TARGET.owner, _TARGET.name),
        )

    def validate_inputs(self, _context):
        return ()

    def prepare(self, _context):
        pass

    def execute(self, context):
        layout_path = context.output_path("layout", "layout.gds")
        layout_path.write_bytes(_contract_gds(self.plan))
        managed = identify_managed_layout(
            layout_path,
            target=_TARGET,
            run_root=context.run_root,
            run_id=context.run_root.name,
            producer=context.node_id,
        )
        receipt = issue_materialization_receipt(
            self.job,
            self.result,
            self.plan,
            _TARGET,
            status=MaterializationExecutionStatus.MATERIALIZED,
            completion=MaterializationCompletion(
                "sigilicon.test.contract-materializer",
                True,
                True,
                True,
                0,
            ),
            layout=managed,
            message="benchmark materialization contract evidence",
        )
        context.output_path("receipt", "receipt.json").write_text(
            receipt.canonical_json(), encoding="utf-8"
        )
        context.output_path("source", "source.cdl").write_text(
            (
                ".SUBCKT wrong_top a b\n.ENDS wrong_top\n"
                if self.corrupt == "source-content"
                else ".SUBCKT receipt_bound_top a b\n.ENDS receipt_bound_top\n"
            ),
            encoding="utf-8",
        )
        context.output_path("verification-policy", "policy.toml").write_text(
            _policy(), encoding="utf-8"
        )
        if self.corrupt == "layout-content":
            layout_path.write_bytes(layout_path.read_bytes() + _record(0x04))
        return AdapterExecution.succeeded()

    def collect_result(self, context, _execution):
        receipt_path = context.output_path("receipt", "receipt.json")
        receipt = materialization_receipt_from_json(
            receipt_path.read_text(encoding="utf-8")
        )
        receipt_identity = materialization_receipt_id(receipt)
        common = {
            "owner": _TARGET.owner,
            "name": _TARGET.name,
            "format": _TARGET.format.value,
            "job-identity": receipt.provenance.job_identity,
            "result-identity": receipt.provenance.result_identity,
            "plan-identity": receipt.provenance.plan_identity,
            "receipt-identity": receipt_identity,
            "status": receipt.status.value,
            "backend": receipt.completion.backend,
        }
        if self.corrupt == "receipt-qualifier":
            common["receipt-identity"] = "corrupt-receipt"
        if self.corrupt == "result-qualifier":
            common["result-identity"] = "corrupt-result"
        layout_path = context.output_path("layout", "layout.gds")
        source_path = context.output_path("source", "source.cdl")
        policy_path = context.output_path("verification-policy", "policy.toml")
        source_qualifiers = {
            "owner": _TARGET.owner,
            "name": _TARGET.name,
            "source-identity": _identity(source_path),
        }
        return CollectedActionResult(
            facts=FactSet.empty(
                context.action.fact_schema,
                source=FactSource(context.action.kind, context.node_id),
            ),
            artifacts=(
                ProducedArtifact(
                    "layout",
                    MATERIALIZED_GDS_KIND,
                    layout_path,
                    qualifiers={
                        **common,
                        "layout-identity": receipt.provenance.layout_identity,
                    },
                ),
                ProducedArtifact(
                    "receipt",
                    MATERIALIZATION_RECEIPT_KIND,
                    receipt_path,
                    qualifiers=common,
                ),
                ProducedArtifact(
                    "source",
                    CANONICAL_SOURCE_NETLIST_KIND,
                    source_path,
                    qualifiers=source_qualifiers,
                ),
                ProducedArtifact(
                    "verification-policy",
                    PHYSICAL_VERIFICATION_POLICY_KIND,
                    policy_path,
                    qualifiers={
                        "owner": _TARGET.owner,
                        "policy-identity": _identity(policy_path),
                    },
                ),
            )
        )


def _registry(*, corrupt: str | None = None, offline: bool = False):
    registry = build_flow_registry()
    registry.register_action(
        ActionContract(
            _INPUT_ACTION,
            outputs=(
                ArtifactPort("layout", MATERIALIZED_GDS_KIND),
                ArtifactPort("receipt", MATERIALIZATION_RECEIPT_KIND),
                ArtifactPort("source", CANONICAL_SOURCE_NETLIST_KIND),
                ArtifactPort(
                    "verification-policy", PHYSICAL_VERIFICATION_POLICY_KIND
                ),
            ),
            adapters=(_INPUT_ADAPTER,),
        )
    )
    registry.register_adapter(
        _INPUT_ADAPTER,
        _ReceiptBoundInputsAdapter(corrupt=corrupt),
    )
    if offline:
        registry.register_adapter(
            OFFLINE_PHYSICAL_VERIFICATION_ADAPTER,
            OfflinePhysicalVerificationAdapter(),
        )
    return registry


def _flow(adapter: str = CALIBRE_PHYSICAL_VERIFICATION_ADAPTER):
    bindings = (
        ArtifactBinding("layout", "inputs", "layout"),
        ArtifactBinding("receipt", "inputs", "receipt"),
        ArtifactBinding(
            "verification-policy", "inputs", "verification-policy"
        ),
    )
    spec = FlowSpec(
        owner="benchmark",
        flow_id="receipt-bound-verification",
        recipe_id="receipt-bound-verification-recipe",
        nodes=(
            FlowNode("inputs", _INPUT_ACTION),
            FlowNode("drc", DRC_ACTION, bindings=bindings),
            FlowNode(
                "lvs",
                LVS_ACTION,
                bindings=(
                    *bindings,
                    ArtifactBinding("source", "inputs", "source"),
                ),
            ),
        ),
        targets=(FlowTarget("verification", ("drc", "lvs")),),
        action_bindings=(
            ActionBinding(_INPUT_ACTION, _INPUT_ADAPTER),
            ActionBinding(DRC_ACTION, adapter),
            ActionBinding(LVS_ACTION, adapter),
        ),
    )
    return spec


def _drc_deck() -> str:
    return '''LAYOUT PATH "GDSFILENAME"
LAYOUT PRIMARY "TOPCELLNAME"
DRC RESULTS DATABASE "DRC_RES.db"
DRC SUMMARY REPORT "DRC.rep"  // HIER
'''


def _lvs_deck() -> str:
    return '''LAYOUT PRIMARY "lvs_top"
LAYOUT PATH "lvs_top.gds"
SOURCE PRIMARY "lvs_top"
SOURCE PATH "lvs_top.cdl"
DRC RESULTS DATABASE "calibre_drc.db" ASCII // ASCII or GDSII
DRC SUMMARY REPORT "calibre_drc.sum"
ERC RESULTS DATABASE "calibre_erc.db" ASCII // ASCII or GDSII
ERC SUMMARY REPORT "calibre_erc.sum"
LVS REPORT "lvs.rep"
  //MASK SVDB DIRECTORY "svdb" QUERY
  MASK SVDB DIRECTORY "svdb" QUERY
'''


def _environment(tmp_path: Path) -> ExecutionEnvironment:
    executable = tmp_path / "calibre"
    executable.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    executable.chmod(0o755)
    drc_deck = tmp_path / "drc.deck"
    lvs_deck = tmp_path / "lvs.deck"
    drc_deck.write_text(_drc_deck(), encoding="utf-8")
    lvs_deck.write_text(_lvs_deck(), encoding="utf-8")
    return ExecutionEnvironment(
        capabilities={
            "tool.calibre": ResolvedCapability(
                "calibre.contract-fixture", executable
            )
        },
        platform_assets=(
            ResolvedPlatformAsset(
                "physical-verification",
                "platform.calibre-verification",
                "benchmark.calibre-decks",
                (
                    ResolvedPlatformAssetMember("drc-deck", drc_deck),
                    ResolvedPlatformAssetMember("lvs-deck", lvs_deck),
                ),
            ),
        ),
    )


class _FakeCalibre:
    def __init__(
        self,
        *,
        drc_violations: int = 0,
        lvs_result: str = "CORRECT",
        return_code: int = 0,
        malformed: bool = False,
    ) -> None:
        self.drc_violations = drc_violations
        self.lvs_result = lvs_result
        self.return_code = return_code
        self.malformed = malformed

    def __call__(self, command, *, cwd, **_kwargs):
        work = Path(cwd)
        if "-drc" in command:
            (work / "drc-results.db").write_text("contract fixture\n", encoding="utf-8")
            summary = (
                "malformed report\n"
                if self.malformed
                else (
                    f"RULECHECK M1.W.1 .... TOTAL Result Count = "
                    f"{self.drc_violations} ({self.drc_violations})\n"
                    f"TOTAL DRC Results Generated:     {self.drc_violations} "
                    f"({self.drc_violations})\n"
                )
            )
            (work / "drc-summary.rep").write_text(summary, encoding="utf-8")
            stdout = "DRC contract fixture"
        else:
            (work / "lvs.rep").write_text(
                "malformed report\n"
                if self.malformed
                else f"  {self.lvs_result} receipt_bound_top receipt_bound_top\n",
                encoding="utf-8",
            )
            for name in ("lvs.rep.ext", "calibre_erc.db", "calibre_erc.sum"):
                (work / name).write_text("contract fixture\n", encoding="utf-8")
            extracted = work / "svdb" / "receipt_bound_top.sp"
            extracted.parent.mkdir()
            extracted.write_text("contract fixture\n", encoding="utf-8")
            stdout = (
                "LVS completed. CORRECT."
                if self.lvs_result == "CORRECT"
                else "LVS completed with mismatch."
            )
        return subprocess.CompletedProcess(command, self.return_code, stdout=stdout)


def _run(
    tmp_path: Path,
    monkeypatch,
    fake: _FakeCalibre,
    *,
    corrupt: str | None = None,
    run_id: str = "7" * 32,
):
    monkeypatch.setattr(
        "sigilicon.workflows.layout_verification.run_process_group",
        fake,
    )
    registry = _registry(corrupt=corrupt)
    spec = _flow()
    engine = FlowEngine(registry)
    return engine.run(
        engine.plan(spec, "verification"),
        artifact_root=tmp_path / "artifacts",
        environment=_environment(tmp_path),
        run_id=run_id,
    )


def test_calibre_adapter_projects_clean_receipt_bound_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    result = _run(tmp_path, monkeypatch, _FakeCalibre())
    drc = drc_evidence_from_json(
        result.nodes["drc"].artifacts["evidence"].path.read_text(encoding="utf-8")
    )
    lvs = lvs_evidence_from_json(
        result.nodes["lvs"].artifacts["evidence"].path.read_text(encoding="utf-8")
    )

    assert result.status == "accepted"
    assert drc.status is PhysicalVerificationStatus.CLEAN
    assert lvs.status is PhysicalVerificationStatus.CLEAN
    assert drc.completion.proven and lvs.completion.proven
    assert drc.layout == lvs.layout
    assert drc.layout.receipt_identity is not None
    assert drc.layout.job_identity is not None
    assert drc.layout.result_identity is not None
    assert drc.layout.plan_identity
    assert drc.layout.format == "gdsii"
    assert lvs.source.artifact_identity
    assert result.nodes["drc"].facts is not None
    assert result.nodes["lvs"].facts is not None
    assert result.nodes["drc"].facts["drc-clean"] is True
    assert result.nodes["lvs"].facts["lvs-clean"] is True


def test_calibre_adapter_projects_parsed_violations_not_execution_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    result = _run(
        tmp_path,
        monkeypatch,
        _FakeCalibre(drc_violations=3, lvs_result="INCORRECT"),
        run_id="8" * 32,
    )
    drc = drc_evidence_from_json(
        result.nodes["drc"].artifacts["evidence"].path.read_text(encoding="utf-8")
    )
    lvs = lvs_evidence_from_json(
        result.nodes["lvs"].artifacts["evidence"].path.read_text(encoding="utf-8")
    )

    assert drc.status is PhysicalVerificationStatus.VIOLATED
    assert [(item.rule, item.count) for item in drc.violations] == [("M1.W.1", 3)]
    assert lvs.status is PhysicalVerificationStatus.VIOLATED
    assert [(item.category, item.count) for item in lvs.mismatches] == [
        ("INCORRECT", 1)
    ]
    assert drc.completion.proven and lvs.completion.proven


@pytest.mark.parametrize(
    "fake,expected_exit",
    (
        (_FakeCalibre(return_code=2), 2),
        (_FakeCalibre(malformed=True), 0),
    ),
)
def test_exit_or_report_failure_is_not_a_verification_conclusion(
    tmp_path: Path,
    monkeypatch,
    fake: _FakeCalibre,
    expected_exit: int,
) -> None:
    result = _run(tmp_path, monkeypatch, fake, run_id=("9" if expected_exit else "a") * 32)
    for node, loader in (("drc", drc_evidence_from_json), ("lvs", lvs_evidence_from_json)):
        evidence = loader(
            result.nodes[node].artifacts["evidence"].path.read_text(encoding="utf-8")
        )
        assert evidence.status is PhysicalVerificationStatus.EXECUTION_FAILED
        assert not evidence.completion.proven
        assert evidence.completion.exit_code == expected_exit


@pytest.mark.parametrize(
    "corrupt",
    (
        "receipt-qualifier",
        "result-qualifier",
        "source-content",
        "layout-content",
    ),
)
def test_receipt_or_layout_identity_corruption_fails_before_calibre(
    tmp_path: Path,
    monkeypatch,
    corrupt: str,
) -> None:
    calls = 0

    def forbidden(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("Calibre must not run for corrupt identity")

    monkeypatch.setattr(
        "sigilicon.workflows.layout_verification.run_process_group",
        forbidden,
    )
    registry = _registry(corrupt=corrupt)
    spec = _flow()
    engine = FlowEngine(registry)
    result = engine.run(
        engine.plan(spec, "verification"),
        artifact_root=tmp_path / "artifacts",
        environment=_environment(tmp_path),
        run_id={
            "receipt-qualifier": "b",
            "result-qualifier": "c",
            "source-content": "d",
            "layout-content": "e",
        }[corrupt]
        * 32,
    )

    assert result.status == "failed"
    assert result.nodes["drc"].execution_status == (
        "succeeded" if corrupt == "source-content" else "failed"
    )
    assert result.nodes["lvs"].execution_status == "failed"
    assert calls == (1 if corrupt == "source-content" else 0)


def test_preflight_requires_real_calibre_and_deck_assets(tmp_path: Path) -> None:
    registry = _registry()
    spec = _flow()
    engine = FlowEngine(registry)
    preflight = engine.preflight(
        engine.plan(spec, "verification"),
        ExecutionEnvironment(),
    )

    assert preflight.status == "blocked"
    assert {(check.requirement, check.status) for check in preflight.checks} >= {
        ("tool.calibre", "missing"),
        ("physical-verification", "missing"),
    }


def test_runtime_backend_unavailable_is_typed_without_a_false_conclusion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def unavailable(*_args, **_kwargs):
        raise FileNotFoundError("contract fixture backend disappeared")

    monkeypatch.setattr(
        "sigilicon.workflows.layout_verification.run_process_group",
        unavailable,
    )
    registry = _registry()
    spec = _flow()
    engine = FlowEngine(registry)
    result = engine.run(
        engine.plan(spec, "verification"),
        artifact_root=tmp_path / "artifacts",
        environment=_environment(tmp_path),
        run_id="f" * 32,
    )
    drc = drc_evidence_from_json(
        result.nodes["drc"].artifacts["evidence"].path.read_text(encoding="utf-8")
    )
    lvs = lvs_evidence_from_json(
        result.nodes["lvs"].artifacts["evidence"].path.read_text(encoding="utf-8")
    )

    assert drc.status is PhysicalVerificationStatus.BACKEND_UNAVAILABLE
    assert lvs.status is PhysicalVerificationStatus.BACKEND_UNAVAILABLE
    assert not drc.completion.executed and not lvs.completion.executed


def test_offline_adapter_is_unregistered_and_cannot_claim_clean(
    tmp_path: Path,
) -> None:
    builtin = build_flow_registry()
    assert builtin.has_adapter(CALIBRE_PHYSICAL_VERIFICATION_ADAPTER)
    assert not builtin.has_adapter(OFFLINE_PHYSICAL_VERIFICATION_ADAPTER)
    source = builtin.action(PHYSICAL_VERIFICATION_SOURCE_ACTION)
    assert source.resolves_source_assets
    assert source.adapters == (RECEIPT_BOUND_VERIFICATION_SOURCE_ADAPTER,)
    assert builtin.has_adapter(RECEIPT_BOUND_VERIFICATION_SOURCE_ADAPTER)
    assert {port.role: port.kind for port in source.outputs} == {
        "source": CANONICAL_SOURCE_NETLIST_KIND,
        "verification-policy": PHYSICAL_VERIFICATION_POLICY_KIND,
    }

    registry = _registry(offline=True)
    spec = _flow(OFFLINE_PHYSICAL_VERIFICATION_ADAPTER)
    spec = replace(
        spec,
        action_bindings=(
            ActionBinding(_INPUT_ACTION, _INPUT_ADAPTER),
            ActionBinding(
                DRC_ACTION,
                OFFLINE_PHYSICAL_VERIFICATION_ADAPTER,
                config={"outcome": "clean"},
            ),
            ActionBinding(
                LVS_ACTION,
                OFFLINE_PHYSICAL_VERIFICATION_ADAPTER,
                config={"outcome": "clean"},
            ),
        ),
    )
    engine = FlowEngine(registry)
    result = engine.run(
        engine.plan(spec, "verification"),
        artifact_root=tmp_path / "artifacts",
        environment=_environment(tmp_path),
        run_id="d" * 32,
    )

    assert result.status == "failed"
    assert result.nodes["drc"].artifacts == {}
    assert "cannot claim clean or violated" in (result.nodes["drc"].reason or "")
