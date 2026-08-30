"""Project-bound Adapter for cataloged source and electrical design checks."""

from __future__ import annotations

from contextlib import ExitStack
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
    DESIGN_ELECTRICAL_DIAGNOSTIC_ACTION,
    DESIGN_SOURCE_CHECK_ACTION,
)
from sigilicon.flow.source_assets import source_member_matches
from sigilicon.workflows.design_targets import (
    DesignTargetCatalog,
)
from sigilicon.workflows.run_artifacts import (
    FlowRunArtifacts,
    managed_run_artifact_environment,
)
from sigilicon.workflows.source_control import (
    artifact_source_state,
    inspect_source_state,
)


_EVIDENCE_ROLES = frozenset({"diagnostic", "regression"})
_EVIDENCE_LEVELS = frozenset({"l0", "l1", "l2", "l3", "l4"})


class ProjectDesignTargetAdapter:
    """Run one owner-declared command inside its current Flow Action."""

    def __init__(
        self,
        project: Any,
        owner: str,
        catalog: DesignTargetCatalog,
    ) -> None:
        self._project = project
        self._owner = project.owner(owner)
        self._catalog = catalog
        self._source_state = (
            inspect_source_state(self._owner.root) if catalog.targets else None
        )

    def run(self, context: ActionContext) -> AdapterResult:
        scope = context.require_project_scope()
        if (
            scope.owner != self._owner.name
            or scope.owner_root != self._owner.root
            or scope.project.project_root != self._project.project_root
        ):
            raise FlowExecutionError("design Action project owner scope drift")
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
        evidence_role = self._text(context, "evidence_role")
        evidence_level = self._text(context, "evidence_level")
        evidence_scope = self._text(context, "evidence_scope")
        if evidence_role not in _EVIDENCE_ROLES:
            raise FlowExecutionError("design Action evidence_role is not supported")
        if evidence_level not in _EVIDENCE_LEVELS:
            raise FlowExecutionError("design Action evidence_level is not supported")
        try:
            target = self._catalog.get(target_name)
            mode = target.get_mode(mode_name)
        except ValueError as exc:
            raise FlowExecutionError(str(exc)) from exc
        if target.owner != self._owner.name:
            raise FlowExecutionError("design target belongs to a different owner")
        route_sources = self._catalog.source_members_for(target)
        if (
            self._source_state is None
            or inspect_source_state(self._owner.root) != self._source_state
            or not self._sources_match(route_sources)
        ):
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
        source = artifact_source_state(self._project.project_root)
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
                cwd=self._project.project_root,
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
        if (
            inspect_source_state(self._owner.root) != self._source_state
            or not self._sources_match(route_sources)
        ):
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
            "owner": self._owner.name,
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


__all__ = ["ProjectDesignTargetAdapter"]
