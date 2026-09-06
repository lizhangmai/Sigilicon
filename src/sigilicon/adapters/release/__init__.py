"""Managed publication adapter for immutable IP releases."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from sigilicon.execution.adapter import AdapterPreparation
from sigilicon.execution.artifact_reference import ArtifactProduct, StepContract
from sigilicon.execution._result import Artifact, StepResult
from sigilicon.execution._values import ContractError, ExecutionError
from sigilicon.execution._io import ExecutionIO
from sigilicon.execution._plan import PreflightCheck, Step
from sigilicon.execution._resources import Resources
from sigilicon.execution._source import Source
from sigilicon.adapters.release.ip_packaging import _publish_ip_release
from sigilicon.adapters.release.ip_release_planning import plan_ip_release
from sigilicon.adapters.release.release_plan_record import IpReleaseError, IpReleasePlan
from sigilicon.release_store import release_store_resource
from sigilicon.project import Project


def _text(config: Mapping[str, Any], name: str) -> str:
    value = config.get(name)
    if not isinstance(value, str) or not value:
        raise ContractError(f"release step requires non-empty {name!r}")
    return value


@dataclass(frozen=True)
class _ReleaseAction:
    plan: IpReleasePlan
    sources: Mapping[Path, tuple[str, str, str]]
    store_root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.plan, IpReleasePlan):
            raise ContractError("release action requires an IpReleasePlan")
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))
        object.__setattr__(self, "store_root", self.store_root.absolute())

    @property
    def record(self) -> Mapping[str, Any]:
        return {
            "release": self.plan.record,
            "sources": tuple(
                sorted(
                    (scope, name, digest)
                    for scope, name, digest in self.sources.values()
                )
            ),
            "native_bundles": tuple(
                sorted(
                    (
                        export,
                        role,
                        hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    )
                    for (export, role), text in self.plan.native_bundles.items()
                )
            ),
        }

    def source_paths(self, context: ExecutionIO) -> Mapping[Path, Path]:
        context.step.validate_action()
        return MappingProxyType(
            {
                original: context.scoped_source_path(scope, name)
                for original, (scope, name, _digest) in self.sources.items()
            }
        )


class IpReleaseAdapter:
    """Plan and publish a release through the normal managed-run lifecycle."""

    name = "sigilicon.ip-release"
    _fields = frozenset({"owner", "maturity"})

    def _configuration(self, project: Project, step: Step):
        unknown = set(step.config) - self._fields
        if unknown:
            raise ContractError(
                f"release step has unknown config fields: {sorted(unknown)}"
            )
        owner_name = _text(step.config, "owner")
        owner = project.owner(owner_name)
        contract = getattr(owner, "release_contract", None)
        if not isinstance(contract, Path):
            raise ContractError(f"owner {owner_name!r} has no release contract")
        maturity = step.config.get("maturity")
        if maturity is not None and (not isinstance(maturity, str) or not maturity):
            raise ContractError("release maturity must be non-empty text")
        from sigilicon.domain.ip_release import load_ip_contract
        load_ip_contract(contract, project=project)
        if maturity is not None and maturity not in {"development", "implementation", "signoff"}:
            raise ContractError("unsupported release maturity")
        return owner, contract, maturity

    def contract(self, project: Project, step: Step) -> StepContract:
        self._configuration(project, step)
        return StepContract(produces=(ArtifactProduct("release", "summary.ip-release", "many"),))

    def prepare(
        self,
        project: Project,
        step: Step,
        _resources: Resources,
    ) -> AdapterPreparation:
        owner, contract, maturity = self._configuration(project, step)
        release = plan_ip_release(contract, project=project, maturity=maturity)
        owner_root = owner.root.resolve()
        project_root = project.project_root.resolve()
        sources: dict[Path, tuple[str, str, str]] = {}
        captured: list[Source] = []
        for relative in release.source_files:
            original = (project_root / relative).absolute()
            if original != original.resolve() or not original.is_file():
                raise ContractError(f"release source is missing or unsafe: {relative}")
            if original.is_relative_to(owner_root):
                scope = "owner"
                root = owner_root
            else:
                scope = "project"
                root = project_root
            source = Source.capture(original, root=root, scope=scope)
            sources[original] = (scope, source.path, source.sha256)
            captured.append(source)
        store_identity = release_store_resource(release.store)
        action = _ReleaseAction(
            release,
            sources,
            _resources.require_destination(store_identity),
        )
        return AdapterPreparation(
            action=action,
            sources=tuple(captured),
            resources=(_resources.capture(store_identity), _resources.capture("vcs.git")),
        )

    def preflight(
        self,
        step: Step,
        _resources: Resources,
    ) -> tuple[PreflightCheck, ...]:
        action = step.action
        if not isinstance(action, _ReleaseAction):
            raise ContractError("release step has no planned release action")
        release = action.plan
        missing = release.missing_items
        clean = not release.working_tree_dirty
        complete = not missing
        return (
            PreflightCheck(
                "source-checkout",
                release.source_commit,
                "ready" if clean else "blocked",
                "clean source checkout" if clean else "source checkout is dirty",
            ),
            PreflightCheck(
                "release-maturity",
                release.maturity,
                "ready" if complete else "blocked",
                "required collateral is complete"
                if complete
                else "missing: " + ", ".join(missing),
            ),
        )

    def run(self, context: ExecutionIO) -> StepResult:
        action = context.step.action
        if not isinstance(action, _ReleaseAction):
            raise ExecutionError("release Step has no planned release action")
        try:
            published = _publish_ip_release(
                action.plan,
                store_root=action.store_root,
                source_paths=action.source_paths(context),
                resources=context.runtime,
            )
        except (IpReleaseError, OSError, RuntimeError, ValueError) as exc:
            raise ExecutionError(f"IP release publication failed: {exc}") from exc
        summary = context.write_text(
            "release",
            "summary.json",
            json.dumps(published, sort_keys=True, indent=2) + "\n",
        )
        return StepResult.succeeded(
            artifacts=(Artifact("release", "summary.ip-release", summary),),
        )


def release_adapters():
    from sigilicon.adapters.release.build_artifacts import BuildArtifactReleaseAdapter
    return (IpReleaseAdapter(), BuildArtifactReleaseAdapter())


__all__ = ["IpReleaseAdapter", "release_adapters"]
