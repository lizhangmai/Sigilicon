"""Typed immutable record produced by IP release planning."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from types import MappingProxyType
from typing import Literal, Mapping, TypeAlias

from sigilicon.domain.ip_release import IpContract


@dataclass(frozen=True)
class RequiredRolesCheck:
    export: str
    passed: bool
    required: tuple[str, ...]
    present: tuple[str, ...]

    @property
    def record(self) -> dict[str, object]:
        return {
            "name": f"required_release_roles:{self.export}",
            "export": self.export,
            "passed": self.passed,
            "required": list(self.required),
            "present": list(self.present),
        }


@dataclass(frozen=True)
class InterfaceConsistencyCheck:
    export: str
    interface_kind: str | None = None
    module: str | None = None
    variant: str | None = None
    port_count: int | None = None
    physical_module: str | None = None
    physical_port_count: int | None = None
    transaction_module: str | None = None
    transaction_port_count: int | None = None
    transaction_signature_checked: bool | None = None
    physical_shell_module: str | None = None
    physical_named_bindings_checked: int | None = None
    oa_library: str | None = None
    oa_cell: str | None = None
    native_oa_port_contract_checked: bool | None = None

    @property
    def record(self) -> dict[str, object]:
        details = {
            name: value
            for name, value in (
                ("interface_kind", self.interface_kind),
                ("module", self.module),
                ("variant", self.variant),
                ("port_count", self.port_count),
                ("physical_module", self.physical_module),
                ("physical_port_count", self.physical_port_count),
                ("transaction_module", self.transaction_module),
                ("transaction_port_count", self.transaction_port_count),
                (
                    "transaction_signature_checked",
                    self.transaction_signature_checked,
                ),
                ("physical_shell_module", self.physical_shell_module),
                (
                    "physical_named_bindings_checked",
                    self.physical_named_bindings_checked,
                ),
                ("oa_library", self.oa_library),
                ("oa_cell", self.oa_cell),
                (
                    "native_oa_port_contract_checked",
                    self.native_oa_port_contract_checked,
                ),
            )
            if value is not None
        }
        return {
            "name": f"development_interface_consistency:{self.export}",
            "export": self.export,
            "passed": True,
            **details,
        }


@dataclass(frozen=True)
class QualifiedViewSemanticsCheck:
    passed: bool
    problems: tuple[str, ...]

    @property
    def record(self) -> dict[str, object]:
        return {
            "name": "qualified_view_semantics",
            "passed": self.passed,
            "problems": list(self.problems),
        }


ReleaseCheck: TypeAlias = (
    RequiredRolesCheck
    | InterfaceConsistencyCheck
    | QualifiedViewSemanticsCheck
)


@dataclass(frozen=True)
class ReleaseAvailability:
    simulation: bool
    synthesis: bool
    physical_implementation: bool

    @property
    def record(self) -> dict[str, bool]:
        return {
            "simulation": self.simulation,
            "synthesis": self.synthesis,
            "physical_implementation": self.physical_implementation,
        }


@dataclass(frozen=True)
class ReleaseComponentRecord:
    name: str
    kind: str
    lifecycle: str
    contract: str

    @property
    def record(self) -> dict[str, str]:
        return {
            "name": self.name,
            "kind": self.kind,
            "lifecycle": self.lifecycle,
            "contract": self.contract,
        }


@dataclass(frozen=True)
class ReleaseOaIdentity:
    library: str
    cell: str
    schematic_view: str
    layout_view: str

    @property
    def record(self) -> dict[str, str]:
        return {
            "library": self.library,
            "cell": self.cell,
            "schematic_view": self.schematic_view,
            "layout_view": self.layout_view,
        }


@dataclass(frozen=True)
class RtlReleaseInterface:
    contract: str
    module: str
    source_role: str
    variant: str | None = None
    kind: Literal["rtl"] = field(default="rtl", init=False)

    @property
    def record(self) -> dict[str, object]:
        value: dict[str, object] = {
            "kind": self.kind,
            "contract": self.contract,
            "module": self.module,
            "source_role": self.source_role,
        }
        if self.variant is not None:
            value["variant"] = self.variant
        return value


@dataclass(frozen=True)
class MixedSignalReleaseInterface:
    contract: str
    physical: str
    logical: str
    interfaces_are_distinct: bool
    kind: Literal["oa-mixed-signal"] = field(
        default="oa-mixed-signal",
        init=False,
    )

    @property
    def record(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "contract": self.contract,
            "physical": self.physical,
            "logical": self.logical,
            "interfaces_are_distinct": self.interfaces_are_distinct,
        }


@dataclass(frozen=True)
class NativeOaReleaseInterface:
    contract: str
    kind: Literal["oa-native"] = field(default="oa-native", init=False)

    @property
    def record(self) -> dict[str, str]:
        return {"kind": self.kind, "contract": self.contract}


ReleaseInterface: TypeAlias = (
    RtlReleaseInterface
    | MixedSignalReleaseInterface
    | NativeOaReleaseInterface
)


@dataclass(frozen=True)
class ReleaseExportRecord:
    name: str
    interface: ReleaseInterface
    maturity_required_roles: tuple[str, ...]
    maturity_missing_items: tuple[str, ...]
    availability: ReleaseAvailability
    oa: ReleaseOaIdentity | None = None

    @property
    def record(self) -> dict[str, object]:
        value: dict[str, object] = {
            "name": self.name,
            "interface": self.interface.record,
            "maturity": {
                "required_roles": list(self.maturity_required_roles),
                "missing_items": list(self.maturity_missing_items),
            },
            "availability": self.availability.record,
        }
        if self.oa is not None:
            value["oa"] = self.oa.record
        return value


@dataclass(frozen=True)
class NativeBundleMetadata:
    subcircuits: tuple[str, ...]
    primitive_masters: tuple[str, ...]
    sha256: str
    composition: Literal["reachable-spectre-hierarchy"] = field(
        default="reachable-spectre-hierarchy",
        init=False,
    )


@dataclass(frozen=True)
class ReleaseCollateralRecord:
    export: str
    role: str
    component: str
    source_id: str
    source: str
    package_path: str
    format: str
    module: str | None
    library: str | None
    cell: str | None
    view: str | None
    corner: str | None
    capabilities: tuple[str, ...]
    source_size: int
    source_sha256: str
    native_bundle: NativeBundleMetadata | None = None

    @property
    def record(self) -> dict[str, object]:
        value: dict[str, object] = {
            "export": self.export,
            "role": self.role,
            "component": self.component,
            "source_id": self.source_id,
            "source": self.source,
            "package_path": self.package_path,
            "format": self.format,
            "module": self.module,
            "library": self.library,
            "cell": self.cell,
            "view": self.view,
            "corner": self.corner,
            "capabilities": list(self.capabilities),
            "source_size": self.source_size,
            "source_sha256": self.source_sha256,
        }
        if self.native_bundle is not None:
            value.update(
                {
                    "composition": self.native_bundle.composition,
                    "subcircuits": list(self.native_bundle.subcircuits),
                    "primitive_masters": list(
                        self.native_bundle.primitive_masters
                    ),
                    "sha256": self.native_bundle.sha256,
                }
            )
        return value


@dataclass(frozen=True)
class IpReleaseRecord:
    ip_name: str
    owner: str
    contract: str
    producer: str
    component: ReleaseComponentRecord
    release_id: str
    release_store: str
    source_commit: str
    working_tree_dirty: bool
    source_files: tuple[str, ...]
    maturity_level: str
    maturity_checks: tuple[ReleaseCheck, ...]
    missing_items: tuple[str, ...]
    availability: ReleaseAvailability
    exports: tuple[ReleaseExportRecord, ...]
    collateral: tuple[ReleaseCollateralRecord, ...]

    def __post_init__(self) -> None:
        export_names = {exported.name for exported in self.exports}
        if len(export_names) != len(self.exports):
            raise ValueError("release export identities contain duplicates")
        roles = {(item.export, item.role) for item in self.collateral}
        if len(roles) != len(self.collateral):
            raise ValueError("release collateral roles contain duplicates")
        if any(item.export not in export_names for item in self.collateral):
            raise ValueError("release collateral refers to an unknown export")

    @property
    def record(self) -> dict[str, object]:
        return {
            "ip_name": self.ip_name,
            "owner": self.owner,
            "contract": self.contract,
            "producer": self.producer,
            "component": self.component.record,
            "release_id": self.release_id,
            "release_store": self.release_store,
            "source_commit": self.source_commit,
            "working_tree_dirty": self.working_tree_dirty,
            "source_files": list(self.source_files),
            "maturity_level": self.maturity_level,
            "maturity_checks": [check.record for check in self.maturity_checks],
            "missing_items": list(self.missing_items),
            "availability": self.availability.record,
            "exports": [exported.record for exported in self.exports],
            "collateral": [item.record for item in self.collateral],
        }


class IpReleaseError(RuntimeError):
    """The requested release is not backed by accepted immutable evidence."""


@dataclass(frozen=True)
class IpReleasePlan:
    """Immutable typed publication plan and generated native collateral."""

    contract: IpContract
    payload: IpReleaseRecord
    native_bundles: Mapping[tuple[str, str], str]

    def __post_init__(self) -> None:
        expected_contract = self.contract.path.relative_to(
            self.contract.project_root
        ).as_posix()
        if (
            self.payload.ip_name != self.contract.name
            or self.payload.owner != self.contract.owner
            or self.payload.contract != expected_contract
            or self.payload.producer != self.contract.producer.as_posix()
        ):
            raise ValueError(
                "release plan payload identity disagrees with its contract"
            )
        bundles = MappingProxyType(dict(self.native_bundles))
        expected_bundles = {
            (item.export, item.role): item.native_bundle
            for item in self.payload.collateral
            if item.native_bundle is not None
        }
        if set(bundles) != set(expected_bundles) or any(
            hashlib.sha256(bundles[key].encode("utf-8")).hexdigest()
            != metadata.sha256
            for key, metadata in expected_bundles.items()
        ):
            raise ValueError("release plan native bundle identity is inconsistent")
        object.__setattr__(self, "native_bundles", bundles)

    @property
    def release_id(self) -> str:
        return self.payload.release_id

    @property
    def store(self) -> str:
        return self.payload.release_store

    @property
    def source_commit(self) -> str:
        return self.payload.source_commit

    @property
    def source_files(self) -> tuple[str, ...]:
        return self.payload.source_files

    @property
    def maturity(self) -> str:
        return self.payload.maturity_level

    @property
    def missing_items(self) -> tuple[str, ...]:
        return self.payload.missing_items

    @property
    def working_tree_dirty(self) -> bool:
        return self.payload.working_tree_dirty

    @property
    def record(self) -> dict[str, object]:
        """Return a detached portable projection for CLI and manifests."""

        return self.payload.record

    @property
    def identity(self) -> str:
        encoded = json.dumps(
            self.record,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
