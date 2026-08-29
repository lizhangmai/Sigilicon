"""Plan composite IPs and resolve their exact immutable release dependencies."""

from __future__ import annotations

import importlib
from pathlib import Path
import tomllib
from typing import Any, Mapping

from sigilicon.domain.config_contracts import require_config_header
from sigilicon.domain.ip_integration import (
    IpIntegrationContract,
    IpIntegrationDependency,
    IpOperatingVariant,
    IpReleaseDependency,
    LockedIpRelease,
    load_ip_dependency_lock,
    load_ip_integration_contract,
)
from sigilicon.domain.ip_release import (
    RELEASE_MATURITY_LEVELS,
    IpContract,
    load_ip_contract,
)
from sigilicon.domain.repository import Project
from sigilicon.workflows.ip_packaging import (
    audit_ip_release_manifest,
    plan_ip_release_contract,
    release_role_view,
    resolve_release_role,
)


def _ip_catalog(project: Project) -> tuple[Path, Mapping[str, Any]]:
    snapshot = project.ip_catalog_snapshot()
    require_config_header(
        snapshot.document,
        snapshot.path,
        contract_kind="ip-catalog",
        path_scope="repository",
        owner=project.manifest_owner,
    )
    return snapshot.path, snapshot.document


def ip_catalog_contract_path(
    root: Path | None,
    target: str,
    *,
    project: Project | None = None,
    section: str = "targets",
) -> Path:
    """Resolve one release or component contract through the canonical IP catalog."""

    if section not in {"targets", "components"}:
        raise ValueError(f"unsupported IP catalog section: {section}")
    repository = Project.bind(project=project, project_root=root)
    _, raw = _ip_catalog(repository)
    entries = raw.get(section)
    if not isinstance(entries, Mapping):
        raise ValueError(f"IP catalog {section} must be a table")
    entry = entries.get(target)
    if not isinstance(entry, Mapping) or not isinstance(entry.get("contract"), str):
        raise KeyError(f"unknown IP {section[:-1]}: {target}")
    project_root = repository.project_root
    relative = Path(entry["contract"])
    path = (project_root / relative).resolve()
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not path.is_relative_to(project_root)
        or not path.is_file()
    ):
        raise FileNotFoundError(f"IP catalog contract is missing or unsafe: {relative}")
    return path


def _producer_contract(
    contract: IpIntegrationContract,
    dependency_name: str,
) -> IpContract:
    path = ip_catalog_contract_path(
        None,
        dependency_name,
        project=contract.project,
        section="targets",
    )
    producer = load_ip_contract(path, project=contract.project)
    if producer.name != dependency_name:
        raise ValueError(f"IP catalog identity mismatch: {dependency_name}")
    return producer


def _role_export(release: IpReleaseDependency, role: str) -> str:
    return release.role_exports.get(role, release.export)


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


def _allowed_files(
    contract: IpIntegrationContract,
    variant: IpOperatingVariant,
    fileset_name: str,
) -> set[Path]:
    root = contract.project_root
    allowed = {
        (root / Path(relative)).resolve()
        for values in contract.component.filesets.values()
        for relative in values
    }
    graph = contract.component_graph
    fileset = variant.get_fileset(fileset_name)
    for dependency_name, source_fileset in fileset.source_filesets.items():
        allowed.update(
            (root / Path(relative)).resolve()
            for relative in graph[dependency_name].filesets[source_fileset]
        )
    return allowed


def _filelist(
    path: Path,
    contract: IpIntegrationContract,
    variant: IpOperatingVariant,
    fileset_name: str,
) -> tuple[Path, ...]:
    root = contract.project_root
    if not path.is_file() or not path.is_relative_to(root):
        raise FileNotFoundError(f"IP filelist is missing or unsafe: {path}")
    allowed = _allowed_files(contract, variant, fileset_name)
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
            or source not in allowed
        ):
            raise RuntimeError(
                "IP filelist may contain only owner files or explicitly selected "
                f"component source files: {line}"
            )
        sources.append(source)
    if not sources:
        raise RuntimeError(f"IP filelist is empty: {path}")
    return tuple(sources)


def _fileset_source_plan(
    contract: IpIntegrationContract,
    variant: IpOperatingVariant,
    fileset_name: str,
) -> dict[str, Any]:
    root = contract.project_root
    fileset = variant.get_fileset(fileset_name)
    filelist_path = (root / Path(fileset.filelist)).resolve()
    if not filelist_path.is_relative_to(root):
        raise FileNotFoundError(f"IP filelist escapes the project root: {fileset.filelist}")
    sources = _filelist(filelist_path, contract, variant, fileset.name)
    return {
        "name": fileset.name,
        "filelist": filelist_path.relative_to(root).as_posix(),
        "sources": [path.relative_to(root).as_posix() for path in sources],
        "dependency_roles": {
            name: list(roles) for name, roles in fileset.dependency_roles.items()
        },
        "source_filesets": dict(fileset.source_filesets),
        "required_capability": fileset.required_capability,
    }


def _binding_plan(variant: IpOperatingVariant) -> dict[str, Any] | None:
    binding = variant.physical_binding
    if binding is None:
        return None
    return {
        "dependency": binding.dependency,
        "transaction_module": binding.transaction_module,
        "physical_shell_module": binding.physical_shell_module,
        "adapter_module": binding.adapter_module,
        "raw_macro_module": binding.raw_macro_module,
        "status": binding.status,
        "blockers": list(binding.blockers),
    }


def _variant_source_plan(
    contract: IpIntegrationContract, variant: IpOperatingVariant
) -> dict[str, Any]:
    return {
        "name": variant.name,
        "contract": variant.path.relative_to(contract.project_root).as_posix(),
        "default_fileset": variant.default_fileset,
        "architecture_validator": variant.architecture_validator,
        "filesets": {
            name: _fileset_source_plan(contract, variant, name)
            for name in variant.filesets
        },
        "physical_binding": _binding_plan(variant),
    }


def _validate_variant_architecture(
    variant: IpOperatingVariant,
) -> dict[str, Any] | None:
    reference = variant.architecture_validator
    if reference is None:
        return None
    module_name, function_name = reference.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(
            f"IP architecture validator module cannot be imported: {module_name}"
        ) from exc
    validator = getattr(module, function_name, None)
    if not callable(validator):
        raise RuntimeError(f"IP architecture validator is not callable: {reference}")
    with variant.path.open("rb") as stream:
        raw: dict[str, Any] = tomllib.load(stream)
    result = validator(raw)
    if not isinstance(result, Mapping):
        raise RuntimeError(f"IP architecture validator returned no mapping: {reference}")
    return dict(result)


def _integration_project(
    *,
    project: Project | None,
    project_root: Path | None,
    artifact_root: Path | None,
) -> Project:
    repository = Project.bind(project=project, project_root=project_root)
    return (
        repository
        if artifact_root is None
        else repository.with_artifact_root(artifact_root)
    )


def plan_ip_integration(
    contract_path: Path,
    *,
    project: Project | None = None,
    project_root: Path | None = None,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    """Validate source intent without resolving or consuming a dependency lock."""

    repository = _integration_project(
        project=project,
        project_root=project_root,
        artifact_root=artifact_root,
    )
    contract = load_ip_integration_contract(contract_path, project=repository)
    dependencies: list[dict[str, Any]] = []
    for dependency in contract.dependencies:
        row: dict[str, Any] = {
            "name": dependency.name,
            "component": dependency.component_contract.as_posix(),
        }
        release = dependency.release
        if release is not None:
            producer = _producer_contract(contract, dependency.name)
            expected = plan_ip_release_contract(
                producer,
                maturity=release.required_maturity,
            )
            exported = _release_export(expected, release.export)
            interface = exported.get("interface")
            if not isinstance(interface, Mapping) or (
                interface.get("logical") != release.logical_interface
                or interface.get("physical") != release.physical_interface
            ):
                raise ValueError(
                    f"IP dependency {dependency.name}/{release.export} interface "
                    "does not match its release contract"
                )
            for role in release.roles:
                role_export = _role_export(release, role)
                _release_export(expected, role_export)
                expected_module = release.role_modules.get(role)
                if (
                    expected_module is not None
                    and _planned_role_view(
                        expected, role, export=role_export
                    ).get("module") != expected_module
                ):
                    raise ValueError(
                        f"IP dependency role {role!r} module does not match its "
                        f"{role_export!r} release export"
                    )
            row["release"] = {
                "export": release.export,
                "provider": expected["contract"],
                "required_maturity": release.required_maturity,
                "logical_interface": release.logical_interface,
                "physical_interface": release.physical_interface,
                "roles": list(release.roles),
                "role_modules": dict(release.role_modules),
                "role_exports": dict(release.role_exports),
                "expected_release_id": expected["release_id"],
            }
        dependencies.append(row)
    return {
        "schema": 1,
        "contract_kind": "ip-integration-plan",
        "owner": contract.owner,
        "ip": contract.name,
        "contract": contract.path.relative_to(contract.project_root).as_posix(),
        "dependency_lock": (
            None
            if contract.dependency_lock is None
            else contract.dependency_lock.as_posix()
        ),
        "source_only": True,
        "dependencies": dependencies,
        "implementation": {
            name: path.as_posix()
            for name, path in contract.implementation_profiles.items()
        },
        "variants": [
            _variant_source_plan(contract, variant) for variant in contract.variants
        ],
    }


def plan_ip_integration_fileset(
    contract_path: Path,
    *,
    project: Project | None = None,
    project_root: Path | None = None,
    variant_name: str,
    fileset_name: str | None = None,
) -> dict[str, Any]:
    repository = Project.bind(project=project, project_root=project_root)
    contract = load_ip_integration_contract(contract_path, project=repository)
    variant = contract.get_variant(variant_name)
    fileset = variant.get_fileset(fileset_name)
    return _fileset_source_plan(contract, variant, fileset.name)


def _level_satisfies(actual: str, required: str) -> bool:
    return RELEASE_MATURITY_LEVELS.index(actual) >= RELEASE_MATURITY_LEVELS.index(required)


def resolve_locked_ip_release(
    *,
    artifact_root: Path,
    pinned: LockedIpRelease,
) -> tuple[Path, Mapping[str, Any]]:
    """Resolve one exact cross-owner release without following producer state."""

    root = artifact_root.resolve()
    manifest_path = root / Path(pinned.manifest)
    if not manifest_path.resolve().is_relative_to(root):
        raise RuntimeError("IP dependency lock escapes the artifact root")
    manifest = audit_ip_release_manifest(manifest_path)
    if (
        manifest.get("ip_name") != pinned.name
        or manifest.get("release_id") != pinned.release_id
    ):
        raise RuntimeError("IP dependency lock identity does not match its manifest")
    maturity = manifest.get("maturity")
    if not isinstance(maturity, Mapping):
        raise RuntimeError("IP dependency release has no maturity record")
    if maturity.get("level") != pinned.maturity:
        raise RuntimeError("IP dependency lock maturity does not match its manifest")
    source = manifest.get("source")
    provenance = manifest.get("provenance")
    source_clean = (
        isinstance(source, Mapping) and source.get("dirty") is False
    ) or (
        isinstance(provenance, Mapping)
        and provenance.get("working_tree_dirty") is False
    )
    if not source_clean:
        raise RuntimeError("IP cannot consume a dependency release built from dirty source")
    return manifest_path, manifest


def _locked_release_manifest(
    *,
    contract: IpIntegrationContract,
    artifact_root: Path,
    dependency: IpIntegrationDependency,
    pinned: LockedIpRelease,
) -> tuple[Path, Mapping[str, Any]]:
    release = dependency.release
    if release is None:
        raise RuntimeError(f"IP dependency {dependency.name} has no release contract")
    manifest_path, manifest = resolve_locked_ip_release(
        artifact_root=artifact_root,
        pinned=pinned,
    )
    if manifest.get("ip_name") != dependency.name:
        raise RuntimeError("IP dependency lock identity does not match its dependency")
    component_path = (
        contract.project_root / Path(dependency.component_contract)
    ).resolve()
    expected_owner = contract.project.require_owner(component_path)
    provenance = manifest.get("provenance")
    expected_producer = expected_owner.root.relative_to(
        contract.project_root
    ).as_posix()
    if not isinstance(provenance, Mapping) or (
        provenance.get("producer") != expected_producer
    ):
        raise RuntimeError("IP dependency release does not match its provider owner")
    exported = _release_export(manifest, release.export)
    interface = exported.get("interface")
    if not isinstance(interface, Mapping) or (
        interface.get("logical") != release.logical_interface
        or interface.get("physical") != release.physical_interface
    ):
        raise RuntimeError(
            "IP dependency release export interface does not match integration intent"
        )
    maturity = manifest.get("maturity")
    assert isinstance(maturity, Mapping)
    checks = maturity.get("checks")
    if not isinstance(checks, list) or not checks or any(
        not isinstance(check, Mapping) or check.get("passed") is not True
        for check in checks
    ):
        raise RuntimeError("IP dependency release maturity checks are incomplete")
    return manifest_path, manifest


def _selected_lock(
    contract: IpIntegrationContract, lock_path: Path | None
) -> Path:
    if lock_path is not None:
        return lock_path
    if contract.dependency_lock is None:
        raise RuntimeError("IP integration has no dependency lock")
    return contract.project_root / Path(contract.dependency_lock)


def check_ip_integration(
    contract_path: Path,
    *,
    project: Project | None = None,
    project_root: Path | None = None,
    artifact_root: Path | None = None,
    variant_name: str,
    fileset_name: str | None = None,
    lock_path: Path | None = None,
) -> dict[str, Any]:
    """Resolve one variant through its explicitly selected immutable releases."""

    repository = _integration_project(
        project=project,
        project_root=project_root,
        artifact_root=artifact_root,
    )
    contract = load_ip_integration_contract(contract_path, project=repository)
    root = contract.project_root
    artifact_root = contract.project.artifact_root
    variant = contract.get_variant(variant_name)
    architecture = _validate_variant_architecture(variant)
    fileset = variant.get_fileset(fileset_name)
    binding = variant.physical_binding
    if (
        fileset.required_capability in {"synthesis", "physical_implementation"}
        and binding is not None
        and binding.status != "ready"
    ):
        raise RuntimeError(
            f"IP physical binding is blocked: {', '.join(binding.blockers)}"
        )
    source_plan = _fileset_source_plan(contract, variant, fileset.name)

    selected_release_dependencies = tuple(
        dependency
        for dependency in contract.release_dependencies
        if dependency.name in fileset.dependency_roles
    )
    locked_by_name: dict[str, LockedIpRelease] = {}
    lock = None
    if selected_release_dependencies:
        lock = load_ip_dependency_lock(
            _selected_lock(contract, lock_path),
            contract=contract,
        )
        locked_by_name = {item.name: item for item in lock.dependencies}

    resolved_dependencies: list[dict[str, Any]] = []
    release_sources: list[str] = []
    for dependency in selected_release_dependencies:
        release = dependency.release
        assert release is not None
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
            actual_level, release.required_maturity
        ):
            raise RuntimeError(
                f"IP dependency release maturity {actual_level!r} does not satisfy "
                f"{release.required_maturity!r}"
            )
        roles = fileset.dependency_roles.get(dependency.name, ())
        if roles:
            exported = _release_export(manifest, release.export)
            availability = exported.get("availability")
            if not isinstance(availability, Mapping) or availability.get(
                fileset.required_capability
            ) is not True:
                raise RuntimeError(
                    "IP dependency release is unavailable for "
                    f"{fileset.required_capability}"
                )
        relative_paths: list[str] = []
        for role in roles:
            role_export = _role_export(release, role)
            role_exported = _release_export(manifest, role_export)
            availability = role_exported.get("availability")
            if not isinstance(availability, Mapping) or availability.get(
                fileset.required_capability
            ) is not True:
                raise RuntimeError(
                    f"IP dependency role {role!r} is unavailable from export "
                    f"{role_export!r} for {fileset.required_capability}"
                )
            expected_module = release.role_modules.get(role)
            if (
                expected_module is not None
                and release_role_view(
                    manifest, role, export=role_export
                ).get("module") != expected_module
            ):
                raise RuntimeError(
                    f"IP dependency role {role!r} module does not match "
                    "integration intent"
                )
            role_path = resolve_release_role(
                manifest,
                manifest_path,
                role,
                export=role_export,
            )
            relative_paths.append(role_path.relative_to(artifact_root).as_posix())
        release_sources.extend(relative_paths)
        resolved_dependencies.append(
            {
                "name": dependency.name,
                "export": release.export,
                "release_id": pinned.release_id,
                "maturity": actual_level,
                "manifest": manifest_path.relative_to(artifact_root).as_posix(),
                "role_exports": {
                    role: _role_export(release, role) for role in roles
                },
                "roles": {
                    role: path
                    for role, path in zip(roles, relative_paths, strict=True)
                },
            }
        )
    return {
        "schema": 1,
        "contract_kind": "ip-integration-check",
        "owner": contract.owner,
        "ip": contract.name,
        "variant": variant.name,
        "fileset": fileset.name,
        "dependency_lock": (
            None if lock is None else lock.path.relative_to(root).as_posix()
        ),
        "passed": True,
        "architecture": architecture,
        "dependency_releases": resolved_dependencies,
        "source_files": source_plan["sources"],
        "release_sources": release_sources,
    }


def resolve_ip_dependency_role(
    contract_path: Path,
    *,
    project: Project | None = None,
    project_root: Path | None = None,
    artifact_root: Path | None = None,
    dependency_name: str,
    role: str,
    lock_path: Path | None = None,
) -> Path:
    repository = _integration_project(
        project=project,
        project_root=project_root,
        artifact_root=artifact_root,
    )
    contract = load_ip_integration_contract(contract_path, project=repository)
    matches = [
        item
        for item in contract.release_dependencies
        if item.name == dependency_name
    ]
    if len(matches) != 1:
        raise KeyError(f"unknown released IP dependency: {dependency_name}")
    dependency = matches[0]
    lock = load_ip_dependency_lock(
        _selected_lock(contract, lock_path), contract=contract
    )
    pinned = next(item for item in lock.dependencies if item.name == dependency_name)
    manifest_path, manifest = _locked_release_manifest(
        contract=contract,
        artifact_root=contract.project.artifact_root,
        dependency=dependency,
        pinned=pinned,
    )
    release = dependency.release
    assert release is not None
    return resolve_release_role(
        manifest,
        manifest_path,
        role,
        export=_role_export(release, role),
    )


def resolve_ip_integration_fileset(
    contract_path: Path,
    *,
    project: Project | None = None,
    project_root: Path | None = None,
    artifact_root: Path | None = None,
    variant_name: str,
    fileset_name: str | None = None,
    lock_path: Path | None = None,
) -> tuple[Path, ...]:
    """Resolve one complete IP compilation unit for an execution adapter."""

    repository = _integration_project(
        project=project,
        project_root=project_root,
        artifact_root=artifact_root,
    )
    result = check_ip_integration(
        contract_path,
        project=repository,
        variant_name=variant_name,
        fileset_name=fileset_name,
        lock_path=lock_path,
    )
    source_files = tuple(
        (repository.project_root / Path(value)).resolve()
        for value in result["source_files"]
    )
    release_sources = tuple(
        (repository.artifact_root / Path(value)).resolve()
        for value in result["release_sources"]
    )
    return (*source_files, *release_sources)
