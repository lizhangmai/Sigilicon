"""Source-bound composition of one owner's targets, operations, and recipes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import tomllib

from sigilicon.artifacts import read_nofollow_text
from sigilicon.domain.repository import Project, RepositoryOwner
from sigilicon.domain.targets import (
    OwnerOperation,
    OwnerTarget,
    OwnerTargetCatalog,
    load_owner_target_catalog,
)
from sigilicon.flow import (
    FlowSpec,
    FlowTarget,
    SourceMember,
    compile_flow_spec,
    parse_execution_recipe,
)
from sigilicon.flow.source_assets import snapshot_source_member, source_member_matches


@dataclass(frozen=True)
class CompiledOwnerOperation:
    """One owner target operation compiled from an exact source closure."""

    owner: RepositoryOwner
    target: OwnerTarget
    operation: OwnerOperation
    spec: FlowSpec

    def __post_init__(self) -> None:
        if self.spec.owner != self.owner.name:
            raise ValueError("compiled operation owner drift")
        if self.spec.owner_root != self.owner.root:
            raise ValueError("compiled operation owner root drift")
        if self.spec.flow_id != self.target.name:
            raise ValueError("compiled operation target drift")
        if tuple(target.target_id for target in self.spec.targets) != (
            self.operation.name,
        ):
            raise ValueError("compiled operation identity drift")


@dataclass(frozen=True)
class OwnerWorkflow:
    """Compile every design style through the same owner operation contract."""

    project: Project
    owner: RepositoryOwner

    def __post_init__(self) -> None:
        if not isinstance(self.project, Project):
            raise ValueError("owner workflow requires an explicit Project")
        if self.owner not in self.project.owners:
            raise ValueError(
                f"owner {self.owner.name!r} does not belong to the selected Project"
            )

    def targets(self) -> tuple[OwnerTarget, ...]:
        """Return the exact current target inventory in catalog order."""

        catalog, _ = self._catalog_snapshot()
        return tuple(catalog.targets.values())

    def target(self, name: str) -> OwnerTarget:
        """Return one source-validated owner target."""

        targets = self.targets()
        for target in targets:
            if target.name == name:
                return target
        available = ", ".join(target.name for target in targets)
        raise ValueError(
            f"unknown target {name!r} for owner {self.owner.name!r}; "
            f"available targets: {available}"
        )

    def compile(self, target: str, operation: str) -> CompiledOwnerOperation:
        """Compile one explicit operation and bind its complete owner source set."""

        catalog, catalog_source = self._catalog_snapshot()
        selected_target = catalog.get(target)
        selected_operation = selected_target.operation(operation)
        recipe_path = self.owner.root.joinpath(*selected_operation.recipe.parts)
        try:
            recipe_record = read_nofollow_text(recipe_path)
            raw = tomllib.loads(recipe_record)
        except (OSError, RuntimeError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(
                f"cannot read execution recipe {recipe_path}: {exc}"
            ) from exc
        recipe = parse_execution_recipe(
            raw,
            recipe_path,
            owner_root=self.owner.root,
        )
        if recipe.owner != self.owner.name:
            raise ValueError("execution recipe owner disagrees with target owner")
        input_sources = [
            catalog_source,
            snapshot_source_member(
                recipe_path,
                source_root=self.project.project_root,
                scope="project",
                record_text=recipe_record,
                source_label="execution recipe",
            ),
        ]
        for name, declaration in recipe.inputs.items():
            if declaration.kind != "owner-path":
                continue
            value = selected_target.inputs.get(name)
            if not isinstance(value, str):
                continue
            relative = PurePosixPath(value)
            if (
                relative.is_absolute()
                or "\\" in value
                or relative.as_posix() != value
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                continue
            configured = self.owner.root.joinpath(*relative.parts)
            resolved = configured.resolve(strict=False)
            if configured != resolved or not resolved.is_relative_to(self.owner.root):
                continue
            if resolved.is_file():
                member = snapshot_source_member(
                    resolved,
                    source_root=self.project.project_root,
                    scope="project",
                    source_label=f"execution input {name}",
                )
                if not any(
                    (existing.scope, existing.source_root, existing.path)
                    == (member.scope, member.source_root, member.path)
                    for existing in input_sources
                ):
                    input_sources.append(member)
        sources = tuple(input_sources)
        self._require_current(sources)
        spec = compile_flow_spec(
            recipe,
            flow_id=selected_target.name,
            targets=(
                FlowTarget(selected_operation.name, selected_operation.goals),
            ),
            inputs=selected_target.inputs,
            source_members=sources,
        )
        self._require_current(sources)
        return CompiledOwnerOperation(
            self.owner,
            selected_target,
            selected_operation,
            spec,
        )

    def _catalog_snapshot(self) -> tuple[OwnerTargetCatalog, SourceMember]:
        snapshot = self.project.owner_target_catalog(self.owner)
        catalog = load_owner_target_catalog(
            self.project,
            self.owner,
            catalog_snapshot=snapshot,
        )
        source = snapshot_source_member(
            snapshot.path,
            source_root=self.project.project_root,
            scope="project",
            record_text=snapshot.record_text,
            source_label="owner targets",
        )
        self._require_current((source,))
        return catalog, source

    @staticmethod
    def _require_current(sources: tuple[SourceMember, ...]) -> None:
        try:
            current = all(source_member_matches(source) for source in sources)
        except (OSError, RuntimeError, UnicodeError):
            current = False
        if not current:
            raise ValueError("target selection source changed during planning")


__all__ = ["CompiledOwnerOperation", "OwnerWorkflow"]
