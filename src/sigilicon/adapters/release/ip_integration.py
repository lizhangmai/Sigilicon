"""Plan composite IPs and resolve their exact immutable release dependencies."""

from __future__ import annotations

from sigilicon.domain.component import SourceDependency, PackageDependency

from pathlib import Path
from typing import Any, Mapping

from sigilicon.artifacts import _inspect_nofollow_file, read_nofollow_text
from sigilicon.domain.ip_integration import (
    IpIntegrationContract,
    IpOperatingVariant,
    LockedIpRelease,
    MixedSignalPhysicalBinding,
    CircuitPhysicalBinding,
    load_ip_dependency_lock,
    load_ip_integration_contract,
    resolve_ip_integration_contract,
)
from sigilicon.domain.ip_release import (
    RELEASE_MATURITY_LEVELS,
)
from sigilicon.contracts import require_relative_path
from sigilicon.project import Project
from sigilicon.release_store import (
    ReleasePackage,
    ReleaseRef,
    ReleaseStore,
    release_store_resource,
)
from sigilicon.adapters.release.release_semantics import ReleaseView
from sigilicon.adapters.release.ip_packaging import release_view
from sigilicon.adapters.release.ip_packaging import validate_ip_release_package


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


def _allowed_files(
    contract: IpIntegrationContract,
    variant: IpOperatingVariant,
    fileset_name: str,
) -> set[Path]:
    root = contract.project_root
    allowed = {
        (root / Path(relative)).resolve()
        for relative in contract.component.sources.values()
    }
    graph = contract.component_graph
    fileset = variant.get_fileset(fileset_name)
    for dependency_name, source_fileset in fileset.source_filesets.items():
        allowed.update(
            (root / Path(relative)).resolve()
            for relative in graph[dependency_name].fileset_paths(source_fileset)
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
    seen: set[Path] = set()
    for raw_line in read_nofollow_text(path).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            relative = require_relative_path(line, "IP filelist source")
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        source = root.joinpath(*relative.parts).resolve()
        if (
            not source.is_file()
            or source not in allowed
        ):
            raise RuntimeError(
                "IP filelist may contain only owner files or explicitly selected "
                f"component source files: {line}"
            )
        if source in seen:
            raise RuntimeError(f"IP filelist contains duplicate source: {line}")
        seen.add(source)
        sources.append(source)
    if not sources:
        raise RuntimeError(f"IP filelist is empty: {path}")
    return tuple(sources)


def _fileset_source_plan(
    contract: IpIntegrationContract,
    variant: IpOperatingVariant,
    fileset_name: str,
    *,
    project: Project,
) -> dict[str, Any]:
    fileset = variant.get_fileset(fileset_name)
    contract.repository.validate(project)
    filelist_path, filelist_relative = project.resolve_owner_file(
        contract.owner,
        fileset.filelist.as_posix(),
        f"variant {variant.name}.filesets.{fileset.name}.filelist",
    )
    sources = _filelist(filelist_path, contract, variant, fileset.name)
    filelist_metadata, filelist_digest = _inspect_nofollow_file(filelist_path)
    return {
        "name": fileset.name,
        "filelist": filelist_relative.as_posix(),
        "filelist_size": filelist_metadata.st_size,
        "filelist_sha256": filelist_digest,
        "sources": [
            path.relative_to(contract.project_root).as_posix() for path in sources
        ],
        "dependency_views": {
            name: list(views) for name, views in fileset.dependency_views.items()
        },
        "source_filesets": dict(fileset.source_filesets),
        "required_capability": fileset.required_capability,
    }


def _binding_plan(variant: IpOperatingVariant) -> dict[str, Any] | None:
    binding = variant.physical_binding
    if binding is None:
        return None
    row = {
        "kind": binding.kind,
        "dependency": binding.dependency,
        "transaction_module": binding.transaction_module,
        "adapter_module": binding.adapter_module,
        "status": binding.status,
        "blockers": list(binding.blockers),
    }
    if isinstance(binding, MixedSignalPhysicalBinding):
        row.update(
            {
                "physical_shell_module": binding.physical_shell_module,
                "raw_macro_module": binding.raw_macro_module,
            }
        )
    else:
        assert isinstance(binding, CircuitPhysicalBinding)
    return row


def _variant_source_plan(
    contract: IpIntegrationContract,
    variant: IpOperatingVariant,
    *,
    project: Project,
) -> dict[str, Any]:
    return {
        "name": variant.name,
        "contract": variant.path.relative_to(contract.project_root).as_posix(),
        "default_fileset": variant.default_fileset,
        "filesets": {
            name: _fileset_source_plan(contract, variant, name, project=project)
            for name in variant.filesets
        },
        "physical_binding": _binding_plan(variant),
    }


def plan_ip_integration(
    contract_path: Path,
    *,
    project: Project,
) -> dict[str, Any]:
    """Validate source intent without resolving or consuming a dependency lock."""

    contract = load_ip_integration_contract(contract_path, project=project)
    return plan_ip_integration_contract(
        contract,
        project=project,
    )


def plan_ip_integration_contract(
    contract: IpIntegrationContract,
    *,
    project: Project,
) -> dict[str, Any]:
    """Plan one already validated composite-IP integration contract."""

    contract = resolve_ip_integration_contract(
        contract.path,
        project=project,
        snapshot=contract,
    )
    dependencies = []
    for dependency in contract.dependencies:
        row = {"name": dependency.name}
        if isinstance(dependency, SourceDependency):
            row["source"] = {"contract": dependency.contract.as_posix()}
        else:
            release = dependency.release
            row["package"] = {"export": release.export, "required_maturity": release.required_maturity,
                              "views": list(release.views)}
        dependencies.append(row)
    return {
        "schema": 2,
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
            _variant_source_plan(contract, variant, project=project)
            for variant in contract.variants
        ],
    }


def plan_ip_integration_fileset(
    contract_path: Path,
    *,
    project: Project,
    variant_name: str,
    fileset_name: str | None = None,
) -> dict[str, Any]:
    repository = project
    contract = load_ip_integration_contract(contract_path, project=repository)
    variant = contract.get_variant(variant_name)
    fileset = variant.get_fileset(fileset_name)
    return _fileset_source_plan(
        contract,
        variant,
        fileset.name,
        project=repository,
    )


def _level_satisfies(actual: str, required: str) -> bool:
    return RELEASE_MATURITY_LEVELS.index(actual) >= RELEASE_MATURITY_LEVELS.index(required)


def resolve_locked_ip_release(
    *,
    release_store_root: Path,
    pinned: LockedIpRelease,
) -> ReleasePackage:
    """Resolve one exact cross-owner release without following producer state."""

    release = ReleaseStore(release_store_root).open(
        ReleaseRef(pinned.store, pinned.manifest_sha256),
        validate=validate_ip_release_package,
    )
    manifest = release.manifest
    if (
        manifest.get("ip_name") != pinned.name
        or manifest.get("release_id") != pinned.release_id
    ):
        raise RuntimeError("IP dependency lock identity does not match its manifest")
    if manifest.get("source_commit") != pinned.source_commit:
        raise RuntimeError(
            "IP dependency lock source commit does not match its manifest"
        )
    maturity = manifest.get("maturity")
    if not isinstance(maturity, Mapping):
        raise RuntimeError("IP dependency release has no maturity record")
    if maturity.get("level") != pinned.maturity:
        raise RuntimeError("IP dependency lock maturity does not match its manifest")
    return release


def _locked_release_manifest(
    *,
    release_store_root: Path,
    dependency: PackageDependency,
    pinned: LockedIpRelease,
) -> ReleasePackage:
    release = dependency.release
    audited = resolve_locked_ip_release(
        release_store_root=release_store_root,
        pinned=pinned,
    )
    manifest = audited.manifest
    if manifest.get("ip_name") != dependency.name:
        raise RuntimeError("IP dependency lock identity does not match its dependency")
    _release_export(manifest, release.export)
    return audited


def check_ip_integration(
    contract_path: Path,
    *,
    project: Project,
    variant_name: str,
    fileset_name: str | None = None,
) -> dict[str, Any]:
    """Resolve one variant through its explicitly selected immutable releases."""

    contract = load_ip_integration_contract(contract_path, project=project)
    root = contract.project_root
    contract.repository.validate(project)
    runtime = project.resources()
    variant = contract.get_variant(variant_name)
    fileset = variant.get_fileset(fileset_name)
    binding = variant.physical_binding
    if (
        fileset.required_capability == "physical_implementation"
        and binding is not None
        and binding.status != "ready"
    ):
        raise RuntimeError(
            f"IP physical binding is blocked: {', '.join(binding.blockers)}"
        )
    source_plan = _fileset_source_plan(
        contract,
        variant,
        fileset.name,
        project=project,
    )

    selected_release_dependencies = tuple(
        dependency
        for dependency in contract.release_dependencies
        if dependency.name in fileset.dependency_views
    )
    locked_by_name: dict[str, LockedIpRelease] = {}
    lock = None
    if selected_release_dependencies:
        lock = load_ip_dependency_lock(contract=contract, project=project)
        locked_by_name = {item.name: item for item in lock.dependencies}

    resolved_dependencies: list[dict[str, Any]] = []
    release_sources: list[str] = []
    for dependency in selected_release_dependencies:
        release = dependency.release
        assert release is not None
        pinned = locked_by_name[dependency.name]
        store_root = Path(
            runtime.require_destination(release_store_resource(pinned.store))
        )
        audited = _locked_release_manifest(
            release_store_root=store_root,
            dependency=dependency,
            pinned=pinned,
        )
        manifest = audited.manifest
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
        views = fileset.dependency_views.get(dependency.name, ())
        exported = _release_export(manifest, release.export)
        availability = exported.get("availability")
        if views and (not isinstance(availability, Mapping) or availability.get(fileset.required_capability) is not True):
            raise RuntimeError(f"IP dependency release is unavailable for {fileset.required_capability}")
        relative_paths: list[str] = []
        for view_name in views:
            view = ReleaseView.from_record(release_view(manifest, view_name, export=release.export))
            if not view.supports(fileset.required_capability):
                raise RuntimeError(f"IP dependency view_name {view_name!r} is unavailable from export {release.export!r} for {fileset.required_capability}")
            artifact = audited.view(release.export, view_name)
            relative_paths.append(
                (
                    Path("release-store")
                    / pinned.store
                    / "objects"
                    / f"sha256-{pinned.manifest_sha256}"
                    / artifact.relative_path
                ).as_posix()
            )
        release_sources.extend(relative_paths)
        resolved_dependencies.append(
            {
                "name": dependency.name,
                "export": release.export,
                "release_id": pinned.release_id,
                "source_commit": pinned.source_commit,
                "store": pinned.store,
                "manifest_sha256": pinned.manifest_sha256,
                "maturity": actual_level,
                "views": {
                    view_name: path
                    for view_name, path in zip(views, relative_paths, strict=True)
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
        "dependency_releases": resolved_dependencies,
        "source_files": source_plan["sources"],
        "release_sources": release_sources,
    }


def resolve_ip_integration_fileset(
    contract_path: Path,
    *,
    project: Project,
    variant_name: str,
    fileset_name: str | None = None,
) -> tuple[Path, ...]:
    """Resolve one complete IP compilation unit for an execution adapter."""

    result = check_ip_integration(
        contract_path,
        project=project,
        variant_name=variant_name,
        fileset_name=fileset_name,
    )
    source_files = tuple(
        (project.project_root / Path(value)).resolve()
        for value in result["source_files"]
    )
    runtime = project.resources()
    release_sources: list[Path] = []
    for dependency in result["dependency_releases"]:
        pinned = LockedIpRelease(
            name=dependency["name"],
            release_id=dependency["release_id"],
            store=dependency["store"],
            maturity=dependency["maturity"],
            source_commit=dependency["source_commit"],
            manifest_sha256=dependency["manifest_sha256"],
        )
        store_root = Path(
            runtime.require_destination(release_store_resource(pinned.store))
        )
        package = resolve_locked_ip_release(
            release_store_root=store_root,
            pinned=pinned,
        )
        release_sources.extend(
            package.view(dependency["export"], view_name).path
            for view_name in dependency["views"]
        )
    return (*source_files, *release_sources)
