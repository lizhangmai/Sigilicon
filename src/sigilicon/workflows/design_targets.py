"""Project-owned design intent catalog mapped onto typed Flow targets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import stat
import sys
from typing import Mapping

from sigilicon.artifacts import read_nofollow_text
from sigilicon.domain.config_contracts import require_config_header
from sigilicon.domain.repository import OwnerCatalogSnapshot, Project
from sigilicon.flow.model import SourceMember
from sigilicon.flow.source_assets import source_member_matches

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_MODULE_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z"
)
_SHARED_MODULES = frozenset({"sigilicon.cli.design_lifecycle"})
_KINDS = frozenset({"script", "module"})
_SPEC_ARGUMENTS = frozenset({"--spec", "--design"})
_ROUTING_ARGUMENTS = frozenset({"--spec", "--design", "--mode"})
_HEADER_FIELDS = frozenset({"schema", "contract_kind", "path_scope", "owner"})
_TARGET_FIELDS = frozenset(
    {
        "description",
        "kind",
        "entrypoint",
        "spec_argument",
        "spec",
        "modes",
        "routes",
    }
)
_BOUND_RUNNER_BOOTSTRAP = (
    "from sigilicon.workflows.design_runner import main;main()"
)


@dataclass(frozen=True)
class DesignMode:
    name: str
    default_args: tuple[str, ...]
    flow: str
    target: str


@dataclass(frozen=True)
class DesignTarget:
    name: str
    owner: str
    description: str
    project_root: Path
    kind: str
    entrypoint: str
    entrypoint_path: Path | None
    spec_argument: str | None
    spec: Path | None
    spec_relative: Path | None
    modes: tuple[DesignMode, ...]
    catalog_member: SourceMember
    source_members: tuple[SourceMember, ...]

    def get_mode(self, name: str) -> DesignMode:
        try:
            return next(mode for mode in self.modes if mode.name == name)
        except StopIteration as exc:
            available = ", ".join(mode.name for mode in self.modes)
            raise ValueError(
                f"design target {self.name!r} does not support mode {name!r}; "
                f"available modes: {available}"
            ) from exc

    def command(self, mode_name: str, extra_args: tuple[str, ...] = ()) -> tuple[str, ...]:
        mode = self.get_mode(mode_name)
        _validate_runner_args(extra_args, f"extra arguments for {self.name}.{mode_name}")
        if self.kind == "script":
            assert self.entrypoint_path is not None
            prefix = (sys.executable, self.entrypoint)
        else:
            prefix = (sys.executable, "-m", self.entrypoint)
        if self.spec_argument is None or self.spec is None:
            routing: tuple[str, ...] = ()
        else:
            assert self.spec_relative is not None
            routing = (self.spec_argument, self.spec_relative.as_posix())
        return (
            *prefix,
            *routing,
            "--mode",
            mode.name,
            *mode.default_args,
            *extra_args,
        )

    def bound_command(
        self,
        mode_name: str,
        *,
        runner_path: str,
        spec_path: str | None,
        extra_args: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        """Execute the snapshotted runner/spec through already-held descriptors."""

        mode = self.get_mode(mode_name)
        _validate_runner_args(extra_args, f"extra arguments for {self.name}.{mode_name}")
        if not runner_path:
            raise ValueError("bound design runner path must be non-empty")
        if self.spec_argument is None:
            if spec_path is not None:
                raise ValueError("spec path provided for a design target without a spec")
            routing: tuple[str, ...] = ()
            bound_spec = ("-", "-")
        else:
            if spec_path is None:
                raise ValueError("bound design target requires its exact spec descriptor")
            assert self.spec is not None
            routing = (self.spec_argument, str(self.spec))
            bound_spec = (spec_path, str(self.spec))
        assert self.entrypoint_path is not None
        package = "-" if self.kind == "script" else self.entrypoint.rpartition(".")[0]
        return (
            sys.executable,
            "-c",
            _BOUND_RUNNER_BOOTSTRAP,
            runner_path,
            str(self.entrypoint_path),
            package,
            *bound_spec,
            *routing,
            "--mode",
            mode.name,
            *mode.default_args,
            *extra_args,
        )


@dataclass(frozen=True)
class DesignTargetCatalog:
    paths: tuple[Path, ...]
    project: Project
    targets: tuple[DesignTarget, ...]
    catalog_members: tuple[SourceMember, ...]
    inventory: tuple[OwnerCatalogSnapshot, ...]

    @property
    def project_root(self) -> Path:
        return self.project.project_root

    def get(self, name: str) -> DesignTarget:
        try:
            return next(target for target in self.targets if target.name == name)
        except StopIteration as exc:
            available = ", ".join(target.name for target in self.targets)
            raise ValueError(
                f"unknown design target {name!r}; available targets: {available}"
            ) from exc

    def source_members_for(self, target: DesignTarget) -> tuple[SourceMember, ...]:
        """Return the exact route definition, runner and spec selected at plan time."""

        if target not in self.targets:
            raise ValueError("design target does not belong to this catalog")
        return (target.catalog_member, *target.source_members)

    def for_owner(self, owner: str) -> DesignTargetCatalog:
        """Narrow a repository inventory to one exact owner binding."""

        targets = tuple(target for target in self.targets if target.owner == owner)
        inventory = tuple(item for item in self.inventory if item.owner == owner)
        catalog_members = tuple(
            member
            for member in self.catalog_members
            if any(member.location == item.path for item in inventory)
        )
        return DesignTargetCatalog(
            tuple(member.location for member in catalog_members),
            self.project,
            targets,
            catalog_members,
            inventory,
        )


def _source_member(
    path: Path,
    *,
    source_root: Path,
    record_text: str | None = None,
) -> SourceMember:
    try:
        source = read_nofollow_text(path) if record_text is None else record_text
        executable = bool(
            path.stat(follow_symlinks=False).st_mode
            & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        )
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise ValueError(f"cannot snapshot design source {path}: {exc}") from exc
    return SourceMember(
        path=path.relative_to(source_root).as_posix(),
        source_root=source_root,
        record_text=source,
        executable=executable,
        location=path,
    )


def _owned_module(
    repository: Project,
    owner: str,
    module: str,
    field: str,
) -> Path:
    module_path = Path(*module.split("."))
    candidates = (
        module_path.with_suffix(".py"),
        module_path / "__main__.py",
    )
    existing = tuple(
        path for path in candidates if (repository.project_root / path).is_file()
    )
    if len(existing) != 1:
        raise ValueError(
            f"{field} must name a Sigilicon CLI or one unambiguous "
            "project-owned module"
        )
    source, _relative = repository.resolve_owner_file(
        owner, existing[0].as_posix(), field
    )
    return source


def _shared_module(module: str) -> tuple[Path, Path]:
    if module not in _SHARED_MODULES:
        raise ValueError(f"unsupported shared design runner module: {module}")
    source_root = Path(__file__).resolve().parents[2]
    source = source_root.joinpath(*module.split(".")).with_suffix(".py")
    if not source.is_file():
        raise ValueError(f"shared design runner source is missing: {module}")
    return source, source_root


def _validate_runner_args(value: tuple[str, ...], field: str) -> None:
    for argument in value:
        if not isinstance(argument, str) or not argument:
            raise ValueError(f"{field} must contain non-empty strings")
        option = argument.split("=", 1)[0]
        if option in _ROUTING_ARGUMENTS:
            raise ValueError(f"{field} cannot override routing argument {option}")


def _modes(
    value: object,
    routes: object,
    field: str,
) -> tuple[DesignMode, ...]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{field} must be a non-empty table")
    if not isinstance(routes, Mapping) or set(routes) != set(value):
        raise ValueError(f"{field.removesuffix('.modes')}.routes must map every mode")
    result: list[DesignMode] = []
    for name, raw_args in value.items():
        if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
            raise ValueError(f"{field} mode names must match {_NAME_RE.pattern!r}")
        if not isinstance(raw_args, (list, tuple)):
            raise ValueError(f"{field}.{name} must be a string array")
        arguments = tuple(raw_args)
        _validate_runner_args(arguments, f"{field}.{name}")
        route = routes[name]
        if (
            not isinstance(route, (list, tuple))
            or len(route) != 2
            or any(
                not isinstance(item, str) or _NAME_RE.fullmatch(item) is None
                for item in route
            )
        ):
            raise ValueError(
                f"{field.removesuffix('.modes')}.routes.{name} must be "
                "[flow, target] identifiers"
            )
        result.append(DesignMode(name, arguments, route[0], route[1]))
    return tuple(result)


def load_design_target_catalog(
    project: Project,
    *,
    catalog_inventory: tuple[OwnerCatalogSnapshot, ...] | None = None,
) -> DesignTargetCatalog:
    """Load every selected design target catalog, or an empty optional domain."""

    repository = project
    root = repository.project_root
    inventory = (
        repository.flow_catalog_inventory()
        if catalog_inventory is None
        else catalog_inventory
    )
    catalogs = repository.flow_catalog_snapshots(
        "design_targets",
        inventory=inventory,
    )
    targets: list[DesignTarget] = []
    catalog_members: list[SourceMember] = []
    names: set[str] = set()
    for catalog in catalogs:
        owner = catalog.owner
        catalog_path = catalog.path
        catalog_member = _source_member(
            catalog_path,
            source_root=root,
            record_text=catalog.record_text,
        )
        catalog_members.append(catalog_member)
        raw = catalog.document
        require_config_header(
            raw,
            catalog_path,
            contract_kind="flow-design-registry",
            path_scope="owner",
            owner=owner,
        )
        unknown = set(raw) - _HEADER_FIELDS - {"targets"}
        if unknown:
            raise ValueError(
                f"design target catalog contains unknown fields: {sorted(unknown)}"
            )
        rows = raw.get("targets")
        if not isinstance(rows, Mapping):
            raise ValueError("design target catalog targets must be a table")
        for name, row in rows.items():
            field = f"targets.{name}"
            if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
                raise ValueError(f"design target name must match {_NAME_RE.pattern!r}")
            if name in names:
                raise ValueError(f"duplicate design target across owner catalogs: {name}")
            names.add(name)
            if not isinstance(row, Mapping):
                raise ValueError(f"{field} must be a table")
            unknown = set(row) - _TARGET_FIELDS
            if unknown:
                raise ValueError(f"{field} contains unknown fields: {sorted(unknown)}")
            description = row.get("description")
            if not isinstance(description, str) or not description.strip():
                raise ValueError(f"{field}.description must be a non-empty string")
            kind = row.get("kind")
            if kind not in _KINDS:
                raise ValueError(f"{field}.kind must be one of {sorted(_KINDS)}")
            entrypoint = row.get("entrypoint")
            entrypoint_path: Path | None = None
            entrypoint_root = root
            if kind == "script":
                entrypoint_path, entrypoint_relative = repository.resolve_owner_file(
                    owner, entrypoint, f"{field}.entrypoint"
                )
                if entrypoint_path.suffix != ".py":
                    raise ValueError(f"{field}.entrypoint must be a Python script")
                entrypoint = entrypoint_relative.as_posix()
            elif not isinstance(entrypoint, str) or _MODULE_RE.fullmatch(entrypoint) is None:
                raise ValueError(f"{field}.entrypoint must name a Python module")
            elif entrypoint in _SHARED_MODULES:
                entrypoint_path, entrypoint_root = _shared_module(entrypoint)
            else:
                entrypoint_path = _owned_module(
                    repository, owner, entrypoint, f"{field}.entrypoint"
                )
            spec_argument = row.get("spec_argument")
            spec_value = row.get("spec")
            spec: Path | None = None
            spec_relative: Path | None = None
            if spec_argument is None and spec_value is None:
                pass
            elif spec_argument not in _SPEC_ARGUMENTS:
                raise ValueError(
                    f"{field}.spec_argument must be one of {sorted(_SPEC_ARGUMENTS)}"
                )
            else:
                spec, spec_relative = repository.resolve_owner_file(
                    owner, spec_value, f"{field}.spec"
                )
            source_members = [
                _source_member(
                    entrypoint_path,
                    source_root=entrypoint_root,
                )
            ]
            if spec is not None:
                source_members.append(_source_member(spec, source_root=root))
            targets.append(
                DesignTarget(
                    name=name,
                    owner=owner,
                    description=description.strip(),
                    project_root=root,
                    kind=kind,
                    entrypoint=entrypoint,
                    entrypoint_path=entrypoint_path,
                    spec_argument=spec_argument,
                    spec=spec,
                    spec_relative=spec_relative,
                    modes=_modes(
                        row.get("modes"),
                        row.get("routes"),
                        f"{field}.modes",
                    ),
                    catalog_member=catalog_member,
                    source_members=tuple(source_members),
                )
            )
    snapshots = (
        *catalog_members,
        *(member for target in targets for member in target.source_members),
    )
    try:
        stable = all(source_member_matches(member) for member in snapshots)
    except (OSError, RuntimeError, UnicodeError):
        stable = False
    if not stable:
        raise ValueError("design source changed during catalog assembly")
    return DesignTargetCatalog(
        tuple(catalog.path for catalog in catalogs),
        repository,
        tuple(targets),
        tuple(catalog_members),
        tuple(inventory),
    )
