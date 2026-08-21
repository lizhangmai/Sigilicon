"""Source planning and immutable release resolution for SoC products."""

from __future__ import annotations

import importlib
from pathlib import Path
import tomllib
from typing import Any, Mapping

from sigilicon.domain.config_contracts import require_config_header
from sigilicon.domain.component import load_component_contract
from sigilicon.domain.ip_release import (
    RELEASE_MATURITY_LEVELS,
    load_ip_contract,
)
from sigilicon.domain.soc import (
    LockedIpRelease,
    SocContract,
    SocIpDependency,
    SocVariant,
    load_soc_contract,
    load_soc_lock,
)
from sigilicon.domain.repository import RepositoryContext
from sigilicon.workflows.ip_packaging import (
    audit_ip_release_manifest,
    plan_ip_release,
    release_role_view,
    resolve_release_role,
)


def _catalog_targets(root: Path, domain: str) -> Mapping[str, Any]:
    if domain not in {"ip", "soc"}:
        raise ValueError(f"unsupported product catalog: {domain}")
    context = RepositoryContext.from_project_root(root)
    path = context.catalog(domain)
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    require_config_header(
        raw,
        path,
        contract_kind={"ip": "ip-catalog", "soc": "soc-catalog"}[domain],
        path_scope="repository",
        owner="repository",
    )
    if not isinstance(raw.get("targets"), Mapping):
        raise ValueError(f"unsupported {domain} catalog")
    return raw["targets"]


def catalog_contract_path(root: Path, domain: str, target: str) -> Path:
    """Resolve one target through a validated product catalog."""

    targets = _catalog_targets(root, domain)
    entry = targets.get(target)
    if not isinstance(entry, Mapping) or not isinstance(entry.get("contract"), str):
        raise KeyError(f"unknown {domain} target: {target}")
    path = (root / entry["contract"]).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise FileNotFoundError(f"{domain} target contract is missing: {path}")
    return path


def _ip_catalog(root: Path) -> Mapping[str, Any]:
    return _catalog_targets(root, "ip")


def _producer_contract(contract: SocContract, dependency_name: str) -> Path:
    entry = _ip_catalog(contract.project_root).get(dependency_name)
    if not isinstance(entry, Mapping) or not isinstance(entry.get("contract"), str):
        raise KeyError(f"released IP is absent from the IP catalog: {dependency_name}")
    path = (contract.project_root / entry["contract"]).resolve()
    if not path.is_relative_to(contract.project_root) or not path.is_file():
        raise FileNotFoundError(f"released IP contract is missing: {path}")
    producer = load_ip_contract(path, project_root=contract.project_root)
    if producer.name != dependency_name:
        raise ValueError(f"IP catalog identity mismatch: {dependency_name}")
    return path


def _role_export(dependency: SocIpDependency, role: str) -> str:
    """Return the release export selected for one SoC collateral role."""

    return dependency.role_exports.get(role, dependency.export)


def _allowed_source_files(contract: SocContract) -> set[Path]:
    allowed: set[Path] = set()
    for dependency in contract.source_ips:
        component_path = (contract.project_root / dependency.component_contract).resolve()
        component = load_component_contract(component_path, project_root=contract.project_root)
        if component.name != dependency.name:
            raise ValueError(f"source IP identity mismatch: {dependency.name}")
        try:
            files = component.filesets[dependency.fileset]
        except KeyError as exc:
            raise ValueError(
                f"source IP {dependency.name} has no {dependency.fileset!r} fileset"
            ) from exc
        allowed.update((contract.project_root / Path(path)).resolve() for path in files)
    return allowed


def _filelist(path: Path, contract: SocContract) -> tuple[Path, ...]:
    root = contract.project_root
    if not path.is_file() or not path.is_relative_to(root):
        raise FileNotFoundError(f"SoC RTL filelist is missing or unsafe: {path}")
    owned_root = (contract.path.parent / "rtl").resolve()
    source_ip_files = _allowed_source_files(contract)
    sources: list[Path] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        relative = Path(line)
        source = (root / relative).resolve()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not source.is_file()
            or (not source.is_relative_to(owned_root) and source not in source_ip_files)
        ):
            raise RuntimeError(
                f"SoC filelist may contain only product RTL or declared source IP: {line}"
            )
        sources.append(source)
    if not sources:
        raise RuntimeError(f"SoC RTL filelist is empty: {path}")
    return tuple(sources)


def _fileset_source_plan(
    contract: SocContract, variant: SocVariant, fileset_name: str
) -> dict[str, Any]:
    root = contract.project_root
    fileset = variant.get_fileset(fileset_name)
    filelist_path = (root / fileset.filelist).resolve()
    sources = _filelist(filelist_path, contract)
    return {
        "name": fileset.name,
        "filelist": filelist_path.relative_to(root).as_posix(),
        "sources": [path.relative_to(root).as_posix() for path in sources],
        "ip_roles": {name: list(roles) for name, roles in fileset.ip_roles.items()},
        "required_capability": fileset.required_capability,
    }


def _variant_source_plan(contract: SocContract, variant: SocVariant) -> dict[str, Any]:
    binding = variant.physical_binding
    return {
        "name": variant.name,
        "contract": variant.path.relative_to(contract.project_root).as_posix(),
        "default_fileset": variant.default_fileset,
        "architecture_validator": variant.architecture_validator,
        "filesets": {
            name: _fileset_source_plan(contract, variant, name)
            for name in variant.filesets
        },
        "physical_binding": {
            "dependency": binding.dependency,
            "transaction_module": binding.transaction_module,
            "physical_shell_module": binding.physical_shell_module,
            "adapter_module": binding.adapter_module,
            "raw_macro_module": binding.raw_macro_module,
            "status": binding.status,
            "blockers": list(binding.blockers),
        },
    }


def _validate_variant_architecture(variant: SocVariant) -> dict[str, Any] | None:
    """Run an explicitly declared product-owned architecture validator."""

    reference = variant.architecture_validator
    if reference is None:
        return None
    module_name, function_name = reference.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(
            f"SoC architecture validator module cannot be imported: {module_name}"
        ) from exc
    validator = getattr(module, function_name, None)
    if not callable(validator):
        raise RuntimeError(f"SoC architecture validator is not callable: {reference}")
    with variant.path.open("rb") as stream:
        raw: dict[str, Any] = tomllib.load(stream)
    result = validator(raw)
    if not isinstance(result, Mapping):
        raise RuntimeError(f"SoC architecture validator returned no mapping: {reference}")
    return dict(result)


def plan_soc(
    contract_path: Path,
    *,
    project_root: Path,
    artifact_root: Path,
) -> dict[str, Any]:
    contract = load_soc_contract(contract_path, project_root=project_root)
    dependencies: list[dict[str, Any]] = []
    for dependency in contract.ips:
        producer_path = _producer_contract(contract, dependency.name)
        expected = plan_ip_release(
            producer_path,
            project_root=contract.project_root,
            artifact_root=artifact_root,
            maturity=dependency.required_maturity,
        )
        exported = _release_export(expected, dependency.export)
        interface = exported.get("interface")
        if not isinstance(interface, Mapping) or (
            interface.get("logical") != dependency.logical_interface
            or interface.get("physical") != dependency.physical_interface
        ):
            raise ValueError(
                f"SoC dependency {dependency.name}/{dependency.export} interface "
                "does not match its release contract"
            )
        for role, expected_module in dependency.role_modules.items():
            role_export = _role_export(dependency, role)
            _release_export(expected, role_export)
            if _planned_role_view(
                expected, role, export=role_export
            ).get("module") != expected_module:
                raise ValueError(
                    f"SoC dependency role {role!r} module does not match its "
                    f"{role_export!r} release export"
                )
        dependencies.append(
            {
                "name": dependency.name,
                "export": dependency.export,
                "provider": expected["contract"],
                "required_maturity": dependency.required_maturity,
                "logical_interface": dependency.logical_interface,
                "physical_interface": dependency.physical_interface,
                "role_modules": dict(dependency.role_modules),
                "role_exports": dict(dependency.role_exports),
                "expected_source_fingerprint": expected["source_fingerprint"],
                "expected_release_id": expected["release_id"],
            }
        )
    return {
        "soc": contract.name,
        "contract": contract.path.relative_to(contract.project_root).as_posix(),
        "lock": contract.lock.as_posix(),
        "source_only": True,
        "ip_dependencies": dependencies,
        "source_ip": [
            {
                "name": item.name,
                "component": item.component_contract.as_posix(),
                "fileset": item.fileset,
            }
            for item in contract.source_ips
        ],
        "implementation": {
            name: path.as_posix()
            for name, path in contract.implementation_profiles.items()
        },
        "variants": [
            _variant_source_plan(contract, variant) for variant in contract.variants
        ],
    }


def plan_soc_fileset(
    contract_path: Path,
    *,
    project_root: Path,
    variant_name: str,
    fileset_name: str | None = None,
) -> dict[str, Any]:
    """Plan one declared product fileset without resolving release collateral."""

    contract = load_soc_contract(contract_path, project_root=project_root)
    variant = contract.get_variant(variant_name)
    fileset = variant.get_fileset(fileset_name)
    return _fileset_source_plan(contract, variant, fileset.name)


def _level_satisfies(actual: str, required: str) -> bool:
    return RELEASE_MATURITY_LEVELS.index(actual) >= RELEASE_MATURITY_LEVELS.index(required)


def _release_export(
    manifest: Mapping[str, Any], export_name: str
) -> Mapping[str, Any]:
    exports = manifest.get("exports")
    if not isinstance(exports, list):
        raise RuntimeError("IP release has no exports")
    matches = [
        item
        for item in exports
        if isinstance(item, Mapping) and item.get("name") == export_name
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"IP release must contain exactly one {export_name!r} export"
        )
    return matches[0]


def _planned_role_view(
    plan: Mapping[str, Any], role: str, *, export: str
) -> Mapping[str, Any]:
    """Resolve one module-bearing role from a source-only IP release plan."""

    collateral = plan.get("collateral")
    if not isinstance(collateral, list):
        raise RuntimeError("IP release plan has no collateral")
    matches = [
        item
        for item in collateral
        if isinstance(item, Mapping)
        and item.get("export") == export
        and item.get("role") == role
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"IP release plan must contain exactly one {export}/{role} collateral"
        )
    return matches[0]


def _locked_release_manifest(
    *,
    contract: SocContract,
    artifact_root: Path,
    dependency: SocIpDependency,
    pinned: LockedIpRelease,
) -> tuple[Path, Mapping[str, Any]]:
    dependency_name = dependency.name
    manifest_path, manifest = resolve_locked_ip_release(
        artifact_root=artifact_root,
        pinned=pinned,
    )
    if manifest.get("ip_name") != dependency_name:
        raise RuntimeError("SoC IP lock identity does not match its dependency")
    exported = _release_export(manifest, dependency.export)
    interface = exported.get("interface")
    if not isinstance(interface, Mapping) or (
        interface.get("logical") != dependency.logical_interface
        or interface.get("physical") != dependency.physical_interface
    ):
        raise RuntimeError(
            "SoC IP release export interface does not match integration intent"
        )
    maturity = manifest.get("maturity")
    assert isinstance(maturity, Mapping)
    checks = maturity.get("checks")
    if not isinstance(checks, list) or not checks or any(
        not isinstance(check, Mapping) or check.get("passed") is not True
        for check in checks
    ):
        raise RuntimeError("SoC IP release maturity checks are incomplete")
    return manifest_path, manifest


def resolve_locked_ip_release(
    *,
    artifact_root: Path,
    pinned: LockedIpRelease,
) -> tuple[Path, Mapping[str, Any]]:
    """Resolve one exact cross-owner Release without following producer state."""

    manifest_path = (artifact_root.resolve() / Path(pinned.manifest)).resolve()
    if not manifest_path.is_relative_to(artifact_root.resolve()):
        raise RuntimeError("SoC IP lock escapes the artifact root")
    manifest = audit_ip_release_manifest(manifest_path)
    if (
        manifest.get("ip_name") != pinned.name
        or manifest.get("release_id") != pinned.release_id
    ):
        raise RuntimeError("SoC IP lock identity does not match its manifest")
    maturity = manifest.get("maturity")
    if not isinstance(maturity, Mapping):
        raise RuntimeError("SoC IP release has no maturity record")
    if maturity.get("level") != pinned.maturity:
        raise RuntimeError("SoC IP lock maturity does not match its manifest")
    source = manifest.get("source")
    provenance = manifest.get("provenance")
    source_clean = (
        isinstance(source, Mapping)
        and source.get("dirty") is False
    ) or (
        isinstance(provenance, Mapping)
        and provenance.get("working_tree_dirty") is False
    )
    if not source_clean:
        raise RuntimeError("SoC cannot consume an IP release built from dirty source")
    return manifest_path, manifest


def check_soc(
    contract_path: Path,
    *,
    project_root: Path,
    artifact_root: Path,
    variant_name: str,
    fileset_name: str | None = None,
    lock_path: Path | None = None,
) -> dict[str, Any]:
    contract = load_soc_contract(contract_path, project_root=project_root)
    resolved_artifact_root = artifact_root.resolve()
    variant = contract.get_variant(variant_name)
    architecture = _validate_variant_architecture(variant)
    fileset = variant.get_fileset(fileset_name)
    if (
        fileset.required_capability in {"synthesis", "physical_implementation"}
        and variant.physical_binding.status != "ready"
    ):
        raise RuntimeError(
            f"SoC physical binding is blocked: {', '.join(variant.physical_binding.blockers)}"
        )
    source_plan = _fileset_source_plan(contract, variant, fileset.name)
    selected_lock = lock_path or (contract.path.parent / Path(contract.lock))
    lock = load_soc_lock(selected_lock, contract=contract)
    locked_by_name = {item.name: item for item in lock.ips}
    resolved_ip: list[dict[str, Any]] = []
    release_sources: list[str] = []
    for dependency in contract.ips:
        pinned = locked_by_name[dependency.name]
        manifest_path, manifest = _locked_release_manifest(
            contract=contract,
            artifact_root=artifact_root,
            dependency=dependency,
            pinned=pinned,
        )
        maturity = manifest.get("maturity")
        assert isinstance(maturity, Mapping)
        actual_level = str(maturity.get("level"))
        if actual_level not in RELEASE_MATURITY_LEVELS or not _level_satisfies(
            actual_level, dependency.required_maturity
        ):
            raise RuntimeError(
                f"SoC IP release maturity {actual_level!r} does not satisfy "
                f"{dependency.required_maturity!r}"
            )
        exported = _release_export(manifest, dependency.export)
        availability = exported.get("availability")
        if not isinstance(availability, Mapping) or availability.get(
            fileset.required_capability
        ) is not True:
            raise RuntimeError(
                f"SoC IP release is unavailable for {fileset.required_capability}"
            )
        roles = fileset.ip_roles[dependency.name]
        for role in roles:
            expected_module = dependency.role_modules.get(role)
            role_export = _role_export(dependency, role)
            role_exported = _release_export(manifest, role_export)
            role_availability = role_exported.get("availability")
            if not isinstance(role_availability, Mapping) or role_availability.get(
                fileset.required_capability
            ) is not True:
                raise RuntimeError(
                    f"SoC IP release role {role!r} is unavailable from export "
                    f"{role_export!r} for {fileset.required_capability}"
                )
            if expected_module is not None and release_role_view(
                manifest, role, export=role_export
            ).get("module") != expected_module:
                raise RuntimeError(
                    f"SoC IP release role {role!r} module does not match integration intent"
                )
        paths = [
            resolve_release_role(
                manifest,
                manifest_path,
                role,
                export=_role_export(dependency, role),
            )
            for role in roles
        ]
        relative_paths = [
            path.relative_to(resolved_artifact_root).as_posix() for path in paths
        ]
        release_sources.extend(relative_paths)
        resolved_ip.append(
            {
                "name": dependency.name,
                "export": dependency.export,
                "release_id": pinned.release_id,
                "source_fingerprint": manifest["source_fingerprint"],
                "maturity": actual_level,
                "manifest": manifest_path.relative_to(
                    resolved_artifact_root
                ).as_posix(),
                "role_exports": {
                    role: _role_export(dependency, role) for role in roles
                },
                "roles": {
                    role: path
                    for role, path in zip(roles, relative_paths, strict=True)
                },
            }
        )
    return {
        "schema": 1,
        "contract_kind": "soc-check",
        "owner": "soc",
        "soc": contract.name,
        "variant": variant.name,
        "fileset": fileset.name,
        "lock": lock.path.relative_to(contract.project_root).as_posix(),
        "passed": True,
        "architecture": architecture,
        "ip_releases": resolved_ip,
        "product_sources": source_plan["sources"],
        "release_sources": release_sources,
    }


def resolve_soc_ip_role(
    contract_path: Path,
    *,
    project_root: Path,
    artifact_root: Path,
    dependency_name: str,
    role: str,
    lock_path: Path | None = None,
) -> Path:
    contract = load_soc_contract(contract_path, project_root=project_root)
    if dependency_name not in {item.name for item in contract.ips}:
        raise KeyError(f"unknown SoC IP dependency: {dependency_name}")
    selected_lock = lock_path or (contract.path.parent / Path(contract.lock))
    lock = load_soc_lock(selected_lock, contract=contract)
    pinned = next(item for item in lock.ips if item.name == dependency_name)
    manifest_path, manifest = _locked_release_manifest(
        contract=contract,
        artifact_root=artifact_root,
        dependency=next(item for item in contract.ips if item.name == dependency_name),
        pinned=pinned,
    )
    dependency = next(item for item in contract.ips if item.name == dependency_name)
    return resolve_release_role(
        manifest,
        manifest_path,
        role,
        export=_role_export(dependency, role),
    )


def resolve_soc_fileset(
    contract_path: Path,
    *,
    project_root: Path,
    artifact_root: Path,
    variant_name: str,
    fileset_name: str | None = None,
    lock_path: Path | None = None,
) -> tuple[Path, ...]:
    """Resolve one complete, declared SoC compilation unit through its exact lock."""

    result = check_soc(
        contract_path,
        project_root=project_root,
        artifact_root=artifact_root,
        variant_name=variant_name,
        fileset_name=fileset_name,
        lock_path=lock_path,
    )
    product_sources = tuple(
        (project_root.resolve() / Path(value)).resolve()
        for value in result["product_sources"]
    )
    release_sources = tuple(
        (artifact_root.resolve() / Path(value)).resolve()
        for value in result["release_sources"]
    )
    return (*product_sources, *release_sources)
