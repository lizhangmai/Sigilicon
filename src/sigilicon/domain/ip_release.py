"""Declarative custom-IP release contracts and maturity rules."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import tomllib
from typing import Any, Iterable, Mapping

from sigilicon.domain.config_contracts import require_config_header

RELEASE_MATURITY_LEVELS = ("development", "implementation", "signoff")


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _source_reference(
    value: str,
    *,
    owner: Path,
    project_root: Path,
) -> tuple[Path, str | None] | None:
    """Resolve one repository-owned file reference without retaining its locator."""

    candidates = [(value, None)]
    prefix, separator, selector = value.partition(":")
    if separator:
        candidates.append((prefix, selector))
    for text, selected in candidates:
        relative = Path(text)
        if not text or relative.is_absolute():
            continue
        for base in (owner.parent, project_root):
            target = (base / relative).resolve()
            if target.is_relative_to(project_root) and target.is_file():
                return target, selected
    return None


def _semantic_source_sha256(
    path: Path,
    *,
    project_root: Path,
    memo: dict[Path, str],
    active: set[Path],
) -> str:
    source = path.resolve()
    if source in memo:
        return memo[source]
    if source in active:
        return _digest({"recursive_source_reference": True})
    if not source.is_relative_to(project_root) or not source.is_file():
        raise ValueError("semantic source must be a repository-owned file")

    active.add(source)
    try:
        if source.suffix == ".toml":
            with source.open("rb") as stream:
                document: object = tomllib.load(stream)
            kind = "toml"
        elif source.suffix == ".json":
            document = json.loads(source.read_text(encoding="utf-8"))
            kind = "json"
        else:
            result = _digest(
                {
                    "kind": source.suffix.lower() or "text",
                    "content_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                }
            )
            memo[source] = result
            return result

        def normalize(value: object) -> object:
            if isinstance(value, Mapping):
                return {str(key): normalize(item) for key, item in value.items()}
            if isinstance(value, list):
                return [normalize(item) for item in value]
            if isinstance(value, str):
                reference = _source_reference(
                    value,
                    owner=source,
                    project_root=project_root,
                )
                if reference is None:
                    return value
                target, selector = reference
                return {
                    "referenced_source_sha256": _semantic_source_sha256(
                        target,
                        project_root=project_root,
                        memo=memo,
                        active=active,
                    ),
                    "selector": selector,
                }
            return value

        result = _digest({"kind": kind, "document": normalize(document)})
        memo[source] = result
        return result
    finally:
        active.remove(source)


def semantic_source_sha256(path: Path, *, project_root: Path) -> str:
    """Hash source semantics while replacing repository file locators by content.

    Exact paths remain provenance, but moving a source and updating a TOML/JSON
    reference to the same content does not alter this identity.
    """

    root = project_root.resolve()
    return _semantic_source_sha256(
        path,
        project_root=root,
        memo={},
        active=set(),
    )


@dataclass(frozen=True)
class ReleaseFingerprintSource:
    """One source bound to a stable release-semantic role."""

    logical_role: str
    source: Path


def release_source_fingerprint(
    *,
    attributes: Mapping[str, object],
    sources: Iterable[ReleaseFingerprintSource],
    project_root: Path,
) -> str:
    """Fingerprint release meaning and content without hashing source locators."""

    root = project_root.resolve()
    rows: list[dict[str, str]] = []
    roles: set[str] = set()
    memo: dict[Path, str] = {}
    for item in sources:
        if not item.logical_role or item.logical_role in roles:
            raise ValueError("release fingerprint source roles must be unique")
        roles.add(item.logical_role)
        rows.append(
            {
                "logical_role": item.logical_role,
                "semantic_sha256": _semantic_source_sha256(
                    item.source,
                    project_root=root,
                    memo=memo,
                    active=set(),
                ),
            }
        )
    return _digest(
        {
            "schema": 1,
            "attributes": dict(attributes),
            "sources": sorted(rows, key=lambda row: row["logical_role"]),
        }
    )


def _table(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a TOML table")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def safe_relative(value: object, label: str) -> PurePosixPath:
    text = _string(value, label)
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or "\\" in text
        or path.as_posix() != text
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{label} must be a safe project-relative path: {text!r}")
    return path


@dataclass(frozen=True)
class IpCollateral:
    export: str
    role: str
    component: str
    fileset: str
    source: PurePosixPath
    package_path: PurePosixPath
    format: str
    module: str | None
    library: str | None
    cell: str | None
    view: str | None
    corner: str | None
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class IpExport:
    name: str
    oa_library: str
    oa_cell: str
    schematic_view: str
    layout_view: str
    interface_contract: PurePosixPath
    physical_interface: str
    logical_interface: str
    collateral: tuple[IpCollateral, ...]
    required_roles: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class IpContract:
    path: Path
    project_root: Path
    name: str
    producer: PurePosixPath
    component_contract: PurePosixPath
    default_maturity: str
    exports: tuple[IpExport, ...]
    source_files: tuple[PurePosixPath, ...]
    oa_assembly: PurePosixPath

    @property
    def collateral(self) -> tuple[IpCollateral, ...]:
        return tuple(item for exported in self.exports for item in exported.collateral)

    def get_export(self, name: str) -> IpExport:
        matches = [item for item in self.exports if item.name == name]
        if len(matches) != 1:
            raise KeyError(f"unknown IP export: {name}")
        return matches[0]

    def require_level(self, level: str) -> str:
        if level not in RELEASE_MATURITY_LEVELS:
            raise ValueError(
                f"unsupported release maturity {level!r}; expected one of "
                f"{', '.join(RELEASE_MATURITY_LEVELS)}"
            )
        return level


@dataclass(frozen=True)
class PromotionEvidence:
    """One owner-selected policy conclusion from an immutable Flow Run."""

    node_id: str
    role: str
    policy: str
    evidence_role: str
    evaluation: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.node_id, "promotion evidence node"),
            (self.role, "promotion evidence role"),
            (self.policy, "promotion evidence policy"),
        ):
            if not value or any(character.isspace() for character in value):
                raise ValueError(f"{label} must be a portable identity")
        if self.evidence_role not in {
            "diagnostic",
            "regression",
            "readiness",
            "qualification",
            "signoff",
        }:
            raise ValueError("promotion evidence_role is unsupported")
        if self.evaluation not in {"producer", "run-policy"}:
            raise ValueError("promotion evidence evaluation is unsupported")


@dataclass(frozen=True)
class PromotionBoundary:
    """An explicit coverage boundary that limits a promoted conclusion."""

    kind: str
    status: str
    summary: str
    subjects: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.kind or any(character.isspace() for character in self.kind):
            raise ValueError("promotion boundary kind must be a portable identity")
        if self.status not in {"open", "closed"}:
            raise ValueError("promotion boundary status must be open or closed")
        if not self.summary:
            raise ValueError("promotion boundary summary must be non-empty")
        if not self.subjects or any(not item for item in self.subjects):
            raise ValueError("promotion boundary subjects must be non-empty")
        if len(self.subjects) != len(set(self.subjects)):
            raise ValueError("promotion boundary subjects must be unique")


@dataclass(frozen=True)
class IpPromotionContract:
    """Owner request to promote exact run artifacts without executing tools."""

    path: Path
    project_root: Path
    owner: str
    name: str
    producer: PurePosixPath
    component_contract: PurePosixPath
    export: str
    maturity: str
    source_commit: str
    source_dirty: bool
    interface_contract: PurePosixPath
    logical_interface: str
    physical_interface: str
    conclusions: Mapping[str, bool]
    artifacts: tuple[Mapping[str, Any], ...]
    evidence: tuple[PromotionEvidence, ...]
    boundaries: tuple[PromotionBoundary, ...]


def load_ip_promotion_contract(
    path: Path,
    *,
    project_root: Path,
) -> IpPromotionContract:
    """Load the sole supported Flow-backed IP promotion source schema."""

    root = project_root.resolve()
    contract_path = path.resolve()
    if not contract_path.is_relative_to(root):
        raise ValueError("IP promotion contract must be inside the project root")
    with contract_path.open("rb") as stream:
        raw: dict[str, Any] = tomllib.load(stream)
    header = require_config_header(
        raw,
        contract_path,
        contract_kind="ip-promotion",
        path_scope="owner",
    )
    expected_fields = {
        "schema",
        "contract_kind",
        "path_scope",
        "owner",
        "name",
        "producer",
        "component",
        "export",
        "maturity",
        "source",
        "interface",
        "conclusions",
        "artifacts",
        "evidence",
        "boundaries",
    }
    if set(raw) != expected_fields:
        raise ValueError("IP promotion fields do not match the current schema")
    producer = safe_relative(raw.get("producer"), "producer")
    producer_path = (root / producer).resolve()
    if (
        not producer_path.is_dir()
        or not producer_path.is_relative_to(root)
        or not contract_path.is_relative_to(producer_path)
    ):
        raise ValueError("IP promotion contract escaped its declared producer")

    component_contract = safe_relative(raw.get("component"), "component")
    component_path = (producer_path / component_contract).resolve()
    if not component_path.is_file() or not component_path.is_relative_to(producer_path):
        raise FileNotFoundError("IP promotion component contract is missing")
    from sigilicon.domain.component import load_component_graph

    component_graph = load_component_graph(component_path, project_root=root)
    name = _string(raw.get("name"), "name")
    component = component_graph.get(name)
    if component is None or component.path != component_path:
        raise ValueError("IP promotion name must match its component identity")

    maturity = _string(raw.get("maturity"), "maturity")
    if maturity not in RELEASE_MATURITY_LEVELS:
        raise ValueError("IP promotion maturity is unsupported")
    source = _table(raw.get("source"), "source")
    if set(source) != {"commit", "dirty"}:
        raise ValueError("promotion source fields do not match the current schema")
    source_commit = _string(source.get("commit"), "source.commit")
    if (
        len(source_commit) not in {40, 64}
        or any(character not in "0123456789abcdef" for character in source_commit)
    ):
        raise ValueError("source.commit must be a Git commit identity")
    source_dirty = source.get("dirty")
    if not isinstance(source_dirty, bool):
        raise ValueError("source.dirty must be boolean")

    interface = _table(raw.get("interface"), "interface")
    if set(interface) != {"contract", "logical", "physical"}:
        raise ValueError("promotion interface fields do not match the current schema")
    interface_contract = safe_relative(
        interface.get("contract"), "interface.contract"
    )
    interface_path = (producer_path / interface_contract).resolve()
    if not interface_path.is_file() or not interface_path.is_relative_to(producer_path):
        raise FileNotFoundError("IP promotion interface contract is missing")

    conclusion_raw = _table(raw.get("conclusions"), "conclusions")
    conclusion_fields = {
        "implementation_regression",
        "physical_completion_readiness",
        "qualification",
        "signoff",
    }
    if set(conclusion_raw) != conclusion_fields or any(
        not isinstance(conclusion_raw[field], bool) for field in conclusion_fields
    ):
        raise ValueError("promotion conclusions do not match the current schema")

    artifact_raw = raw.get("artifacts")
    if not isinstance(artifact_raw, list) or not artifact_raw:
        raise ValueError("IP promotion must select at least one Run Artifact")
    artifacts = tuple(
        dict(_table(value, f"artifacts[{index}]"))
        for index, value in enumerate(artifact_raw)
    )
    if any(reference.get("owner") != header.owner for reference in artifacts):
        raise ValueError("IP promotion Run Artifact owner differs from its owner")
    run_identities = {
        (reference.get("flow"), reference.get("run_id")) for reference in artifacts
    }
    if len(run_identities) != 1:
        raise ValueError("IP promotion artifacts must select one exact Flow Run")

    evidence_raw = raw.get("evidence")
    if not isinstance(evidence_raw, list) or not evidence_raw:
        raise ValueError("IP promotion must select policy evidence")
    evidence = tuple(
        PromotionEvidence(
            node_id=_string(
                _table(value, f"evidence[{index}]").get("node"),
                f"evidence[{index}].node",
            ),
            role=_string(value.get("role"), f"evidence[{index}].role"),
            policy=_string(value.get("policy"), f"evidence[{index}].policy"),
            evidence_role=_string(
                value.get("evidence_role"), f"evidence[{index}].evidence_role"
            ),
            evaluation=_string(
                value.get("evaluation"), f"evidence[{index}].evaluation"
            ),
        )
        for index, value in enumerate(evidence_raw)
    )
    if any(
        set(_table(value, f"evidence[{index}]"))
        != {"node", "role", "policy", "evidence_role", "evaluation"}
        for index, value in enumerate(evidence_raw)
    ):
        raise ValueError("promotion evidence fields do not match the current schema")
    if len({item.role for item in evidence}) != len(evidence):
        raise ValueError("IP promotion evidence roles must be unique")

    boundaries_raw = raw.get("boundaries", [])
    if not isinstance(boundaries_raw, list):
        raise ValueError("IP promotion boundaries must be an array")
    boundaries: list[PromotionBoundary] = []
    for index, value in enumerate(boundaries_raw):
        item = _table(value, f"boundaries[{index}]")
        if set(item) != {"kind", "status", "summary", "subjects"}:
            raise ValueError("promotion boundary fields do not match the current schema")
        subjects = item.get("subjects")
        if not isinstance(subjects, list) or any(
            not isinstance(subject, str) for subject in subjects
        ):
            raise ValueError(f"boundaries[{index}].subjects must be strings")
        boundaries.append(
            PromotionBoundary(
                kind=_string(item.get("kind"), f"boundaries[{index}].kind"),
                status=_string(item.get("status"), f"boundaries[{index}].status"),
                summary=_string(
                    item.get("summary"), f"boundaries[{index}].summary"
                ),
                subjects=tuple(subjects),
            )
        )

    return IpPromotionContract(
        path=contract_path,
        project_root=root,
        owner=header.owner,
        name=name,
        producer=producer,
        component_contract=component_contract,
        export=_string(raw.get("export"), "export"),
        maturity=maturity,
        source_commit=source_commit,
        source_dirty=source_dirty,
        interface_contract=interface_contract,
        logical_interface=_string(interface.get("logical"), "interface.logical"),
        physical_interface=_string(interface.get("physical"), "interface.physical"),
        conclusions={field: bool(conclusion_raw[field]) for field in conclusion_fields},
        artifacts=artifacts,
        evidence=evidence,
        boundaries=tuple(boundaries),
    )


def load_ip_contract(path: Path, *, project_root: Path) -> IpContract:
    root = project_root.resolve()
    contract_path = path.resolve()
    if not contract_path.is_relative_to(root):
        raise ValueError("IP contract must be inside the project root")
    with contract_path.open("rb") as stream:
        raw: dict[str, Any] = tomllib.load(stream)
    producer = safe_relative(raw.get("producer"), "producer")
    require_config_header(
        raw,
        contract_path,
        contract_kind="ip-release",
        path_scope="owner",
    )
    source = _table(raw.get("source"), "source")

    component_contract = safe_relative(raw.get("component"), "component")
    producer_path = (root / producer).resolve()
    if (
        not producer_path.is_dir()
        or not producer_path.is_relative_to(root)
        or not contract_path.is_relative_to(producer_path)
    ):
        raise ValueError("IP release contract must stay inside its declared producer")
    component_path = (producer_path / component_contract).resolve()
    if not component_path.is_file() or not component_path.is_relative_to(producer_path):
        raise FileNotFoundError("IP component contract is missing or outside its owner")
    from sigilicon.domain.component import load_component_graph, resolve_component_fileset

    component_graph = load_component_graph(component_path, project_root=root)
    ip_name = _string(raw.get("name"), "name")
    component = component_graph.get(ip_name)
    if component is None or component.path != component_path:
        raise ValueError(
            "IP release name must match its producer component identity"
        )

    exports_raw = raw.get("exports")
    if not isinstance(exports_raw, list) or not exports_raw:
        raise ValueError("IP contract must declare at least one exports entry")
    export_specs: dict[str, dict[str, object]] = {}
    for index, value in enumerate(exports_raw):
        entry = _table(value, f"exports[{index}]")
        name = _string(entry.get("name"), f"exports[{index}].name")
        if name in export_specs:
            raise ValueError(f"duplicate IP export: {name}")
        oa = _table(entry.get("oa"), f"exports[{index}].oa")
        interface = _table(
            entry.get("interface"), f"exports[{index}].interface"
        )
        maturity = _table(
            entry.get("maturity"), f"exports[{index}].maturity"
        )
        required_roles: dict[str, tuple[str, ...]] = {}
        previous: set[str] = set()
        for level in RELEASE_MATURITY_LEVELS:
            level_table = _table(
                maturity.get(level),
                f"exports[{index}].maturity.{level}",
            )
            values = level_table.get("required_roles")
            if not isinstance(values, list) or any(
                not isinstance(item, str) or not item for item in values
            ):
                raise ValueError(
                    f"exports[{index}].maturity.{level}.required_roles "
                    "must be strings"
                )
            current = set(values)
            if len(current) != len(values):
                raise ValueError(
                    f"exports[{index}].maturity.{level}.required_roles "
                    "must be unique"
                )
            if not previous.issubset(current):
                raise ValueError(
                    f"exports[{index}].maturity.{level} must include "
                    "lower-level roles"
                )
            required_roles[level] = tuple(values)
            previous = current
        export_specs[name] = {
            "oa_library": _string(
                oa.get("library"), f"exports[{index}].oa.library"
            ),
            "oa_cell": _string(oa.get("cell"), f"exports[{index}].oa.cell"),
            "schematic_view": _string(
                oa.get("schematic_view"),
                f"exports[{index}].oa.schematic_view",
            ),
            "layout_view": _string(
                oa.get("layout_view"), f"exports[{index}].oa.layout_view"
            ),
            "interface_contract": safe_relative(
                interface.get("contract"),
                f"exports[{index}].interface.contract",
            ),
            "physical_interface": _string(
                interface.get("physical"),
                f"exports[{index}].interface.physical",
            ),
            "logical_interface": _string(
                interface.get("logical"),
                f"exports[{index}].interface.logical",
            ),
            "required_roles": required_roles,
        }

    collateral_raw = raw.get("collateral")
    if not isinstance(collateral_raw, list) or not collateral_raw:
        raise ValueError("IP contract must declare at least one collateral entry")
    roles: set[tuple[str, str]] = set()
    package_paths: set[PurePosixPath] = set()
    collateral_by_export: dict[str, list[IpCollateral]] = {
        name: [] for name in export_specs
    }
    for index, item in enumerate(collateral_raw):
        entry = _table(item, f"collateral[{index}]")
        export = _string(entry.get("export"), f"collateral[{index}].export")
        if export not in export_specs:
            raise ValueError(
                f"collateral[{index}] names unknown IP export: {export}"
            )
        role = _string(entry.get("role"), f"collateral[{index}].role")
        component = _string(
            entry.get("component"), f"collateral[{index}].component"
        )
        fileset = _string(entry.get("fileset"), f"collateral[{index}].fileset")
        sources = resolve_component_fileset(component_graph, component, fileset)
        if len(sources) != 1:
            raise ValueError(
                f"collateral[{index}] fileset must resolve to exactly one file"
            )
        package_path = safe_relative(
            entry.get("package_path"), f"collateral[{index}].package_path"
        )
        if package_path.parts[:2] != ("exports", export):
            raise ValueError(
                f"collateral[{index}].package_path must stay below "
                f"exports/{export}/"
            )
        capabilities = entry.get("capabilities", [])
        if not isinstance(capabilities, list) or any(
            not isinstance(value, str) or not value for value in capabilities
        ):
            raise ValueError(f"collateral[{index}].capabilities must be strings")
        role_key = (export, role)
        if role_key in roles:
            raise ValueError(f"duplicate IP collateral role: {export}/{role}")
        if package_path in package_paths:
            raise ValueError(f"duplicate IP package path: {package_path}")
        roles.add(role_key)
        package_paths.add(package_path)
        module = entry.get("module")
        library = entry.get("library")
        cell = entry.get("cell")
        view = entry.get("view")
        corner = entry.get("corner")
        if module is not None:
            module = _string(module, f"collateral[{index}].module")
        if corner is not None:
            corner = _string(corner, f"collateral[{index}].corner")
        if library is not None:
            library = _string(library, f"collateral[{index}].library")
        if cell is not None:
            cell = _string(cell, f"collateral[{index}].cell")
        if view is not None:
            view = _string(view, f"collateral[{index}].view")
        parsed = IpCollateral(
            export=export,
            role=role,
            component=component,
            fileset=fileset,
            source=sources[0],
            package_path=package_path,
            format=_string(entry.get("format"), f"collateral[{index}].format"),
            module=module,
            library=library,
            cell=cell,
            view=view,
            corner=corner,
            capabilities=tuple(capabilities),
        )
        collateral_by_export[export].append(parsed)

    exports: list[IpExport] = []
    oa_identities: set[tuple[str, str]] = set()
    for name, values in export_specs.items():
        exported = IpExport(
            name=name,
            oa_library=str(values["oa_library"]),
            oa_cell=str(values["oa_cell"]),
            schematic_view=str(values["schematic_view"]),
            layout_view=str(values["layout_view"]),
            interface_contract=values["interface_contract"],
            physical_interface=str(values["physical_interface"]),
            logical_interface=str(values["logical_interface"]),
            collateral=tuple(collateral_by_export[name]),
            required_roles=values["required_roles"],
        )
        oa_identity = (exported.oa_library, exported.oa_cell)
        if oa_identity in oa_identities:
            raise ValueError(
                f"duplicate IP export OA identity: {exported.oa_library}/"
                f"{exported.oa_cell}"
            )
        oa_identities.add(oa_identity)
        present = {item.role for item in exported.collateral}
        development = set(exported.required_roles["development"])
        if not development.issubset(present):
            missing = ", ".join(sorted(development - present))
            raise ValueError(
                f"IP export {name} is missing development collateral: {missing}"
            )
        interface_path = (producer_path / exported.interface_contract).resolve()
        if not interface_path.is_file() or not interface_path.is_relative_to(
            producer_path
        ):
            raise FileNotFoundError(
                f"IP export interface contract is missing or outside its owner: {name}"
            )
        exports.append(exported)

    source_files = source.get("files", [])
    if not isinstance(source_files, list):
        raise ValueError("source.files must be an array")
    oa_assembly = safe_relative(source.get("oa_assembly"), "source.oa_assembly")
    oa_assembly_path = (root / oa_assembly).resolve()
    if (
        not oa_assembly_path.is_file()
        or not oa_assembly_path.is_relative_to(producer_path)
    ):
        raise FileNotFoundError(
            "source.oa_assembly must name a manifest inside the producer root"
        )

    default = _string(raw.get("default_maturity"), "default_maturity")
    if default not in RELEASE_MATURITY_LEVELS:
        raise ValueError("default_maturity is unsupported")
    result = IpContract(
        path=contract_path,
        project_root=root,
        name=ip_name,
        producer=producer,
        component_contract=component_contract,
        default_maturity=default,
        exports=tuple(exports),
        source_files=tuple(
            safe_relative(value, "source.files entry") for value in source_files
        ),
        oa_assembly=oa_assembly,
    )
    return result
