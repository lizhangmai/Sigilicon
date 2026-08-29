"""Composite-IP integration intent and exact dependency release selection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import tomllib
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

from sigilicon.domain.component import (
    ComponentContract,
    load_component_contract,
    load_component_graph,
)
from sigilicon.domain.config_contracts import require_config_header
from sigilicon.domain.ip_release import RELEASE_MATURITY_LEVELS, safe_relative

if TYPE_CHECKING:
    from sigilicon.domain.repository import Project


_CAPABILITIES = frozenset({"simulation", "synthesis", "physical_implementation"})


def _table(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a TOML table")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _project_relative(
    value: object,
    label: str,
    *,
    project_root: Path,
) -> tuple[PurePosixPath, Path]:
    relative = safe_relative(value, label)
    resolved = (project_root / Path(relative)).resolve()
    if not resolved.is_relative_to(project_root):
        raise ValueError(f"{label} escapes the project root")
    return PurePosixPath(resolved.relative_to(project_root).as_posix()), resolved


@dataclass(frozen=True)
class IpReleaseDependency:
    export: str
    required_maturity: str
    logical_interface: str
    physical_interface: str
    roles: tuple[str, ...]
    role_modules: Mapping[str, str]
    role_exports: Mapping[str, str]


@dataclass(frozen=True)
class IpIntegrationDependency:
    name: str
    component_contract: PurePosixPath
    release: IpReleaseDependency | None


@dataclass(frozen=True)
class IpIntegrationFileset:
    name: str
    filelist: PurePosixPath
    dependency_roles: Mapping[str, tuple[str, ...]]
    source_filesets: Mapping[str, str]
    required_capability: str


@dataclass(frozen=True)
class IpPhysicalBinding:
    dependency: str
    transaction_module: str
    physical_shell_module: str
    adapter_module: str
    raw_macro_module: str
    status: str
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class IpOperatingVariant:
    name: str
    path: Path
    default_fileset: str
    filesets: Mapping[str, IpIntegrationFileset]
    physical_binding: IpPhysicalBinding | None
    architecture_validator: str | None

    def get_fileset(self, name: str | None = None) -> IpIntegrationFileset:
        selected = name or self.default_fileset
        try:
            return self.filesets[selected]
        except KeyError as exc:
            raise KeyError(
                f"unknown fileset {selected!r} for IP variant {self.name!r}"
            ) from exc


@dataclass(frozen=True)
class IpIntegrationContract:
    project: Project
    component: ComponentContract
    component_graph: Mapping[str, ComponentContract]
    dependency_lock: PurePosixPath | None
    dependencies: tuple[IpIntegrationDependency, ...]
    implementation_profiles: Mapping[str, PurePosixPath]
    variants: tuple[IpOperatingVariant, ...]

    @property
    def path(self) -> Path:
        return self.component.path

    @property
    def project_root(self) -> Path:
        return self.project.project_root

    @property
    def owner(self) -> str:
        return self.component.owner

    @property
    def name(self) -> str:
        return self.component.name

    @property
    def release_dependencies(self) -> tuple[IpIntegrationDependency, ...]:
        return tuple(item for item in self.dependencies if item.release is not None)

    def get_variant(self, name: str) -> IpOperatingVariant:
        matches = [variant for variant in self.variants if variant.name == name]
        if len(matches) != 1:
            raise KeyError(f"unknown IP operating variant: {name}")
        return matches[0]


@dataclass(frozen=True)
class LockedIpRelease:
    name: str
    release_id: str
    manifest: PurePosixPath
    maturity: str


@dataclass(frozen=True)
class IpDependencyLock:
    path: Path
    ip: str
    dependencies: tuple[LockedIpRelease, ...]


def _release_dependency(value: object, label: str) -> IpReleaseDependency:
    item = _table(value, label)
    maturity = _string(item.get("required_maturity"), f"{label}.required_maturity")
    if maturity not in RELEASE_MATURITY_LEVELS:
        raise ValueError(f"{label}.required_maturity is unsupported: {maturity}")
    roles_raw = item.get("roles")
    if (
        not isinstance(roles_raw, (list, tuple))
        or not roles_raw
        or any(not isinstance(role, str) or not role for role in roles_raw)
        or len(set(roles_raw)) != len(roles_raw)
    ):
        raise ValueError(f"{label}.roles must be unique non-empty strings")
    roles = tuple(roles_raw)
    modules_raw = _table(item.get("role_modules", {}), f"{label}.role_modules")
    role_modules = {
        _string(role, f"{label}.role_modules key"): _string(
            module, f"{label}.role_modules.{role}"
        )
        for role, module in modules_raw.items()
    }
    unknown_module_roles = set(role_modules) - set(roles)
    if unknown_module_roles:
        raise ValueError(
            f"{label}.role_modules names undeclared roles: {sorted(unknown_module_roles)}"
        )
    export = _string(item.get("export"), f"{label}.export")
    exports_raw = _table(item.get("role_exports", {}), f"{label}.role_exports")
    role_exports = {role: export for role in roles}
    for role, role_export in exports_raw.items():
        role = _string(role, f"{label}.role_exports key")
        if role not in roles:
            raise ValueError(f"{label}.role_exports names undeclared role: {role}")
        role_exports[role] = _string(role_export, f"{label}.role_exports.{role}")
    return IpReleaseDependency(
        export=export,
        required_maturity=maturity,
        logical_interface=_string(
            item.get("logical_interface"), f"{label}.logical_interface"
        ),
        physical_interface=_string(
            item.get("physical_interface"), f"{label}.physical_interface"
        ),
        roles=roles,
        role_modules=role_modules,
        role_exports=role_exports,
    )


def _implementation_profiles(
    raw: Mapping[str, Any],
    *,
    project_root: Path,
    owner: str,
) -> Mapping[str, PurePosixPath]:
    values = _table(raw.get("implementation", {}), "implementation")
    result: dict[str, PurePosixPath] = {}
    for name, value in values.items():
        name = _string(name, "implementation profile name")
        relative, path = _project_relative(
            value,
            f"implementation.{name}",
            project_root=project_root,
        )
        if not path.is_file():
            raise FileNotFoundError(f"IP implementation profile is missing: {relative}")
        with path.open("rb") as stream:
            profile: dict[str, Any] = tomllib.load(stream)
        require_config_header(
            profile,
            path,
            contract_kind=_string(
                profile.get("contract_kind"),
                f"implementation.{name}.contract_kind",
            ),
            path_scope="owner",
            owner=owner,
        )
        result[name] = relative
    return result


def _operating_variant(
    name: str,
    path: Path,
    *,
    contract: ComponentContract,
    graph: Mapping[str, ComponentContract],
    dependencies: Mapping[str, IpIntegrationDependency],
) -> IpOperatingVariant:
    with path.open("rb") as stream:
        raw: dict[str, Any] = tomllib.load(stream)
    require_config_header(
        raw,
        path,
        contract_kind="ip-operating-variant",
        path_scope="variant",
        owner=contract.owner,
    )
    integration = _table(raw.get("integration"), f"variant {name}.integration")
    if integration.get("variant") != name:
        raise ValueError(f"IP operating variant identity mismatch: {name}")
    expected_component = contract.path.relative_to(contract.project_root).as_posix()
    declared_component = integration.get("component_contract")
    if declared_component is not None and declared_component != expected_component:
        raise ValueError(f"variant {name} component contract identity mismatch")

    filesets_raw = _table(raw.get("filesets"), f"variant {name}.filesets")
    filesets: dict[str, IpIntegrationFileset] = {}
    for fileset_name, value in filesets_raw.items():
        fileset_name = _string(fileset_name, f"variant {name} fileset name")
        fileset = _table(value, f"variant {name}.filesets.{fileset_name}")
        roles_raw = _table(
            fileset.get("dependency_roles", {}),
            f"variant {name}.filesets.{fileset_name}.dependency_roles",
        )
        dependency_roles: dict[str, tuple[str, ...]] = {}
        for dependency_name, roles_value in roles_raw.items():
            if dependency_name not in dependencies:
                raise ValueError(
                    f"variant {name} names undeclared dependency {dependency_name}"
                )
            release = dependencies[dependency_name].release
            if release is None:
                raise ValueError(
                    f"variant {name} requests release roles from source-only "
                    f"dependency {dependency_name}"
                )
            if (
                not isinstance(roles_value, list)
                or not roles_value
                or any(not isinstance(role, str) or not role for role in roles_value)
                or len(set(roles_value)) != len(roles_value)
            ):
                raise ValueError(
                    f"variant {name} dependency roles for {dependency_name} "
                    "must be unique strings"
                )
            unknown_roles = set(roles_value) - set(release.roles)
            if unknown_roles:
                raise ValueError(
                    f"variant {name} requests undeclared dependency roles: "
                    f"{sorted(unknown_roles)}"
                )
            dependency_roles[dependency_name] = tuple(roles_value)

        source_raw = _table(
            fileset.get("source_filesets", {}),
            f"variant {name}.filesets.{fileset_name}.source_filesets",
        )
        source_filesets: dict[str, str] = {}
        for dependency_name, source_fileset in source_raw.items():
            if dependency_name not in dependencies:
                raise ValueError(
                    f"variant {name} names undeclared dependency {dependency_name}"
                )
            source_fileset = _string(
                source_fileset,
                f"variant {name}.filesets.{fileset_name}.source_filesets."
                f"{dependency_name}",
            )
            if source_fileset not in graph[dependency_name].filesets:
                raise ValueError(
                    f"dependency {dependency_name} has no source fileset "
                    f"{source_fileset!r}"
                )
            source_filesets[dependency_name] = source_fileset

        capability = _string(
            fileset.get("required_capability"),
            f"variant {name}.filesets.{fileset_name}.required_capability",
        )
        if capability not in _CAPABILITIES:
            raise ValueError(
                f"variant {name} fileset {fileset_name} requires an unsupported "
                "capability"
            )
        filesets[fileset_name] = IpIntegrationFileset(
            name=fileset_name,
            filelist=safe_relative(
                fileset.get("filelist"),
                f"variant {name}.filesets.{fileset_name}.filelist",
            ),
            dependency_roles=dependency_roles,
            source_filesets=source_filesets,
            required_capability=capability,
        )

    default_fileset = _string(
        integration.get("default_fileset"),
        f"variant {name}.integration.default_fileset",
    )
    if default_fileset not in filesets:
        raise ValueError(f"variant {name} default_fileset is undeclared")

    validator: str | None = None
    if raw.get("architecture_validation") is not None:
        validation = _table(
            raw["architecture_validation"],
            f"variant {name}.architecture_validation",
        )
        validator = _string(
            validation.get("validator"),
            f"variant {name}.architecture_validation.validator",
        )
        module_name, separator, function_name = validator.partition(":")
        if separator != ":" or not module_name or not function_name or ":" in function_name:
            raise ValueError(
                f"variant {name} architecture validator must be module:function"
            )

    physical_binding: IpPhysicalBinding | None = None
    if raw.get("physical_binding") is not None:
        binding = _table(raw["physical_binding"], f"variant {name}.physical_binding")
        dependency_name = _string(
            binding.get("dependency"),
            f"variant {name}.physical_binding.dependency",
        )
        dependency = dependencies.get(dependency_name)
        if dependency is None or dependency.release is None:
            raise ValueError(
                f"variant {name} physical binding names no released dependency"
            )
        status = _string(
            binding.get("status"), f"variant {name}.physical_binding.status"
        )
        if status not in {"blocked", "ready"}:
            raise ValueError(f"variant {name} physical binding status is unsupported")
        blockers_raw = binding.get("blockers", [])
        if not isinstance(blockers_raw, list) or any(
            not isinstance(item, str) or not item for item in blockers_raw
        ):
            raise ValueError(f"variant {name} physical binding blockers must be strings")
        if (status == "blocked") != bool(blockers_raw):
            raise ValueError(
                f"variant {name} blocked physical binding must have blockers and "
                "ready binding must not"
            )
        physical_binding = IpPhysicalBinding(
            dependency=dependency_name,
            transaction_module=_string(
                binding.get("transaction_module"),
                f"variant {name}.physical_binding.transaction_module",
            ),
            physical_shell_module=_string(
                binding.get("physical_shell_module"),
                f"variant {name}.physical_binding.physical_shell_module",
            ),
            adapter_module=_string(
                binding.get("adapter_module"),
                f"variant {name}.physical_binding.adapter_module",
            ),
            raw_macro_module=_string(
                binding.get("raw_macro_module"),
                f"variant {name}.physical_binding.raw_macro_module",
            ),
            status=status,
            blockers=tuple(blockers_raw),
        )
        expected_modules = {
            "transaction_model": physical_binding.transaction_module,
            "integration_adapter": physical_binding.physical_shell_module,
            "physical_blackbox": physical_binding.raw_macro_module,
        }
        assert dependency.release is not None
        if any(
            dependency.release.role_modules.get(role) != module
            for role, module in expected_modules.items()
        ):
            raise ValueError(
                f"variant {name} physical binding disagrees with dependency modules"
            )

    return IpOperatingVariant(
        name=name,
        path=path,
        default_fileset=default_fileset,
        filesets=filesets,
        physical_binding=physical_binding,
        architecture_validator=validator,
    )


def load_ip_integration_contract(
    path: Path,
    *,
    project: Project | None = None,
    project_root: Path | None = None,
) -> IpIntegrationContract:
    """Load composite-IP integration intent from its canonical component manifest."""

    from sigilicon.domain.repository import Project

    repository = Project.bind(project=project, project_root=project_root)
    root = repository.project_root
    contract_path = path.resolve()
    cataloged_owner = repository.require_owner(contract_path)
    component = (
        cataloged_owner.component
        if cataloged_owner.component.path == contract_path
        else load_component_contract(contract_path, project_root=root)
    )
    if component.owner != cataloged_owner.name:
        raise ValueError("IP integration owner disagrees with the project catalog")
    if component.kind != "composite-ip":
        raise ValueError("IP integration requires a composite-ip component")
    if component.document:
        raw = component.document
    else:
        with component.path.open("rb") as stream:
            raw = tomllib.load(stream)
    graph = load_component_graph(
        component.path,
        project_root=root,
        root_contract=component,
    )

    dependency_rows = raw.get("component", [])
    assert isinstance(dependency_rows, (list, tuple))
    dependencies: list[IpIntegrationDependency] = []
    for index, (base, value) in enumerate(
        zip(component.components, dependency_rows, strict=True)
    ):
        row = _table(value, f"component[{index}]")
        release_value = row.get("release")
        dependencies.append(
            IpIntegrationDependency(
                name=base.name,
                component_contract=base.contract,
                release=(
                    None
                    if release_value is None
                    else _release_dependency(
                        release_value, f"component[{index}].release"
                    )
                ),
            )
        )
    by_name = {item.name: item for item in dependencies}

    lock_value = raw.get("dependency_lock")
    dependency_lock: PurePosixPath | None = None
    if lock_value is not None:
        dependency_lock, _ = _project_relative(
            lock_value,
            "dependency_lock",
            project_root=root,
        )

    variants_raw = _table(raw.get("variants"), "variants")
    variants: list[IpOperatingVariant] = []
    for name, value in variants_raw.items():
        name = _string(name, "variant name")
        _, variant_path = _project_relative(
            value,
            f"variants.{name}",
            project_root=root,
        )
        if not variant_path.is_file():
            raise FileNotFoundError(f"IP operating variant is missing: {variant_path}")
        variants.append(
            _operating_variant(
                name,
                variant_path,
                contract=component,
                graph=graph,
                dependencies=by_name,
            )
        )
    if not variants:
        raise ValueError("IP integration must declare at least one operating variant")

    return IpIntegrationContract(
        project=repository,
        component=component,
        component_graph=MappingProxyType(dict(graph)),
        dependency_lock=dependency_lock,
        dependencies=tuple(dependencies),
        implementation_profiles=_implementation_profiles(
            raw,
            project_root=root,
            owner=component.owner,
        ),
        variants=tuple(variants),
    )


def load_ip_dependency_lock(
    path: Path, *, contract: IpIntegrationContract
) -> IpDependencyLock:
    lock_path = path.resolve()
    if not lock_path.is_relative_to(contract.project_root):
        raise ValueError("IP dependency lock must be inside the project root")
    if not lock_path.is_file():
        raise FileNotFoundError(f"IP dependency lock is missing: {lock_path}")
    with lock_path.open("rb") as stream:
        raw: dict[str, Any] = tomllib.load(stream)
    require_config_header(
        raw,
        lock_path,
        contract_kind="ip-dependency-lock",
        path_scope="owner",
        owner=contract.owner,
    )
    if raw.get("ip") != contract.name:
        raise ValueError("IP dependency lock identity does not match its component")
    entries = raw.get("dependency")
    if not isinstance(entries, list):
        raise ValueError("IP dependency lock must declare dependency entries")
    locked: list[LockedIpRelease] = []
    for index, value in enumerate(entries):
        item = _table(value, f"lock.dependency[{index}]")
        maturity = _string(
            item.get("maturity"), f"lock.dependency[{index}].maturity"
        )
        if maturity not in RELEASE_MATURITY_LEVELS:
            raise ValueError("IP dependency lock has an unsupported maturity")
        locked.append(
            LockedIpRelease(
                name=_string(item.get("name"), f"lock.dependency[{index}].name"),
                release_id=_string(
                    item.get("release_id"),
                    f"lock.dependency[{index}].release_id",
                ),
                manifest=safe_relative(
                    item.get("manifest"),
                    f"lock.dependency[{index}].manifest",
                ),
                maturity=maturity,
            )
        )
    expected = {item.name for item in contract.release_dependencies}
    actual = {item.name for item in locked}
    if len(actual) != len(locked) or actual != expected:
        raise ValueError(
            "IP dependency lock entries do not exactly match release dependencies"
        )
    return IpDependencyLock(
        path=lock_path,
        ip=contract.name,
        dependencies=tuple(locked),
    )
