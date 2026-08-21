"""Declarative SoC integration contracts and exact IP release locks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import tomllib
from typing import Any, Mapping

from sigilicon.domain.config_contracts import require_config_header
from sigilicon.domain.ip_release import RELEASE_MATURITY_LEVELS, safe_relative


def _table(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a TOML table")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


@dataclass(frozen=True)
class SocIpDependency:
    name: str
    export: str
    required_maturity: str
    logical_interface: str
    physical_interface: str
    role_modules: Mapping[str, str]
    role_exports: Mapping[str, str]


@dataclass(frozen=True)
class SocSourceIp:
    name: str
    component_contract: PurePosixPath
    fileset: str


@dataclass(frozen=True)
class SocFileset:
    name: str
    filelist: PurePosixPath
    ip_roles: Mapping[str, tuple[str, ...]]
    required_capability: str


@dataclass(frozen=True)
class SocPhysicalBinding:
    dependency: str
    transaction_module: str
    physical_shell_module: str
    adapter_module: str
    raw_macro_module: str
    status: str
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class SocVariant:
    name: str
    path: Path
    default_fileset: str
    filesets: Mapping[str, SocFileset]
    physical_binding: SocPhysicalBinding
    architecture_validator: str | None

    def get_fileset(self, name: str | None = None) -> SocFileset:
        selected = name or self.default_fileset
        try:
            return self.filesets[selected]
        except KeyError as exc:
            raise KeyError(
                f"unknown fileset {selected!r} for SoC variant {self.name!r}"
            ) from exc


@dataclass(frozen=True)
class SocContract:
    path: Path
    project_root: Path
    name: str
    lock: PurePosixPath
    ips: tuple[SocIpDependency, ...]
    source_ips: tuple[SocSourceIp, ...]
    implementation_profiles: Mapping[str, PurePosixPath]
    variants: tuple[SocVariant, ...]

    def get_variant(self, name: str) -> SocVariant:
        matches = [variant for variant in self.variants if variant.name == name]
        if len(matches) != 1:
            raise KeyError(f"unknown SoC variant: {name}")
        return matches[0]


@dataclass(frozen=True)
class LockedIpRelease:
    name: str
    release_id: str
    manifest: PurePosixPath
    maturity: str


@dataclass(frozen=True)
class SocLock:
    path: Path
    soc: str
    ips: tuple[LockedIpRelease, ...]


def load_soc_contract(path: Path, *, project_root: Path) -> SocContract:
    root = project_root.resolve()
    contract_path = path.resolve()
    if not contract_path.is_relative_to(root):
        raise ValueError("SoC contract must be inside the project root")
    with contract_path.open("rb") as stream:
        raw: dict[str, Any] = tomllib.load(stream)
    header = require_config_header(
        raw,
        contract_path,
        contract_kind="soc-contract",
        path_scope="product",
    )
    required_roles_raw = raw.get("required_ip_roles", [])
    if not isinstance(required_roles_raw, list) or any(
        not isinstance(role, str) or not role for role in required_roles_raw
    ):
        raise ValueError("required_ip_roles must be a string array")
    required_module_roles = set(required_roles_raw)
    if len(required_module_roles) != len(required_roles_raw):
        raise ValueError("required_ip_roles must be unique")
    dependencies = raw.get("ip")
    if not isinstance(dependencies, list) or not dependencies:
        raise ValueError("SoC contract must declare at least one released IP dependency")
    ips: list[SocIpDependency] = []
    names: set[str] = set()
    for index, value in enumerate(dependencies):
        item = _table(value, f"ip[{index}]")
        name = _string(item.get("name"), f"ip[{index}].name")
        maturity = _string(
            item.get("required_maturity"),
            f"ip[{index}].required_maturity",
        )
        if name in names:
            raise ValueError(f"duplicate SoC IP dependency: {name}")
        if maturity not in RELEASE_MATURITY_LEVELS:
            raise ValueError(f"unsupported SoC IP maturity: {maturity}")
        names.add(name)
        role_modules_raw = _table(
            item.get("role_modules"), f"ip[{index}].role_modules"
        )
        role_modules: dict[str, str] = {}
        for role, module in role_modules_raw.items():
            if not isinstance(role, str) or not role:
                raise ValueError(f"ip[{index}].role_modules keys must be non-empty")
            role_modules[role] = _string(module, f"ip[{index}].role_modules.{role}")
        if not required_module_roles.issubset(role_modules):
            raise ValueError(
                f"ip[{index}].role_modules must declare {sorted(required_module_roles)}"
            )
        export = _string(item.get("export"), f"ip[{index}].export")
        role_exports_raw = item.get("role_exports", {})
        role_exports_table = _table(
            role_exports_raw, f"ip[{index}].role_exports"
        )
        role_exports: dict[str, str] = {
            role: export for role in role_modules
        }
        for role, role_export in role_exports_table.items():
            if not isinstance(role, str) or not role:
                raise ValueError(f"ip[{index}].role_exports keys must be non-empty")
            if role not in role_modules:
                raise ValueError(
                    f"ip[{index}].role_exports names undeclared role: {role}"
                )
            role_exports[role] = _string(
                role_export, f"ip[{index}].role_exports.{role}"
            )
        ips.append(
            SocIpDependency(
                name=name,
                export=export,
                required_maturity=maturity,
                logical_interface=_string(
                    item.get("logical_interface"), f"ip[{index}].logical_interface"
                ),
                physical_interface=_string(
                    item.get("physical_interface"), f"ip[{index}].physical_interface"
                ),
                role_modules=role_modules,
                role_exports=role_exports,
            )
        )

    source_ips_raw = raw.get("source_ip", [])
    if not isinstance(source_ips_raw, list):
        raise ValueError("source_ip must be an array of tables")
    source_ips: list[SocSourceIp] = []
    for index, value in enumerate(source_ips_raw):
        item = _table(value, f"source_ip[{index}]")
        source_ips.append(
            SocSourceIp(
                name=_string(item.get("name"), f"source_ip[{index}].name"),
                component_contract=safe_relative(
                    item.get("component"), f"source_ip[{index}].component"
                ),
                fileset=_string(item.get("fileset"), f"source_ip[{index}].fileset"),
            )
        )

    implementation_raw = _table(raw.get("implementation", {}), "implementation")
    implementation_profiles: dict[str, PurePosixPath] = {}
    for name, relative_value in implementation_raw.items():
        if not isinstance(name, str) or not name:
            raise ValueError("implementation profile names must be non-empty")
        relative = safe_relative(relative_value, f"implementation.{name}")
        profile_path = (contract_path.parent / Path(relative)).resolve()
        if (
            not profile_path.is_relative_to(contract_path.parent)
            or not profile_path.is_file()
        ):
            raise FileNotFoundError(
                f"SoC implementation profile is missing or outside the product: {relative}"
            )
        with profile_path.open("rb") as stream:
            profile_raw: dict[str, Any] = tomllib.load(stream)
        profile_kind = _string(
            profile_raw.get("contract_kind"),
            f"implementation.{name}.contract_kind",
        )
        require_config_header(
            profile_raw,
            profile_path,
            contract_kind=profile_kind,
            path_scope="product",
            owner=header.owner,
        )
        implementation_profiles[name] = PurePosixPath(
            profile_path.relative_to(root).as_posix()
        )

    variants_table = _table(raw.get("variants"), "variants")
    variants: list[SocVariant] = []
    for name, relative_value in variants_table.items():
        if not isinstance(name, str) or not name:
            raise ValueError("SoC variant names must be non-empty strings")
        relative = safe_relative(relative_value, f"variants.{name}")
        variant_path = (contract_path.parent / Path(relative)).resolve()
        if not variant_path.is_relative_to(root) or not variant_path.is_file():
            raise FileNotFoundError(f"SoC variant contract is missing: {relative}")
        with variant_path.open("rb") as stream:
            variant_raw: dict[str, Any] = tomllib.load(stream)
        require_config_header(
            variant_raw,
            variant_path,
            contract_kind="soc-variant",
            path_scope="variant",
            owner=header.owner,
        )
        integration = _table(variant_raw.get("integration"), f"variant {name}.integration")
        if integration.get("variant") != name:
            raise ValueError(f"SoC variant identity mismatch: {name}")
        filesets_raw = _table(
            variant_raw.get("filesets"), f"variant {name}.filesets"
        )
        filesets: dict[str, SocFileset] = {}
        for fileset_name, fileset_value in filesets_raw.items():
            if not isinstance(fileset_name, str) or not fileset_name:
                raise ValueError(f"variant {name} fileset names must be non-empty")
            fileset = _table(
                fileset_value, f"variant {name}.filesets.{fileset_name}"
            )
            roles_raw = _table(
                fileset.get("ip_roles"),
                f"variant {name}.filesets.{fileset_name}.ip_roles",
            )
            roles: dict[str, tuple[str, ...]] = {}
            for dependency_name, values in roles_raw.items():
                if dependency_name not in names:
                    raise ValueError(
                        f"variant {name} names undeclared released IP {dependency_name}"
                    )
                if not isinstance(values, list) or not values or any(
                    not isinstance(role, str) or not role for role in values
                ):
                    raise ValueError(
                        f"variant {name} fileset {fileset_name} roles for "
                        f"{dependency_name} must be strings"
                    )
                roles[dependency_name] = tuple(values)
            if set(roles) != names:
                raise ValueError(
                    f"variant {name} fileset {fileset_name} must declare every IP"
                )
            capability = _string(
                fileset.get("required_capability"),
                f"variant {name}.filesets.{fileset_name}.required_capability",
            )
            if capability not in {
                "simulation",
                "synthesis",
                "physical_implementation",
            }:
                raise ValueError(
                    f"variant {name} fileset {fileset_name} requires an unsupported capability"
                )
            filesets[fileset_name] = SocFileset(
                name=fileset_name,
                filelist=safe_relative(
                    fileset.get("filelist"),
                    f"variant {name}.filesets.{fileset_name}.filelist",
                ),
                ip_roles=roles,
                required_capability=capability,
            )
        default_fileset = _string(
            integration.get("default_fileset"),
            f"variant {name}.integration.default_fileset",
        )
        if default_fileset not in filesets:
            raise ValueError(f"variant {name} default_fileset is undeclared")
        validation_value = variant_raw.get("architecture_validation")
        architecture_validator = None
        if validation_value is not None:
            validation = _table(
                validation_value, f"variant {name}.architecture_validation"
            )
            architecture_validator = _string(
                validation.get("validator"),
                f"variant {name}.architecture_validation.validator",
            )
            module_name, separator, function_name = architecture_validator.partition(
                ":"
            )
            if (
                separator != ":"
                or not module_name
                or not function_name
                or ":" in function_name
            ):
                raise ValueError(
                    f"variant {name} architecture validator must be module:function"
                )
        binding_raw = _table(
            variant_raw.get("physical_binding"), f"variant {name}.physical_binding"
        )
        dependency_name = _string(
            binding_raw.get("dependency"),
            f"variant {name}.physical_binding.dependency",
        )
        dependency = next((item for item in ips if item.name == dependency_name), None)
        if dependency is None:
            raise ValueError(f"variant {name} physical binding names undeclared IP")
        status = _string(
            binding_raw.get("status"), f"variant {name}.physical_binding.status"
        )
        if status not in {"blocked", "ready"}:
            raise ValueError(f"variant {name} physical binding status is unsupported")
        blockers_raw = binding_raw.get("blockers", [])
        if not isinstance(blockers_raw, list) or any(
            not isinstance(item, str) or not item for item in blockers_raw
        ):
            raise ValueError(f"variant {name} physical binding blockers must be strings")
        if (status == "blocked") != bool(blockers_raw):
            raise ValueError(
                f"variant {name} blocked physical binding must have blockers and "
                "ready binding must not"
            )
        physical_binding = SocPhysicalBinding(
            dependency=dependency_name,
            transaction_module=_string(
                binding_raw.get("transaction_module"),
                f"variant {name}.physical_binding.transaction_module",
            ),
            physical_shell_module=_string(
                binding_raw.get("physical_shell_module"),
                f"variant {name}.physical_binding.physical_shell_module",
            ),
            adapter_module=_string(
                binding_raw.get("adapter_module"),
                f"variant {name}.physical_binding.adapter_module",
            ),
            raw_macro_module=_string(
                binding_raw.get("raw_macro_module"),
                f"variant {name}.physical_binding.raw_macro_module",
            ),
            status=status,
            blockers=tuple(blockers_raw),
        )
        expected_binding_modules = {
            "transaction_model": physical_binding.transaction_module,
            "integration_adapter": physical_binding.physical_shell_module,
            "physical_blackbox": physical_binding.raw_macro_module,
        }
        if any(
            dependency.role_modules.get(role) != module
            for role, module in expected_binding_modules.items()
        ):
            raise ValueError(
                f"variant {name} physical binding disagrees with SoC IP modules"
            )
        variants.append(
            SocVariant(
                name=name,
                path=variant_path,
                default_fileset=default_fileset,
                filesets=filesets,
                physical_binding=physical_binding,
                architecture_validator=architecture_validator,
            )
        )
    if not variants:
        raise ValueError("SoC contract must declare at least one variant")
    return SocContract(
        path=contract_path,
        project_root=root,
        name=_string(raw.get("name"), "name"),
        lock=safe_relative(raw.get("lock"), "lock"),
        ips=tuple(ips),
        source_ips=tuple(source_ips),
        implementation_profiles=implementation_profiles,
        variants=tuple(variants),
    )


def load_soc_lock(path: Path, *, contract: SocContract) -> SocLock:
    lock_path = path.resolve()
    if not lock_path.is_file():
        raise FileNotFoundError(f"SoC IP lock is missing: {lock_path}")
    with lock_path.open("rb") as stream:
        raw: dict[str, Any] = tomllib.load(stream)
    require_config_header(
        raw,
        lock_path,
        contract_kind="soc-release-lock",
        path_scope="product",
        owner="soc",
    )
    if raw.get("soc") != contract.name:
        raise ValueError("SoC IP lock identity does not match its contract")
    entries = raw.get("ip")
    if not isinstance(entries, list):
        raise ValueError("SoC IP lock must declare ip entries")
    locked: list[LockedIpRelease] = []
    for index, value in enumerate(entries):
        item = _table(value, f"lock.ip[{index}]")
        maturity = _string(
            item.get("maturity"), f"lock.ip[{index}].maturity"
        )
        if maturity not in RELEASE_MATURITY_LEVELS:
            raise ValueError("SoC IP lock has an unsupported maturity")
        locked.append(
            LockedIpRelease(
                name=_string(item.get("name"), f"lock.ip[{index}].name"),
                release_id=_string(item.get("release_id"), f"lock.ip[{index}].release_id"),
                manifest=safe_relative(item.get("manifest"), f"lock.ip[{index}].manifest"),
                maturity=maturity,
            )
        )
    expected = {item.name for item in contract.ips}
    actual = {item.name for item in locked}
    if len(actual) != len(locked) or actual != expected:
        raise ValueError("SoC IP lock entries do not exactly match its dependencies")
    return SocLock(path=lock_path, soc=contract.name, ips=tuple(locked))
