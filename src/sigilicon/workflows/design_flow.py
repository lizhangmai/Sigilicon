"""Stateless Adapter for explicitly planned source and electrical checks."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any

from sigilicon.external_tools import (
    cadence_subprocess_env,
    owned_input_file,
    owned_sealed_input,
    run_process_group_capture,
)
from sigilicon.flow import (
    ActionContext,
    AdapterResult,
    CollectedActionResult,
    FlowExecutionError,
    ProducedArtifact,
    SourceMember,
)
from sigilicon.flow.circuit_design import (
    DESIGN_ACTION_PLAN,
    DESIGN_ELECTRICAL_DIAGNOSTIC_ACTION,
    DESIGN_SOURCE_CHECK_ACTION,
)
from sigilicon.flow.source_assets import source_member_matches
from sigilicon.flow.serialization import json_value
from sigilicon.workflows.design_targets import (
    DesignMode,
    DesignTarget,
)
from sigilicon.workflows.run_artifacts import (
    FlowRunArtifacts,
    managed_run_artifact_environment,
)
from sigilicon.workflows.source_control import (
    artifact_source_state,
)


@dataclass(frozen=True)
class DesignActionPlan:
    """One selected design target/mode with explicit source members."""

    target: DesignTarget
    mode: DesignMode

    def as_dict(self) -> dict[str, object]:
        return {
            "target": self.target.name,
            "mode": self.mode.name,
            "kind": self.target.kind,
            "entrypoint": self.target.entrypoint,
            "spec": (
                None
                if self.target.spec_relative is None
                else self.target.spec_relative.as_posix()
            ),
            "default_args": list(self.mode.default_args),
        }


class DesignTargetAdapter:
    """Run one owner-declared command inside its current Flow Action."""

    def run(self, context: ActionContext) -> AdapterResult:
        selected = context.require_action_plan(
            DESIGN_ACTION_PLAN,
            DesignActionPlan,
        )
        assert context.action_plan is not None
        target = selected.target
        mode = selected.mode
        scope = context.require_project_scope()
        project = scope.project
        if (
            scope.owner != target.owner
            or scope.project.project_root != target.project_root
        ):
            raise FlowExecutionError("design Action Plan project owner scope drift")
        if json_value(context.action_plan.record) != selected.as_dict():
            raise FlowExecutionError("typed design Action Plan record drift")
        allowed = {
            "target",
            "mode",
            "evidence_role",
            "evidence_level",
            "evidence_scope",
        }
        unknown = set(context.action_config) - allowed
        if unknown:
            raise FlowExecutionError(
                f"design Action contains unknown configuration: {sorted(unknown)}"
            )
        target_name = self._text(context, "target")
        mode_name = self._text(context, "mode")
        evidence = context.require_evidence()
        evidence_role = evidence.role
        evidence_level = evidence.level
        evidence_scope = evidence.scope
        if target_name != target.name or mode_name != mode.name:
            raise FlowExecutionError("design Action target or mode drift")
        route_sources = context.action_plan.sources
        expected_sources = (target.catalog_member, *target.source_members)
        provided = {
            member.location: member.record_text
            for member in route_sources
        }
        if any(
            provided.get(member.location) != member.record_text
            for member in expected_sources
        ):
            raise FlowExecutionError("typed design Action Plan source closure drift")
        if not self._sources_match(route_sources):
            raise FlowExecutionError("owner design source changed after Flow planning")

        extra_args: tuple[str, ...] = ()
        environment = os.environ.copy()
        environment.pop("SIGILICON_MANAGED_RUN_ARTIFACTS", None)
        managed_electrical = False
        if context.action.kind == DESIGN_ELECTRICAL_DIAGNOSTIC_ACTION:
            capability = context.capabilities["tool.cadence-spectre"]
            if capability.executable is None:
                raise FlowExecutionError(
                    "Spectre design diagnostic requires an executable capability"
                )
            extra_args = ("--spectre", str(capability.executable))
            environment = cadence_subprocess_env()
            managed_electrical = True
        elif context.action.kind != DESIGN_SOURCE_CHECK_ACTION:
            raise FlowExecutionError(
                f"unsupported project design Action: {context.action.kind}"
            )
        timeout = context.adapter_config.get("timeout_seconds", 600)
        if (
            set(context.adapter_config) != {"timeout_seconds"}
            or isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or timeout <= 0
        ):
            raise FlowExecutionError(
                "design Action profile requires one positive timeout_seconds"
            )
        source = artifact_source_state(project.project_root)
        source["design_route"] = [
            {
                "path": member.path,
                "sha256": hashlib.sha256(
                    member.record_text.encode("utf-8")
                ).hexdigest(),
                "executable": member.executable,
            }
            for member in route_sources
        ]
        if managed_electrical:
            environment.update(
                managed_run_artifact_environment(context, "evidence", source)
            )
        runner_member = next(
            member
            for member in target.source_members
            if member.location == target.entrypoint_path
        )
        spec_member = next(
            (
                member
                for member in target.source_members
                if target.spec is not None and member.location == target.spec
            ),
            None,
        )
        with ExitStack() as stack:
            runner_source = stack.enter_context(
                owned_input_file(runner_member.location)
            )
            spec_source = (
                None
                if spec_member is None
                else stack.enter_context(owned_input_file(spec_member.location))
            )
            if not self._owned_source_matches(runner_source.fd, runner_member) or (
                spec_member is not None
                and (
                    spec_source is None
                    or not self._owned_source_matches(spec_source.fd, spec_member)
                )
            ):
                raise FlowExecutionError(
                    "owner design source changed while binding runner inputs"
                )
            sealed_runner = stack.enter_context(
                owned_sealed_input(
                    runner_member.record_text.encode("utf-8"),
                    name="design-runner.py",
                )
            )
            sealed_spec = (
                None
                if spec_member is None
                else stack.enter_context(
                    owned_sealed_input(
                        spec_member.record_text.encode("utf-8"),
                        name="design-spec.toml",
                    )
                )
            )
            pass_fds = (sealed_runner.fd,)
            if sealed_spec is not None:
                pass_fds = (*pass_fds, sealed_spec.fd)
            completed = run_process_group_capture(
                target.bound_command(
                    mode.name,
                    runner_path=sealed_runner.child_path,
                    spec_path=(
                        None if sealed_spec is None else sealed_spec.child_path
                    ),
                    extra_args=extra_args,
                ),
                cwd=project.project_root,
                env=environment,
                timeout=timeout,
                pass_fds=pass_fds,
            )
        artifacts = FlowRunArtifacts(context, "evidence", source)
        stdout = artifacts.write_text(
            "logs",
            ("design-runner.stdout.log",),
            completed.stdout,
        )
        stderr = artifacts.write_text(
            "logs",
            ("design-runner.stderr.log",),
            completed.stderr or "",
        )
        if not self._sources_match(route_sources):
            raise FlowExecutionError("owner design source changed during Flow execution")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise FlowExecutionError("design runner did not emit one JSON result") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("passed"), bool):
            raise FlowExecutionError(
                "design runner result must be an object with boolean passed"
            )
        if completed.returncode != 0 and payload["passed"]:
            tail = "\n".join((completed.stderr or completed.stdout).splitlines()[-80:])
            raise FlowExecutionError(
                "design runner reported passed=true but exited "
                f"{completed.returncode}:\n{tail}"
            )
        runner_result = self._runner_result(payload)
        result_payload = {
            "schema": 1,
            "contract_kind": "design-target-evidence",
            "owner": target.owner,
            "target": target.name,
            "mode": mode.name,
            "runner_result": runner_result,
            "process_returncode": completed.returncode,
            "source": source,
            "evidence_role": evidence_role,
            "evidence_level": evidence_level,
            "evidence_scope": evidence_scope,
            "product_qualification_conclusion": False,
        }
        evidence = context.output_path("evidence", "design-evidence.json")
        evidence.write_text(
            json.dumps(result_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return AdapterResult.succeeded(
            CollectedActionResult(
                artifacts=(
                    ProducedArtifact(
                        "evidence",
                        context.action.output("evidence").kind,
                        evidence,
                    ),
                ),
                facts={
                    "passed": payload["passed"],
                    "execution-completed": True,
                    "process-returncode": completed.returncode,
                    "evidence-role": evidence_role,
                    "evidence-level": evidence_level,
                    "evidence-scope": evidence_scope,
                    "product-qualification-conclusion": False,
                },
                evidence=(stdout, stderr),
                details={
                    "target": target.name,
                    "mode": mode.name,
                    "process_returncode": completed.returncode,
                    "product_qualification_conclusion": False,
                },
            )
        )

    @staticmethod
    def _sources_match(sources: tuple[SourceMember, ...]) -> bool:
        try:
            return all(source_member_matches(source) for source in sources)
        except (OSError, RuntimeError, UnicodeError):
            return False

    @staticmethod
    def _owned_source_matches(descriptor: int, source: SourceMember) -> bool:
        metadata = os.fstat(descriptor)
        executable = bool(
            metadata.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        )
        chunks: list[bytes] = []
        offset = 0
        while chunk := os.pread(descriptor, 1024 * 1024, offset):
            chunks.append(chunk)
            offset += len(chunk)
        try:
            record = b"".join(chunks).decode("utf-8")
        except UnicodeError:
            return False
        return record == source.record_text and executable == source.executable

    @staticmethod
    def _runner_result(payload: dict[str, Any]) -> dict[str, Any]:
        """Project untrusted runner JSON onto the portable evidence contract."""

        result: dict[str, Any] = {"passed": payload["passed"]}
        for field in ("reason", "status"):
            value = payload.get(field)
            if (
                isinstance(value, str)
                and len(value) <= 4000
                and not Path(value).is_absolute()
            ):
                result[field] = value
        if payload.get("product_qualification_conclusion") is False:
            result["product_qualification_conclusion"] = False
        return result

    @staticmethod
    def _text(context: ActionContext, field: str) -> str:
        value = context.action_config.get(field)
        if not isinstance(value, str) or not value:
            raise FlowExecutionError(f"design Action {field} must be non-empty text")
        return value


__all__ = ["DesignActionPlan", "DesignTargetAdapter"]
