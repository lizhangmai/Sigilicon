"""Managed physical verification from source files or upstream typed artifacts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import tomllib
from typing import Mapping

from sigilicon.artifacts import SafeTree, read_nofollow_text
from sigilicon.canonical import canonical_digest
from sigilicon.contracts import ContractReader, require_relative_path
from sigilicon.domain.physical_verification import PhysicalVerificationPolicy, parse_physical_verification_policy
from sigilicon.domain.platform import VerificationDeck, load_platform
from sigilicon.execution.adapter import AdapterPreparation
from sigilicon.execution._workspace import ExecutionWorkspace
from sigilicon.execution._values import ContractError, ExecutionError
from sigilicon.execution._io import ExecutionIO
from sigilicon.execution.artifact_reference import ArtifactReference, ArtifactProduct, StepContract
from sigilicon.execution._plan import PreflightCheck, Step
from sigilicon.execution._resources import ResourceBinding, Resources
from sigilicon.execution._source import Source
from sigilicon.execution._result import StepResult
from sigilicon.external_tools import owned_scratch_directory, process_group_cleanup_uncertainty
from sigilicon.project import Project
from sigilicon.adapters.mentor.physical_verification import VerificationRequest, render_run_deck, run_calibre_verification


@dataclass(frozen=True)
class _Input:
    source: str | None
    step: str | None
    role: str | None
    kinds: tuple[str, ...]
    path: str | None = None

    @classmethod
    def parse(cls, raw: Mapping, step: Step, *, layout: bool) -> _Input:
        fields = ContractReader(raw, "physical verification input")
        source = fields.text("source", None)
        dependency = fields.text("step", None)
        role = fields.text("role", None)
        member = fields.text("path", None)
        if member is not None:
            member = require_relative_path(member, "artifact member").as_posix()
        fields.finish()
        if source is not None:
            source = require_relative_path(source, "verification source").as_posix()
            if dependency is not None or role is not None or member is not None or source not in step.sources:
                raise ContractError("verification source must select exactly one file in the step closure")
        elif dependency not in step.needs or role is None:
            raise ContractError("verification artifact must select one role from a declared step dependency")
        return cls(source, dependency, role, ("layout.gds",) if layout else ("netlist.cdl",), member)

    @property
    def record(self) -> dict:
        return {"source": self.source, "step": self.step, "role": self.role, "kinds": list(self.kinds), "path": self.path}

    def stage(self, context: ExecutionIO, workspace: ExecutionWorkspace, filename: str) -> Path:
        if self.source is not None:
            return workspace.copy_file("inputs", (filename,), context.owner_source_path(self.source))
        return context.materialize_artifact(
            ArtifactReference(self.step, self.role, self.kinds[0], path=self.path),
            workspace.input_root / filename,
        )


@dataclass(frozen=True)
class _CalibreAction:
    owner: str
    cell: str
    check: str
    platform: str
    layout: _Input
    source: _Input | None
    policy: PhysicalVerificationPolicy
    deck: VerificationDeck
    deck_resource: str
    timeout: int

    @property
    def record(self) -> dict:
        return {
            "owner": self.owner, "cell": self.cell, "check": self.check, "platform": self.platform,
            "layout": self.layout.record, "source": self.source.record if self.source else None,
            "deck_resource": self.deck_resource, "timeout_seconds": self.timeout,
            "substitutions": [{"match": item.match, "replacement": item.replacement, "count": item.count}
                              for item in self.deck.substitutions],
            "policy": {"disabled_defines": dict(self.policy.drc_disabled_defines),
                       "configuration_warnings": list(self.policy.drc_configuration_warnings),
                       "waiver_layers": list(self.policy.drc_waiver_layers)},
        }

    @property
    def identity(self) -> str:
        return canonical_digest(self.record)


class CalibreAdapter:
    name = "mentor.calibre"

    def _configuration(self, project: Project, step: Step, resources=None):
        config = ContractReader(step.config, "Calibre config")
        owner = config.text("owner")
        cell = config.text("cell")
        check = config.text("check")
        platform_name = config.text("platform")
        layout = _Input.parse(config.table("layout"), step, layout=True)
        source_raw = config.take("source", None)
        source = _Input.parse(source_raw, step, layout=False) if source_raw is not None else None
        policy_name = require_relative_path(config.text("policy"), "physical verification policy").as_posix()
        timeout = config.integer("timeout_seconds", minimum=1)
        config.finish()
        if check not in {"drc", "lvs"} or (check == "lvs" and source is None):
            raise ContractError("Calibre requires drc or lvs, with an explicit LVS source")
        if step.evidence is None:
            raise ContractError("Calibre verification requires an evidence envelope")
        if policy_name not in step.sources:
            raise ContractError("verification policy must belong to the selected source closure")
        policy_path = project.owner(owner).root / policy_name
        policy_source = next((source for source in step.source_closure if source.location == policy_path), None)
        if policy_source is None:
            raise ContractError("verification policy owner disagrees with its operation source")
        policy = parse_physical_verification_policy(policy_path, tomllib.loads(policy_source.read_text()), owner=owner)
        platform = load_platform(project, platform_name, resources=resources)
        if platform.verification is None:
            raise ContractError("Calibre requires a platform verification capability")
        deck = platform.verification.require_check(check)
        identity = f"pdk:{platform.key}:verification/{check}"
        action = _CalibreAction(owner, cell, check, platform_name, layout, source, policy, deck, identity, timeout)
        return action, platform

    def contract(self, project: Project, step: Step) -> StepContract:
        action, _ = self._configuration(project, step)
        consumes = tuple(ArtifactReference(item.step, item.role, item.kinds[0], path=item.path)
                         for item in (action.layout, action.source) if item is not None and item.step is not None)
        products = (ArtifactProduct("verification", "evidence.physical-verification", path="verification/typed-evidence.json"),
                    ArtifactProduct("verification", "report.calibre", "many"))
        if action.check == "lvs":
            products += (ArtifactProduct("verification", "netlist.cdl", path="verification/extracted.sp"),)
        return StepContract(consumes, products)

    def prepare(self, project: Project, step: Step, resources: Resources) -> AdapterPreparation:
        action, platform = self._configuration(project, step, resources)
        deck, owner, cell, check, policy, identity = action.deck, action.owner, action.cell, action.check, action.policy, action.deck_resource
        render_run_deck(read_nofollow_text(deck.asset.require_path()),
            VerificationRequest(owner, cell, check, action.identity, policy, deck),
            layout_path="layout.gds", source_path="source.cdl", primary=cell, work_dir="work",
            results_path="drc-results.db", summary_path="drc-summary.rep")
        return AdapterPreparation(
            action=action,
            sources=tuple(Source.capture_document(path, document=document, root=project.project_root)
                          for path, document in sorted({platform.source_paths[0]: platform.catalog_document,
                                                       **platform.source_documents}.items())),
            resources=(ResourceBinding.capture(deck.asset.require_path(), identity=identity), resources.capture(self.name)),
        )

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        step.validate_action()
        if not isinstance(step.action, _CalibreAction):
            raise ContractError("Calibre requires its compiled action")
        tool = resources.configured_tool(self.name)
        return (PreflightCheck("executable", self.name, "ready" if tool is not None else "blocked",
                               "explicit project tool binding" if tool else "missing Calibre binding"),)

    def run(self, context: ExecutionIO) -> StepResult:
        context.step.validate_action()
        action = context.step.action
        if not isinstance(action, _CalibreAction):
            raise ExecutionError("Calibre requires its compiled action")
        request = VerificationRequest(action.owner, action.cell, action.check, context.plan_identity, action.policy, action.deck)
        with owned_scratch_directory(prefix=f"sigilicon-calibre-{context.run_id}-",
                retain_on_error=lambda exc: process_group_cleanup_uncertainty(exc) is not None) as scratch:
            workspace = context.workspace("verification", {"check": action.check, "cell": action.cell}, tool_work_root=scratch.path)
            gds = action.layout.stage(context, workspace, "layout.gds")
            cdl = action.source.stage(context, workspace, "source.cdl") if action.source else None
            evidence = run_calibre_verification(
                workspace, request, resources=context.runtime, gds=gds, source_cdl=cdl,
                deck_source=read_nofollow_text(context.resource_path(action.deck_resource)), timeout=action.timeout,
            )
            workspace.write_text("outputs", ("typed-evidence.json",), evidence.canonical_json())
            workspace.write_json("outputs", ("completion.json",), {
                "check": action.check, "cell": action.cell, "status": evidence.status.value,
                "passed": evidence.clean, "product_qualification_conclusion": False,
            })
        return StepResult("succeeded" if evidence.clean else "failed",
                          tuple(replace(item, kind=("evidence.physical-verification" if item.path.name == "typed-evidence.json"
                                                   else "netlist.cdl" if item.path.name == "extracted.sp" else "report.calibre"))
                                for item in context.output_artifacts("verification", "report.calibre", directory="verification")), message=evidence.message)
