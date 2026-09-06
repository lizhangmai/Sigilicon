"""Structural netlist-link adapter."""

from __future__ import annotations

import json
from sigilicon.execution.adapter import AdapterPreparation
from sigilicon.execution.artifact_reference import ArtifactProduct, StepContract
from typing import Any
from sigilicon.execution._values import ContractError, ExecutionError
from sigilicon.execution._io import ExecutionIO
from sigilicon.execution._plan import PreflightCheck, Step
from sigilicon.execution._resources import ResourceBinding, Resources
from sigilicon.execution._result import StepResult
from collections.abc import Mapping
from pathlib import Path
from sigilicon.project import Project
from sigilicon.adapters.synopsys.structural_link import (
    StructuralLinkPlan,
    execute_structural_link,
    plan_structural_link,
)
from sigilicon.canonical import canonical_digest
from dataclasses import dataclass, replace
from sigilicon.external_tools import owned_scratch_directory, process_group_cleanup_uncertainty
from sigilicon.execution.runtime import preflight_environment
from sigilicon.adapters.synopsys._common import (
    _mapping,
    _positive_integer,
    _runtime_environment,
    _safe_relative,
    _text,
)

@dataclass(frozen=True)
class _StructuralLinkAction:
    """Typed structural-link plan plus its sealed runtime path bindings."""

    plan: StructuralLinkPlan
    rtl_sources: tuple[str, ...]
    compile_script: str
    link_script: str
    release_manifest_resource: str
    release_liberty_resource: str
    timeout_seconds: int

    @property
    def record(self) -> Mapping[str, Any]:
        return {
            "owner": self.plan.owner,
            "timeout_seconds": self.timeout_seconds,
            "variant": self.plan.variant,
            "top": self.plan.top,
            "rtl_sources": list(self.rtl_sources),
            "compile_script": self.compile_script,
            "link_script": self.link_script,
            "library_name": self.plan.library_name,
            "macro_cell": self.plan.macro_cell,
            "parameter_overrides": dict(self.plan.parameter_overrides),
            "expected_macro_instances": self.plan.expected_macro_instances,
            "expected_unresolved_references": (
                self.plan.expected_unresolved_references
            ),
            "library_compiler_version": self.plan.library_compiler_version,
            "release_id": self.plan.release_id,
            "release_source_commit": self.plan.release_source_commit,
            "release_store": self.plan.release_store,
            "release_manifest_resource": self.release_manifest_resource,
            "release_manifest_sha256": self.plan.release_manifest_sha256,
            "release_liberty_resource": self.release_liberty_resource,
            "release_liberty_sha256": self.plan.release_liberty_sha256,
        }

    @property
    def identity(self) -> str:
        return canonical_digest(self.record)

    def validate(self, context: ExecutionIO) -> None:
        for source in (*self.rtl_sources, self.compile_script, self.link_script):
            context.source_path(source)
        context.resource_path(self.release_manifest_resource)
        context.resource_path(self.release_liberty_resource)

    def runtime(self, context: ExecutionIO) -> StructuralLinkPlan:
        self.validate(context)
        return replace(
            self.plan,
            rtl_sources=tuple(
                context.source_path(name) for name in self.rtl_sources
            ),
            compile_script=context.source_path(self.compile_script),
            link_script=context.source_path(self.link_script),
            release_liberty=context.resource_path(self.release_liberty_resource),
            release_sources=(
                context.resource_path(self.release_manifest_resource),
                context.resource_path(self.release_liberty_resource),
            ),
        )


class StructuralLinkAdapter:
    """Link owner RTL against one locked, uncharacterized macro release."""

    name = "synopsys.structural-link"
    _fields = frozenset(
        {
            "owner",
            "dependency",
            "dependency_lock",
            "variant",
            "variant_contract",
            "compile_script",
            "link_script",
            "library_name",
            "macro_cell",
            "parameter_overrides",
            "expected_macro_instances",
            "expected_unresolved_references",
            "library_compiler_version",
            "release_export",
            "liberty_view",
            "timeout_seconds",
        }
    )
    def _config(self, step: Step) -> Mapping[str, Any]:
        config = step.config
        unknown = set(config) - self._fields
        missing = self._fields - set(config)
        if unknown or missing:
            raise ContractError(
                "structural-link config fields disagree with its contract; "
                f"missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        return config

    @staticmethod
    def _rtl_sources(step: Step) -> tuple[str, ...]:
        sources = tuple(
            source
            for source in step.sources
            if Path(source).suffix.lower() in {".sv", ".v"}
        )
        if not sources:
            raise ContractError("structural-link filesets select no RTL sources")
        return sources

    def _configuration(self, step: Step):
        config = self._config(step)
        _text(config, "owner")
        for name in (
            "dependency_lock",
            "variant_contract",
            "compile_script",
            "link_script",
        ):
            path = _safe_relative(_text(config, name), name)
            if path not in step.sources:
                raise ContractError(
                    f"structural-link {name} must be inside the source closure"
                )
        for source in self._rtl_sources(step):
            _safe_relative(source, "structural-link RTL source")
            if source not in step.sources:
                raise ContractError(
                    "structural-link RTL source must be inside the source closure"
                )
        for name in (
            "dependency",
            "variant",
            "library_name",
            "macro_cell",
            "release_export",
            "liberty_view",
        ):
            _text(config, name)
        _mapping(config, "parameter_overrides")
        _positive_integer(config, "expected_macro_instances")
        _text(config, "library_compiler_version")
        unresolved = config.get("expected_unresolved_references")
        if type(unresolved) is not int or unresolved < 0:
            raise ContractError(
                "structural-link expected_unresolved_references must be non-negative"
            )
        _positive_integer(config, "timeout_seconds")
        if step.evidence is None:
            raise ContractError("structural-link requires an evidence envelope")
        if not step.runtime.tools:
            raise ContractError(
                "structural-link requires an owner-declared runtime profile"
            )
        return config

    def contract(self, project: Project, step: Step) -> StepContract:
        config = self._configuration(step)
        project.owner(config["owner"])
        return StepContract(produces=(ArtifactProduct("structural-link", "evidence.structural-link", "many"),))

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        self._configuration(step)
        return preflight_environment(step.runtime, resources)

    def prepare(
        self,
        project: Project,
        step: Step,
        resources: Resources,
    ) -> AdapterPreparation:
        initial = step
        config = self._configuration(initial)
        owner = _text(config, "owner")
        project.owner(owner)
        lock_name = _safe_relative(
            _text(config, "dependency_lock"), "dependency lock"
        )
        variant_name = _safe_relative(
            _text(config, "variant_contract"), "structural-link variant contract"
        )
        compile_name = _safe_relative(
            _text(config, "compile_script"), "Liberty compile script"
        )
        link_name = _safe_relative(
            _text(config, "link_script"), "structural link script"
        )
        rtl_names = tuple(
            _safe_relative(name, "structural-link RTL source")
            for name in self._rtl_sources(initial)
        )
        owner_names = (
            lock_name,
            variant_name,
            compile_name,
            link_name,
        )
        by_name = {source.path: source for source in initial.source_closure}
        missing = tuple(name for name in (*owner_names, *rtl_names) if name not in by_name)
        if missing:
            raise ContractError(
                "structural-link source closure is missing declared sources: "
                f"{sorted(set(missing))}"
            )
        if any(by_name[name].scope != "owner" for name in owner_names):
            raise ContractError(
                "structural-link configuration sources must belong to the owner"
            )
        planning = plan_structural_link(
            owner=owner,
            dependency=_text(config, "dependency"),
            dependency_lock_path=by_name[lock_name].location,
            variant_path=by_name[variant_name].location,
            variant=_text(config, "variant"),
            rtl_sources=tuple(by_name[name].location for name in rtl_names),
            compile_script=by_name[compile_name].location,
            link_script=by_name[link_name].location,
            library_name=_text(config, "library_name"),
            macro_cell=_text(config, "macro_cell"),
            parameter_overrides=_mapping(config, "parameter_overrides"),
            expected_macro_instances=_positive_integer(
                config, "expected_macro_instances"
            ),
            expected_unresolved_references=int(
                config["expected_unresolved_references"]
            ),
            library_compiler_version=_text(config, "library_compiler_version"),
            release_export=_text(config, "release_export"),
            liberty_view=_text(config, "liberty_view"),
            resources=resources,
        )
        external = (
            ResourceBinding.capture(
                planning.release_sources[0],
                identity=(
                    f"release:{_text(config, 'dependency')}:"
                    f"{planning.release_id}/manifest"
                ),
            ),
            ResourceBinding.capture(
                planning.release_sources[1],
                identity=(
                    f"release:{_text(config, 'dependency')}:"
                    f"{planning.release_id}/view/{_text(config, 'liberty_view')}"
                ),
            ),
        )
        captured_sources = tuple(by_name[name] for name in (*owner_names, *rtl_names))
        if any(not source.current() for source in captured_sources):
            raise ContractError(
                "structural-link input changed while its Step was being prepared"
            )
        if (
            external[0].sha256 != planning.release_manifest_sha256
            or external[1].sha256 != planning.release_liberty_sha256
        ):
            raise ContractError(
                "structural-link release changed while its Step was being bound"
            )
        structural_link = _StructuralLinkAction(
            planning,
            rtl_names,
            compile_name,
            link_name,
            external[0].identity,
            external[1].identity,
            _positive_integer(config, "timeout_seconds"),
        )
        return AdapterPreparation(
            action=structural_link,
            sources=captured_sources,
            resources=external,
        )

    def _execute(
        self,
        context: ExecutionIO,
        action: _StructuralLinkAction,
    ) -> StepResult:
        planning = action.runtime(context)
        runtime = _runtime_environment(context.runtime, context.step)
        try:
            library_compiler = context.step.runtime.tools[
                "SIGILICON_SYNOPSYS_LIBRARY_COMPILER"
            ]
            design_compiler = context.step.runtime.tools[
                "SIGILICON_SYNOPSYS_DC_SHELL"
            ]
        except KeyError as exc:
            raise ContractError(
                "structural-link runtime profile must bind both compiler roles"
            ) from exc
        with owned_scratch_directory(
            prefix=f"sigilicon-structural-link-{context.run_id}-",
            retain_on_error=lambda exc: process_group_cleanup_uncertainty(exc)
            is not None,
        ) as scratch:
            artifacts = context.workspace(
                "structural-link",
                {"owner": planning.owner, "variant": planning.variant},
                tool_work_root=scratch.path,
            )
            result = execute_structural_link(
                planning,
                artifacts=artifacts,
                resources=context.runtime,
                library_compiler=library_compiler,
                design_compiler=design_compiler,
                environment=runtime.values,
                timeout=action.timeout_seconds,
            )
        envelope = context.step.evidence
        if envelope is None:
            raise ExecutionError("structural-link lost its evidence envelope")
        context.write_text(
            "structural-link",
            "flow-evidence.json",
            json.dumps(
                {
                    "schema": 1,
                    "contract_kind": "structural-link-flow-evidence",
                    "plan_identity": context.plan_identity,
                    "variant": planning.variant,
                    "evidence_role": envelope.role,
                    "evidence_level": envelope.level,
                    "evidence_scope": envelope.scope,
                    "status": result.status,
                    **result.facts,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        published = context.output_artifacts(
            "structural-link",
            "evidence.structural-link",
            required=True,
        )
        return (
            StepResult.succeeded(artifacts=published)
            if result.passed
            else StepResult(
                "failed",
                published,
                message=(
                    "Synopsys structural link did not prove the declared macro seam"
                ),
            )
        )


    def run(self, context: ExecutionIO) -> StepResult:
        step = context.step
        step.validate_action()
        action = step.action
        if not isinstance(action, _StructuralLinkAction):
            raise ExecutionError("structural-link Step has no typed plan")
        return self._execute(context, action)
