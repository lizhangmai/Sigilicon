"""Project-owned target catalog for design runner entrypoints."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
import tomllib

from sigilicon.domain.config_contracts import require_config_header
from sigilicon.domain.repository import Project

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_MODULE_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z"
)
_PACKAGE_MODULE_PREFIXES = ("sigilicon.cli.",)
_KINDS = frozenset({"script", "module"})
_SPEC_ARGUMENTS = frozenset({"--spec", "--design"})
_ROUTING_ARGUMENTS = frozenset({"--spec", "--design", "--mode"})
_HEADER_FIELDS = frozenset({"schema", "contract_kind", "path_scope", "owner"})
_TARGET_FIELDS = frozenset(
    {"description", "kind", "entrypoint", "spec_argument", "spec", "modes"}
)


@dataclass(frozen=True)
class DesignMode:
    name: str
    default_args: tuple[str, ...]


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


@dataclass(frozen=True)
class DesignTargetCatalog:
    paths: tuple[Path, ...]
    project: Project
    targets: tuple[DesignTarget, ...]

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


def execute_design_target(
    target: DesignTarget,
    mode: str,
    extra_args: tuple[str, ...] = (),
    *,
    process_executor: Callable[[str, list[str]], object] = os.execv,
) -> int:
    """Replace the dispatcher with the existing project-owned runner."""

    command = target.command(mode, extra_args)
    previous_directory = Path.cwd()
    try:
        os.chdir(target.project_root)
        process_executor(command[0], list(command))
    except OSError as exc:
        raise RuntimeError(
            f"cannot execute design runner {target.entrypoint}: {exc}"
        ) from exc
    finally:
        os.chdir(previous_directory)
    return 0


def _relative_file(root: Path, value: object, field: str) -> tuple[Path, Path]:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty relative path")
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError(f"{field} must stay below the project root")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"{field} must stay below the project root")
    if not resolved.is_file():
        raise ValueError(f"{field} does not exist: {resolved}")
    return resolved, relative


def _validate_runner_args(value: tuple[str, ...], field: str) -> None:
    for argument in value:
        if not isinstance(argument, str) or not argument:
            raise ValueError(f"{field} must contain non-empty strings")
        option = argument.split("=", 1)[0]
        if option in _ROUTING_ARGUMENTS:
            raise ValueError(f"{field} cannot override routing argument {option}")


def _modes(value: object, field: str) -> tuple[DesignMode, ...]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{field} must be a non-empty table")
    result: list[DesignMode] = []
    for name, raw_args in value.items():
        if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
            raise ValueError(f"{field} mode names must match {_NAME_RE.pattern!r}")
        if not isinstance(raw_args, list):
            raise ValueError(f"{field}.{name} must be a string array")
        arguments = tuple(raw_args)
        _validate_runner_args(arguments, f"{field}.{name}")
        result.append(DesignMode(name, arguments))
    return tuple(result)


def load_design_target_catalog(
    project_root: Path | None = None,
    *,
    project: Project | None = None,
) -> DesignTargetCatalog:
    """Load every selected design target catalog, or an empty optional domain."""

    repository = Project.bind(project=project, project_root=project_root)
    root = repository.project_root
    catalogs = repository.flow_catalogs("design_targets")
    targets: list[DesignTarget] = []
    names: set[str] = set()
    for owner, catalog_path in catalogs:
        try:
            with catalog_path.open("rb") as stream:
                raw = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(
                f"cannot read design target catalog {catalog_path}: {exc}"
            ) from exc
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
        if not isinstance(rows, dict):
            raise ValueError("design target catalog targets must be a table")
        for name, row in rows.items():
            field = f"targets.{name}"
            if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
                raise ValueError(f"design target name must match {_NAME_RE.pattern!r}")
            if name in names:
                raise ValueError(f"duplicate design target across owner catalogs: {name}")
            names.add(name)
            if not isinstance(row, dict):
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
            if kind == "script":
                entrypoint_path, entrypoint_relative = _relative_file(
                    root, entrypoint, f"{field}.entrypoint"
                )
                if entrypoint_path.suffix != ".py":
                    raise ValueError(f"{field}.entrypoint must be a Python script")
                entrypoint = entrypoint_relative.as_posix()
            elif not isinstance(entrypoint, str) or _MODULE_RE.fullmatch(entrypoint) is None:
                raise ValueError(f"{field}.entrypoint must name a Python module")
            elif not entrypoint.startswith(_PACKAGE_MODULE_PREFIXES):
                module_path = root.joinpath(*entrypoint.split("."))
                if not (
                    module_path.with_suffix(".py").is_file()
                    or (module_path / "__main__.py").is_file()
                ):
                    raise ValueError(
                        f"{field}.entrypoint must name a Sigilicon CLI or a "
                        "project-owned module"
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
                spec, spec_relative = _relative_file(root, spec_value, f"{field}.spec")
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
                    modes=_modes(row.get("modes"), f"{field}.modes"),
                )
            )
    return DesignTargetCatalog(
        tuple(path for _, path in catalogs),
        repository,
        tuple(targets),
    )
