"""Composite-IP integration intent and exact dependency release selection."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import tomllib
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Mapping

from sigilicon.project._component import (
    ComponentContract,
    load_component_contract,
    load_component_graph,
    resolve_component_contract,
    resolve_component_graph,
)
from sigilicon.contracts import (
    freeze_toml_document,
    is_frozen_toml_document,
    require_config_header,
)
from sigilicon.domain.ip_release import RELEASE_MATURITY_LEVELS, safe_relative
from sigilicon.release_store import ReleaseRef

if TYPE_CHECKING:
    from sigilicon.project import Project


_CAPABILITIES = frozenset({"simulation", "synthesis", "physical_implementation"})
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))


def _table(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a TOML table")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _hex_digest(
    value: object,
    label: str,
    *,
    lengths: tuple[int, ...],
) -> str:
    digest = _string(value, label)
    if len(digest) not in lengths or any(
        character not in "0123456789abcdef" for character in digest
    ):
        allowed = " or ".join(str(length) for length in lengths)
        raise ValueError(
            f"{label} must be a {allowed}-character lowercase hex digest"
        )
    return digest


@dataclass(frozen=True)
class IpReleaseDependency:
    """One producer export and the roles consumed from that same export."""

    export: str
    required_maturity: str
    roles: tuple[str, ...]


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
class OaMixedSignalPhysicalBinding:
    """Consumer transaction shell bound to a mixed-signal OA release."""

    dependency: str
    transaction_module: str
    physical_shell_module: str
    adapter_module: str
    raw_macro_module: str
    status: str
    blockers: tuple[str, ...]
    kind: Literal["oa-mixed-signal"] = field(
        default="oa-mixed-signal",
        init=False,
    )


@dataclass(frozen=True)
class OaNativePhysicalBinding:
    """Consumer-owned transaction adapter bound to a native OA dependency."""

    kind: Literal["oa-native"]
    dependency: str
    transaction_module: str
    adapter_module: str
    status: str
    blockers: tuple[str, ...]


PhysicalBinding = OaMixedSignalPhysicalBinding | OaNativePhysicalBinding


@dataclass(frozen=True)
class IpOperatingVariant:
    name: str
    path: Path
    default_fileset: str
    filesets: Mapping[str, IpIntegrationFileset]
    physical_binding: PhysicalBinding | None
    source_document: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
    )

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
    source_documents: Mapping[Path, Mapping[str, Any]] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
    )

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
    store: str
    object: str
    maturity: str
    source_commit: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        ReleaseRef(self.store, self.object, self.manifest_sha256)


@dataclass(frozen=True)
class IpDependencyLock:
    path: Path
    ip: str
    dependencies: tuple[LockedIpRelease, ...]


_LOCKED_IP_RELEASE_FIELDS = {
    "name",
    "release_id",
    "store",
    "object",
    "maturity",
    "source_commit",
    "manifest_sha256",
}


def parse_locked_ip_release(value: object, label: str) -> LockedIpRelease:
    """Parse the one canonical schema-two dependency-lock entry shape."""

    item = _table(value, label)
    if set(item) != _LOCKED_IP_RELEASE_FIELDS:
        raise ValueError(
            f"{label} fields must be exactly {sorted(_LOCKED_IP_RELEASE_FIELDS)}"
        )
    maturity = _string(item.get("maturity"), f"{label}.maturity")
    if maturity not in RELEASE_MATURITY_LEVELS:
        raise ValueError(f"{label}.maturity is unsupported: {maturity}")
    return LockedIpRelease(
        name=_string(item.get("name"), f"{label}.name"),
        release_id=_string(item.get("release_id"), f"{label}.release_id"),
        store=_string(item.get("store"), f"{label}.store"),
        object=_string(item.get("object"), f"{label}.object"),
        maturity=maturity,
        source_commit=_hex_digest(
            item.get("source_commit"),
            f"{label}.source_commit",
            lengths=(40, 64),
        ),
        manifest_sha256=_hex_digest(
            item.get("manifest_sha256"),
            f"{label}.manifest_sha256",
            lengths=(64,),
        ),
    )


def _release_dependency(value: object, label: str) -> IpReleaseDependency:
    item = _table(value, label)
    required_fields = {"export", "required_maturity", "roles"}
    if set(item) != required_fields:
        raise ValueError(f"{label} fields must be exactly {sorted(required_fields)}")
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
    export = _string(item.get("export"), f"{label}.export")
    return IpReleaseDependency(
        export=export,
        required_maturity=maturity,
        roles=roles,
    )


def _implementation_profiles(
    raw: Mapping[str, Any],
    *,
    project: Project,
    owner: str,
    source_documents: Mapping[Path, Mapping[str, Any]] | None = None,
) -> tuple[Mapping[str, PurePosixPath], Mapping[Path, Mapping[str, Any]]]:
    values = _table(raw.get("implementation", {}), "implementation")
    result: dict[str, PurePosixPath] = {}
    documents: dict[Path, Mapping[str, Any]] = {}
    for name, value in values.items():
        name = _string(name, "implementation profile name")
        path, relative = project.resolve_owner_file(
            owner,
            value,
            f"implementation.{name}",
        )
        if source_documents is None:
            with path.open("rb") as stream:
                profile: Mapping[str, Any] = tomllib.load(stream)
        else:
            profile = source_documents.get(path)
            if not isinstance(profile, Mapping):
                raise ValueError("IP integration source snapshot is incomplete")
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
        documents[path] = (
            freeze_toml_document(profile)
            if source_documents is None
            else profile
        )
    return MappingProxyType(result), MappingProxyType(documents)


def _operating_variant(
    name: str,
    path: Path,
    *,
    contract: ComponentContract,
    graph: Mapping[str, ComponentContract],
    dependencies: Mapping[str, IpIntegrationDependency],
    source_document: Mapping[str, Any] | None = None,
) -> IpOperatingVariant:
    if source_document is None:
        with path.open("rb") as stream:
            raw: Mapping[str, Any] = tomllib.load(stream)
    else:
        raw = source_document
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
                not isinstance(roles_value, (list, tuple))
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
            dependency_roles=MappingProxyType(dependency_roles),
            source_filesets=MappingProxyType(source_filesets),
            required_capability=capability,
        )

    default_fileset = _string(
        integration.get("default_fileset"),
        f"variant {name}.integration.default_fileset",
    )
    if default_fileset not in filesets:
        raise ValueError(f"variant {name} default_fileset is undeclared")

    physical_binding: PhysicalBinding | None = None
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
        if not isinstance(blockers_raw, (list, tuple)) or any(
            not isinstance(item, str) or not item for item in blockers_raw
        ):
            raise ValueError(f"variant {name} physical binding blockers must be strings")
        if (status == "blocked") != bool(blockers_raw):
            raise ValueError(
                f"variant {name} blocked physical binding must have blockers and "
                "ready binding must not"
            )
        assert dependency.release is not None
        binding_kind = binding.get("kind")
        common_fields = {
            "dependency",
            "transaction_module",
            "adapter_module",
            "status",
            "blockers",
        }
        if binding_kind == "oa-native":
            if set(binding) != common_fields | {"kind"}:
                raise ValueError(
                    f"variant {name} native OA physical binding fields are invalid"
                )
            physical_binding = OaNativePhysicalBinding(
                kind="oa-native",
                dependency=dependency_name,
                transaction_module=_string(
                    binding.get("transaction_module"),
                    f"variant {name}.physical_binding.transaction_module",
                ),
                adapter_module=_string(
                    binding.get("adapter_module"),
                    f"variant {name}.physical_binding.adapter_module",
                ),
                status=status,
                blockers=tuple(blockers_raw),
            )
        elif binding_kind == "oa-mixed-signal":
            mixed_signal_fields = common_fields | {
                "physical_shell_module",
                "raw_macro_module",
            }
            allowed_fields = mixed_signal_fields | {"kind"}
            if set(binding) != allowed_fields:
                raise ValueError(
                    f"variant {name} mixed-signal physical binding fields are invalid"
                )
            physical_binding = OaMixedSignalPhysicalBinding(
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
        else:
            raise ValueError(
                f"variant {name} physical binding kind is unsupported: "
                f"{binding_kind!r}"
            )

    return IpOperatingVariant(
        name=name,
        path=path,
        default_fileset=default_fileset,
        filesets=MappingProxyType(filesets),
        physical_binding=physical_binding,
        source_document=freeze_toml_document(raw),
    )


def _integration_dependencies(
    component: ComponentContract,
) -> tuple[IpIntegrationDependency, ...]:
    dependency_rows = component.document.get("component", ())
    if not isinstance(dependency_rows, (list, tuple)):
        raise ValueError("IP integration component dependencies must be an array")
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
                        release_value,
                        f"component[{index}].release",
                    )
                ),
            )
        )
    return tuple(dependencies)


def load_ip_integration_contract(
    path: Path,
    *,
    project: Project,
    variant_source_documents: Mapping[Path, Mapping[str, Any]] | None = None,
) -> IpIntegrationContract:
    """Load composite-IP integration intent from its canonical component manifest."""

    from sigilicon.project import Project

    repository = project
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
    raw = component.document
    graph = load_component_graph(
        component.path,
        project_root=root,
        root_contract=component,
        contract_inventory=repository.component_inventory,
    )

    dependencies = _integration_dependencies(component)
    by_name = {item.name: item for item in dependencies}

    lock_value = raw.get("dependency_lock")
    dependency_lock: PurePosixPath | None = None
    if lock_value is not None:
        _, dependency_lock = repository.resolve_owner_file(
            cataloged_owner,
            lock_value,
            "dependency_lock",
        )

    variants_raw = _table(raw.get("variants"), "variants")
    if (
        variant_source_documents is not None
        and not isinstance(variant_source_documents, _MAPPING_PROXY_TYPE)
    ):
        raise ValueError("IP operating variant source inventory must be immutable")
    variants: list[IpOperatingVariant] = []
    source_documents: dict[Path, Mapping[str, Any]] = {}
    for name, value in variants_raw.items():
        name = _string(name, "variant name")
        variant_path, _ = repository.resolve_owner_file(
            cataloged_owner,
            value,
            f"variants.{name}",
        )
        variant_document = None
        if variant_source_documents is not None:
            variant_document = variant_source_documents.get(variant_path)
            if (
                not isinstance(variant_document, Mapping)
                or not is_frozen_toml_document(variant_document)
            ):
                raise ValueError("IP operating variant source inventory is incomplete")
        variant = _operating_variant(
            name,
            variant_path,
            contract=component,
            graph=graph,
            dependencies=by_name,
            source_document=variant_document,
        )
        variants.append(variant)
        source_documents[variant.path] = variant.source_document
    if not variants:
        raise ValueError("IP integration must declare at least one operating variant")
    if (
        variant_source_documents is not None
        and set(variant_source_documents) != {variant.path for variant in variants}
    ):
        raise ValueError("IP operating variant source inventory identity drift")

    implementation_profiles, implementation_documents = _implementation_profiles(
        raw,
        project=repository,
        owner=component.owner,
    )
    source_documents.update(implementation_documents)
    return IpIntegrationContract(
        project=repository,
        component=component,
        component_graph=MappingProxyType(dict(graph)),
        dependency_lock=dependency_lock,
        dependencies=tuple(dependencies),
        implementation_profiles=implementation_profiles,
        variants=tuple(variants),
        source_documents=MappingProxyType(source_documents),
    )


def resolve_ip_integration_contract(
    path: Path,
    *,
    project: Project,
    snapshot: IpIntegrationContract | None = None,
) -> IpIntegrationContract:
    """Load an integration contract or validate one operation-owned snapshot."""

    if snapshot is None:
        return load_ip_integration_contract(path, project=project)
    contract_path = path.resolve()
    root = project.project_root
    if (
        snapshot.path != contract_path
        or snapshot.project is not project
        or not contract_path.is_relative_to(root)
        or not contract_path.is_file()
    ):
        raise ValueError("IP integration snapshot identity drift")
    owner = project.require_owner(contract_path)
    component = resolve_component_contract(
        contract_path,
        project_root=root,
        snapshot=snapshot.component,
    )
    if component.owner != owner.name or component.kind != "composite-ip":
        raise ValueError("IP integration snapshot owner drift")
    graph = resolve_component_graph(
        contract_path,
        project_root=root,
        snapshot=snapshot.component_graph,
    )
    if (
        not isinstance(snapshot.implementation_profiles, _MAPPING_PROXY_TYPE)
        or not isinstance(snapshot.source_documents, _MAPPING_PROXY_TYPE)
    ):
        raise ValueError("IP integration snapshot mappings are mutable")
    for variant in snapshot.variants:
        if (
            not isinstance(variant.filesets, _MAPPING_PROXY_TYPE)
            or not isinstance(variant.source_document, Mapping)
            or not is_frozen_toml_document(variant.source_document)
            or any(
                not isinstance(fileset.dependency_roles, _MAPPING_PROXY_TYPE)
                or not isinstance(fileset.source_filesets, _MAPPING_PROXY_TYPE)
                for fileset in variant.filesets.values()
            )
        ):
            raise ValueError("IP operating variant snapshot is mutable")
    if any(
        not isinstance(source, Path)
        or source != source.resolve()
        or not source.is_relative_to(owner.root)
        or not source.is_file()
        or not isinstance(document, Mapping)
        or not is_frozen_toml_document(document)
        for source, document in snapshot.source_documents.items()
    ):
        raise ValueError("IP integration source snapshot identity drift")

    dependencies = _integration_dependencies(component)
    by_name = {item.name: item for item in dependencies}
    raw = component.document
    lock_value = raw.get("dependency_lock")
    dependency_lock = (
        None
        if lock_value is None
        else project.resolve_owner_file(
            owner,
            lock_value,
            "dependency_lock",
        )[1]
    )
    variants_raw = _table(raw.get("variants"), "variants")
    variants: list[IpOperatingVariant] = []
    expected_paths: set[Path] = set()
    for name, value in variants_raw.items():
        name = _string(name, "variant name")
        variant_path, _ = project.resolve_owner_file(
            owner,
            value,
            f"variants.{name}",
        )
        expected_paths.add(variant_path)
        document = snapshot.source_documents.get(variant_path)
        if not isinstance(document, Mapping):
            raise ValueError("IP integration source snapshot is incomplete")
        variants.append(
            _operating_variant(
                name,
                variant_path,
                contract=component,
                graph=graph,
                dependencies=by_name,
                source_document=document,
            )
        )
    implementation_profiles, implementation_documents = _implementation_profiles(
        raw,
        project=project,
        owner=component.owner,
        source_documents=snapshot.source_documents,
    )
    expected_paths.update(implementation_documents)
    if set(snapshot.source_documents) != expected_paths:
        raise ValueError("IP integration source snapshot identity drift")
    expected_documents = {
        variant.path: variant.source_document for variant in variants
    }
    expected_documents.update(implementation_documents)
    parsed = IpIntegrationContract(
        project=project,
        component=component,
        component_graph=MappingProxyType(dict(graph)),
        dependency_lock=dependency_lock,
        dependencies=dependencies,
        implementation_profiles=implementation_profiles,
        variants=tuple(variants),
        source_documents=MappingProxyType(expected_documents),
    )
    if parsed != snapshot:
        raise ValueError("IP integration snapshot typed contract drift")
    return snapshot


def load_ip_dependency_lock(
    path: Path, *, contract: IpIntegrationContract
) -> IpDependencyLock:
    lock_path = path.resolve()
    try:
        relative = lock_path.relative_to(contract.project_root).as_posix()
    except ValueError as exc:
        raise ValueError(
            "IP dependency lock must stay inside its owner root"
        ) from exc
    lock_path, _ = contract.project.resolve_owner_file(
        contract.owner,
        relative,
        "IP dependency lock",
    )
    with lock_path.open("rb") as stream:
        raw: dict[str, Any] = tomllib.load(stream)
    require_config_header(
        raw,
        lock_path,
        contract_kind="ip-dependency-lock",
        path_scope="owner",
        owner=contract.owner,
        schema=2,
    )
    if raw.get("ip") != contract.name:
        raise ValueError("IP dependency lock identity does not match its component")
    entries = raw.get("dependency")
    if not isinstance(entries, list):
        raise ValueError("IP dependency lock must declare dependency entries")
    locked: list[LockedIpRelease] = []
    for index, value in enumerate(entries):
        locked.append(parse_locked_ip_release(value, f"lock.dependency[{index}]"))
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
