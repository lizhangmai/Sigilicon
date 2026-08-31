from __future__ import annotations

from pathlib import Path
import struct
import subprocess

import pytest

from conftest import StagedAdapterFixture
from sigilicon.domain.post_layout import PexStatus, pex_evidence_from_json
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
from sigilicon.flow.physical_verification import CANONICAL_SOURCE_NETLIST_KIND
from sigilicon.flow.post_layout import (
    PEX_ACTION,
    PEX_NETLIST_KIND,
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
from physical_design_fixtures import routed_artifacts
from sigilicon.workflows.action_registry import build_action_registry
from sigilicon.workflows.calibre_pex import (
    CALIBRE_XRC_PEX_ADAPTER,
    render_calibre_xrc_deck,
)


_INPUT_ACTION = "contract-fixture.pex-inputs"
_INPUT_ADAPTER = "contract-pex-inputs"
_TARGET = MaterializationExecutionTarget(
    "benchmark",
    "pex_top",
    LayoutArtifactFormat.GDSII,
)


def _identity(path: Path) -> str:
    return f"fixture:{path.name}"


def _record(record_type: int, data_type: int = 0, data: bytes = b"") -> bytes:
    return struct.pack(">HBB", len(data) + 4, record_type, data_type) + data


def _gds_string(value: str) -> bytes:
    payload = value.encode("ascii")
    return payload if len(payload) % 2 == 0 else payload + b"\0"


def _contract_gds(plan) -> bytes:
    segment = plan.route_segments[0]
    return b"".join(
        (
            _record(0x00, 0x02, struct.pack(">H", 600)),
            _record(0x01, 0x02, bytes(24)),
            _record(0x02, 0x06, _gds_string("PEX-FIXTURE")),
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


class _PexInputsAdapter(StagedAdapterFixture):
    def __init__(self, *, corrupt_source: bool = False) -> None:
        self.corrupt_source = corrupt_source
        self.job, self.result = routed_artifacts("pex-contract")
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
                "sigilicon.test.pex-materializer",
                True,
                True,
                True,
                0,
            ),
            layout=managed,
            message="PEX contract materialization",
        )
        context.output_path("receipt", "receipt.json").write_text(
            receipt.canonical_json(), encoding="utf-8"
        )
        context.output_path("source", "source.cdl").write_text(
            (
                ".SUBCKT wrong_top a b\n.ENDS wrong_top\n"
                if self.corrupt_source
                else ".SUBCKT pex_top a b\n.ENDS pex_top\n"
            ),
            encoding="utf-8",
        )
        return AdapterExecution.succeeded()

    def collect_result(self, context, _execution):
        layout_path = context.output_path("layout", "layout.gds")
        receipt_path = context.output_path("receipt", "receipt.json")
        source_path = context.output_path("source", "source.cdl")
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
                    qualifiers={
                        "owner": _TARGET.owner,
                        "name": _TARGET.name,
                        "source-identity": (
                            _identity(source_path)
                        ),
                    },
                ),
            )
        )


def _deck() -> str:
    return '''LAYOUT PRIMARY "lvs_top"
LAYOUT PATH "lvs_top.gds"
SOURCE PRIMARY "lvs_top"
SOURCE PATH "lvs_top.cdl"
PEX NETLIST                    "net.dist" HSPICE LAYOUTNAMES GROUND VSS LOCATION RCNAMED
PEX NETLIST SIMPLE             "net.simple" HSPICE LAYOUTNAMES LOCATION RCNAMED
//PEX NETLIST                    "net.dist" HSPICE SOURCENAMES GROUND VSS LOCATION RCNAMED
//PEX NETLIST SIMPLE             "net.simple" HSPICE SOURCENAMES LOCATION RCNAMED
'''


def _environment(tmp_path: Path, *, symlink_support: bool = False) -> ExecutionEnvironment:
    executable = tmp_path / "calibre"
    executable.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    executable.chmod(0o755)
    support = tmp_path / "pex-kit"
    support.mkdir()
    deck = support / "calibre.rcx"
    deck.write_text(_deck(), encoding="utf-8")
    (support / "rules").write_text("contract rules\n", encoding="utf-8")
    if symlink_support:
        (support / "unsafe").symlink_to(tmp_path / "outside")
    return ExecutionEnvironment(
        capabilities={
            "tool.pex": ResolvedCapability("calibre-xrc.contract-fixture", executable)
        },
        platform_assets=(
            ResolvedPlatformAsset(
                "physical-pex",
                "platform.pex",
                "benchmark.calibre-xrc",
                (
                    ResolvedPlatformAssetMember("pex-deck", deck),
                    ResolvedPlatformAssetMember("pex-support-root", support),
                ),
            ),
        ),
    )


class _FakeXrc:
    def __init__(self, *, fail_stage: str | None = None, malformed: bool = False) -> None:
        self.fail_stage = fail_stage
        self.malformed = malformed
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command, *, cwd, **_kwargs):
        argv = tuple(command)
        self.calls.append(argv)
        stage = next(value for value in ("phdb", "pdb", "fmt") if f"-{value}" in argv)
        if self.fail_stage == stage:
            return subprocess.CompletedProcess(argv, 2, stdout=f"{stage} failed")
        work = Path(cwd)
        if stage == "phdb":
            stdout = "--- CALIBRE xRC::PHDB GENERATOR COMPLETED"
        elif stage == "pdb":
            stdout = (
                "----- CALIBRE xRC::HIERARCHICAL PARASITIC EXTRACTION COMPLETED\n"
                "                          xRC Errors  =  0\n"
                "Total Number of Nets 14 Total Extracted Nets 3"
            )
        else:
            (work / "extracted.pex").write_text(
                '* Design: pex_top\n* Created: "volatile fixture time"\n'
                '.include "extracted.pex.pex"\n'
                ".subckt PM_pex_top%a 0 n1 a\n"
                "c_helper n1 0 0.1f\n"
                ".ends PM_pex_top%a\n"
                ".subckt pex_top a b\n"
                "xM0 n1 a 0 0 nch_mac L=30n W=100n\n"
                '.include "extracted.pex.pex_top.pxi"\n.ends\n',
                encoding="utf-8",
            )
            (work / "extracted.pex.pex").write_text(
                "c1 n1 0 0.2f\nr1 n1 b 4.0\n", encoding="utf-8"
            )
            (work / "extracted.pex.pex_top.pxi").write_text(
                "x_PM_pex_top%a 0 n1 a PM_pex_top%a\n", encoding="utf-8"
            )
            stdout = (
                "--- CALIBRE xRC::FORMATTER COMPLETED\n"
                "                          xRC Errors  =  0\n"
                "Ascii file \"extracted.pex\" created."
            )
            if self.malformed:
                stdout = "formatter returned without authoritative completion"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout)


def _registry(*, corrupt_source: bool = False):
    registry = build_action_registry()
    registry.register_action(
        ActionContract(
            _INPUT_ACTION,
            outputs=(
                ArtifactPort("layout", MATERIALIZED_GDS_KIND),
                ArtifactPort("receipt", MATERIALIZATION_RECEIPT_KIND),
                ArtifactPort("source", CANONICAL_SOURCE_NETLIST_KIND),
            ),
            adapters=(_INPUT_ADAPTER,),
        )
    )
    registry.register_adapter(
        _INPUT_ADAPTER,
        _PexInputsAdapter(corrupt_source=corrupt_source),
    )
    return registry


def _flow():
    spec = FlowSpec(
        owner="benchmark",
        flow_id="receipt-bound-pex",
        recipe_id="receipt-bound-pex-recipe",
        nodes=(
            FlowNode("inputs", _INPUT_ACTION),
            FlowNode(
                "pex",
                PEX_ACTION,
                bindings=(
                    ArtifactBinding("layout", "inputs", "layout"),
                    ArtifactBinding("receipt", "inputs", "receipt"),
                    ArtifactBinding("source", "inputs", "source"),
                ),
            ),
        ),
        targets=(FlowTarget("pex", ("pex",)),),
        action_bindings=(
            ActionBinding(_INPUT_ACTION, _INPUT_ADAPTER),
            ActionBinding(
                PEX_ACTION,
                CALIBRE_XRC_PEX_ADAPTER,
                platform_assets={"physical-pex": "benchmark.calibre-xrc"},
            ),
        ),
    )
    return spec


def _run(tmp_path: Path, monkeypatch, fake: _FakeXrc, *, run_id: str = "1" * 32):
    monkeypatch.setattr("sigilicon.workflows.calibre_pex.run_process_group", fake)
    engine = FlowEngine(_registry())
    spec = _flow()
    return engine.run(
        engine.plan(spec, "pex"),
        artifact_root=tmp_path / "artifacts",
        environment=_environment(tmp_path),
        run_id=run_id,
    )


def test_calibre_xrc_adapter_projects_receipt_bound_parasitics(
    tmp_path: Path, monkeypatch
) -> None:
    fake = _FakeXrc()
    result = _run(tmp_path, monkeypatch, fake)
    node = result.nodes["pex"]
    evidence = pex_evidence_from_json(
        node.artifacts["evidence"].path.read_text(encoding="utf-8")
    )
    parasitics = node.artifacts["parasitics"].path.read_text(encoding="utf-8")

    assert result.status == "accepted"
    assert evidence.status is PexStatus.EXTRACTED
    assert evidence.completion.proven
    assert evidence.parasitics is not None
    assert evidence.parasitics.identity.endswith(":parasitics")
    assert '.include "extracted.pex' not in parasitics
    assert "volatile fixture time" not in parasitics
    assert "* Created: normalized by Sigilicon" in parasitics
    assert ".subckt pex_top a b" in parasitics
    assert "xM0 " in parasitics
    assert "c1 " in parasitics
    assert "r1 " in parasitics
    assert node.facts is not None
    assert node.facts.as_mapping() == {
        "pex-status": "extracted",
        "pex-completed": True,
    }
    assert [next(item for item in ("phdb", "pdb", "fmt") if f"-{item}" in call) for call in fake.calls] == [
        "phdb",
        "pdb",
        "fmt",
    ]


@pytest.mark.parametrize("failure", ("phdb", "pdb", "fmt", "malformed"))
def test_calibre_xrc_failure_never_publishes_parasitics(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    fake = _FakeXrc(
        fail_stage=None if failure == "malformed" else failure,
        malformed=failure == "malformed",
    )
    result = _run(tmp_path, monkeypatch, fake, run_id=f"{len(failure):x}" * 32)
    node = result.nodes["pex"]
    evidence = pex_evidence_from_json(
        node.artifacts["evidence"].path.read_text(encoding="utf-8")
    )

    assert evidence.status is PexStatus.EXECUTION_FAILED
    assert not evidence.completion.proven
    assert "parasitics" not in node.artifacts
    assert node.facts is not None
    assert node.facts.as_mapping() == {
        "pex-status": "execution_failed",
        "pex-completed": False,
    }


def test_calibre_xrc_identity_drift_fails_before_tool(
    tmp_path: Path, monkeypatch
) -> None:
    fake = _FakeXrc()
    monkeypatch.setattr("sigilicon.workflows.calibre_pex.run_process_group", fake)
    engine = FlowEngine(_registry(corrupt_source=True))
    spec = _flow()
    result = engine.run(
        engine.plan(spec, "pex"),
        artifact_root=tmp_path / "artifacts",
        environment=_environment(tmp_path),
        run_id="d" * 32,
    )

    assert result.status == "failed"
    assert result.nodes["pex"].execution_status == "failed"
    assert fake.calls == []


def test_calibre_xrc_rejects_symlinked_support_tree_before_tool(
    tmp_path: Path, monkeypatch
) -> None:
    fake = _FakeXrc()
    monkeypatch.setattr("sigilicon.workflows.calibre_pex.run_process_group", fake)
    engine = FlowEngine(_registry())
    spec = _flow()
    result = engine.run(
        engine.plan(spec, "pex"),
        artifact_root=tmp_path / "artifacts",
        environment=_environment(tmp_path, symlink_support=True),
        run_id="e" * 32,
    )

    assert result.status == "failed"
    assert fake.calls == []


def test_calibre_xrc_deck_renderer_is_exact_and_rejects_injection() -> None:
    rendered = render_calibre_xrc_deck(
        _deck(),
        layout_path="/proc/self/fd/10/layout.gds",
        source_path="/proc/self/fd/11/source.cdl",
        primary="pex_top",
    )
    assert 'LAYOUT PRIMARY "pex_top"' in rendered
    assert 'PEX NETLIST                    "extracted.pex"' in rendered
    assert '//PEX NETLIST                    "net.dist"' in rendered

    with pytest.raises(ValueError, match="safe SVRF"):
        render_calibre_xrc_deck(
            _deck(),
            layout_path='layout.gds"\nPEX NETLIST "owned"',
            source_path="source.cdl",
            primary="pex_top",
        )
    with pytest.raises(ValueError, match="primary"):
        render_calibre_xrc_deck(
            _deck(),
            layout_path="layout.gds",
            source_path="source.cdl",
            primary="../pex_top",
        )
    with pytest.raises(RuntimeError, match="changed"):
        render_calibre_xrc_deck(
            _deck().replace('LAYOUT PRIMARY "lvs_top"', 'LAYOUT PRIMARY "other"'),
            layout_path="layout.gds",
            source_path="source.cdl",
            primary="pex_top",
        )


def test_builtin_registry_owns_calibre_xrc_pex_adapter() -> None:
    registry = build_action_registry()
    assert registry.has_adapter(CALIBRE_XRC_PEX_ADAPTER)
    assert CALIBRE_XRC_PEX_ADAPTER in registry.action(PEX_ACTION).adapters
    assert registry.action(PEX_ACTION).output("parasitics").required is False
    assert registry.action(PEX_ACTION).output("parasitics").kind == PEX_NETLIST_KIND
