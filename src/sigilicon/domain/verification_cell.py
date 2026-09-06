"""Contract and source-boundary checks for one verification cell."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

from sigilicon.contracts import (
    contract_schema,
    freeze_toml_document,
    read_toml,
    require_config_header,
)
from sigilicon.domain.context import RepositoryIdentity
from sigilicon.domain.hdl import HdlCompilation

if TYPE_CHECKING:
    from sigilicon.project import Project


_FIELDS = frozenset(
    {
        "schema",
        "contract_kind",
        "path_scope",
        "owner",
        "cell",
        "role",
        "canonical_source",
        "dut",
        "simulator",
        "compile_sources",
        "support_files",
        "contracts",
        "runner",
        "success_marker",
        "ams",
        "top",
        "include_dirs",
        "defines",
    }
)

_AMS_FIELDS = frozenset(
    {
        "platform",
        "model_set",
        "circuit",
        "transient_stop",
        "ie_voltage",
    }
)
_AMS_RELEASE_CIRCUIT_FIELDS = frozenset(
    {"kind", "contract", "variant", "fileset", "dependency", "view"}
)
_AMS_SOURCE_CIRCUIT_FIELDS = frozenset({"kind", "path", "cell"})
_AMS_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_SPECTRE_TIME = re.compile(
    r"(?P<value>(?:0|[1-9][0-9]*)(?:\.[0-9]+)?)(?:[fpnum])?\Z"
)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _token(value: object, field: str) -> str:
    text = _text(value, field)
    if _AMS_TOKEN.fullmatch(text) is None:
        raise ValueError(f"{field} contains unsupported characters")
    return text


def _file(
    value: object,
    *,
    cell_root: Path,
    project_root: Path,
    field: str,
    inside_cell: bool = False,
) -> Path:
    relative = Path(_text(value, field))
    if relative.is_absolute():
        raise ValueError(f"{field} must be a relative path")
    resolved = (cell_root / relative).resolve()
    if not resolved.is_relative_to(project_root) or not resolved.is_file():
        raise ValueError(f"{field} must name an existing project-owned file")
    if inside_cell and not resolved.is_relative_to(cell_root):
        raise ValueError(f"{field} must stay inside the verification cell")
    return resolved


def _files(
    raw: object,
    *,
    cell_root: Path,
    project_root: Path,
    field: str,
    inside_cell: bool = False,
) -> tuple[Path, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)) or any(
        not isinstance(value, str) for value in raw
    ):
        raise ValueError(f"{field} must be a string array")
    result = tuple(
        _file(
            value,
            cell_root=cell_root,
            project_root=project_root,
            field=f"{field}[{index}]",
            inside_cell=inside_cell,
        )
        for index, value in enumerate(raw)
    )
    if len(set(result)) != len(result):
        raise ValueError(f"{field} must not contain duplicate files")
    return result


@dataclass(frozen=True)
class VerificationCellSpec:
    """One verification-owned testbench cell and its source boundary."""

    path: Path
    repository: RepositoryIdentity
    owner: str
    cell: str
    role: str
    canonical_source: Path
    dut: str
    simulator: str
    compile_sources: tuple[Path, ...]
    hdl: HdlCompilation
    support_files: tuple[Path, ...]
    contracts: tuple[Path, ...]
    runner: Path | None
    success_marker: str | None
    ams: XceliumAmsConfiguration | None = None
    source_documents: Mapping[Path, Mapping[str, Any]] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @property
    def project_root(self) -> Path:
        return self.repository.project_root

    @property
    def workspace_root(self) -> Path:
        return self.repository.workspace_root

    @property
    def source_inputs(self) -> tuple[Path, ...]:
        """Files whose identity defines this verification cell invocation."""

        return (
            self.canonical_source,
            *self.compile_sources,
            *self.support_files,
            *self.contracts,
            *(self.ams.circuit.source_inputs if self.ams is not None else ()),
        )

    def as_dict(self) -> dict[str, Any]:
        root = self.project_root

        def relative(path: Path) -> str:
            return path.relative_to(root).as_posix()

        payload: dict[str, Any] = {
            "owner": self.owner,
            "cell": self.cell,
            "role": self.role,
            "canonical_source": relative(self.canonical_source),
            "dut": self.dut,
            "simulator": self.simulator,
            "compile_sources": [relative(path) for path in self.compile_sources],
            "hdl": self.hdl.record,
            "support_files": [relative(path) for path in self.support_files],
            "contracts": [relative(path) for path in self.contracts],
            "runner": relative(self.runner) if self.runner is not None else None,
            "success_marker": self.success_marker,
        }
        if self.ams is not None:
            payload["ams"] = self.ams.as_dict(root=root)
        return payload


@dataclass(frozen=True)
class XceliumAmsReleaseCircuit:
    """One native circuit selected through a locked IP release."""

    contract: Path
    variant: str
    fileset: str
    dependency: str
    view: str

    @property
    def source_inputs(self) -> tuple[Path, ...]:
        return (self.contract,)

    def as_dict(self, *, root: Path) -> dict[str, object]:
        return {
            "kind": "ip-release",
            "contract": self.contract.relative_to(root).as_posix(),
            "variant": self.variant,
            "fileset": self.fileset,
            "dependency": self.dependency,
            "view": self.view,
        }


@dataclass(frozen=True)
class XceliumAmsSourceCircuit:
    """One project-owned standalone Spectre circuit source."""

    path: Path
    cell: str

    @property
    def source_inputs(self) -> tuple[Path, ...]:
        return (self.path,)

    def as_dict(self, *, root: Path) -> dict[str, object]:
        return {
            "kind": "source",
            "path": self.path.relative_to(root).as_posix(),
            "cell": self.cell,
        }


XceliumAmsCircuit = XceliumAmsReleaseCircuit | XceliumAmsSourceCircuit


@dataclass(frozen=True)
class XceliumAmsConfiguration:
    """Typed platform and circuit inputs for one Xcelium AMS cell."""

    platform: str
    model_set: str
    circuit: XceliumAmsCircuit
    transient_stop: str
    ie_voltage: float

    def as_dict(self, *, root: Path) -> dict[str, object]:
        return {
            "platform": self.platform,
            "model_set": self.model_set,
            "circuit": self.circuit.as_dict(root=root),
            "transient_stop": self.transient_stop,
            "ie_voltage": self.ie_voltage,
        }


def _parse_xcelium_ams_circuit(
    value: object,
    *,
    field: str,
    cell_root: Path,
    project_root: Path,
    owner: str,
    repository: Project,
) -> XceliumAmsCircuit:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a table")
    kind = _text(value.get("kind"), f"{field}.kind")
    if kind == "ip-release":
        expected = _AMS_RELEASE_CIRCUIT_FIELDS
        unknown = set(value) - expected
        missing = expected - set(value)
        if unknown or missing:
            raise ValueError(
                f"{field} fields must be exactly {sorted(expected)}; "
                f"missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        contract = _file(
            value.get("contract"),
            cell_root=cell_root,
            project_root=project_root,
            field=f"{field}.contract",
        )
        integration_owner = repository.require_owner(contract).name
        if integration_owner != owner:
            raise ValueError(
                f"{field}.contract owner must be {owner!r}, "
                f"got {integration_owner!r}"
            )
        return XceliumAmsReleaseCircuit(
            contract=contract,
            variant=_token(value.get("variant"), f"{field}.variant"),
            fileset=_token(value.get("fileset"), f"{field}.fileset"),
            dependency=_token(value.get("dependency"), f"{field}.dependency"),
            view=_token(value.get("view"), f"{field}.view"),
        )
    if kind == "source":
        expected = _AMS_SOURCE_CIRCUIT_FIELDS
        unknown = set(value) - expected
        missing = expected - set(value)
        if unknown or missing:
            raise ValueError(
                f"{field} fields must be exactly {sorted(expected)}; "
                f"missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        path = _file(
            value.get("path"),
            cell_root=cell_root,
            project_root=project_root,
            field=f"{field}.path",
        )
        circuit_owner = repository.require_owner(path).name
        if circuit_owner != owner:
            raise ValueError(
                f"{field}.path owner must be {owner!r}, got {circuit_owner!r}"
            )
        return XceliumAmsSourceCircuit(
            path=path,
            cell=_token(value.get("cell"), f"{field}.cell"),
        )
    raise ValueError(f"{field}.kind must be 'ip-release' or 'source'")


def _parse_xcelium_ams_configuration(
    value: object,
    *,
    contract: Path,
    cell_root: Path,
    project_root: Path,
    owner: str,
    repository: Project,
) -> XceliumAmsConfiguration:
    field = f"{contract}: ams"
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a table")
    unknown = set(value) - _AMS_FIELDS
    missing = _AMS_FIELDS - set(value)
    if unknown or missing:
        raise ValueError(
            f"{field} fields must be exactly {sorted(_AMS_FIELDS)}; "
            f"missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    circuit = _parse_xcelium_ams_circuit(
        value.get("circuit"),
        field=f"{field}.circuit",
        cell_root=cell_root,
        project_root=project_root,
        owner=owner,
        repository=repository,
    )
    transient_stop = _text(value.get("transient_stop"), f"{field}.transient_stop")
    time_match = _SPECTRE_TIME.fullmatch(transient_stop)
    if time_match is None or float(time_match.group("value")) <= 0.0:
        raise ValueError(
            f"{field}.transient_stop must be a positive Spectre time token"
        )
    ie_voltage = value.get("ie_voltage")
    if (
        isinstance(ie_voltage, bool)
        or not isinstance(ie_voltage, (int, float))
        or not math.isfinite(float(ie_voltage))
        or float(ie_voltage) <= 0.0
    ):
        raise ValueError(f"{field}.ie_voltage must be a positive finite number")
    return XceliumAmsConfiguration(
        platform=_token(value.get("platform"), f"{field}.platform"),
        model_set=_token(value.get("model_set"), f"{field}.model_set"),
        circuit=circuit,
        transient_stop=transient_stop,
        ie_voltage=float(ie_voltage),
    )


def _verification_cell_path(path: Path, repository: Project) -> Path:
    contract = path.resolve()
    if (
        not contract.is_relative_to(repository.project_root)
        or not contract.is_file()
    ):
        raise ValueError("verification cell contract must be project-owned")
    return contract


def _parse_verification_cell(
    contract: Path,
    raw: Mapping[str, Any],
    *,
    repository: Project,
    contract_documents: Mapping[Path, Mapping[str, Any]] | None = None,
) -> VerificationCellSpec:
    root = repository.project_root
    cell_root = contract.parent
    require_config_header(
        raw,
        contract,
        contract_kind="verification-cell",
        path_scope="cell",
        schema=2,
    )
    unknown = set(raw) - _FIELDS
    if unknown:
        raise ValueError(
            f"{contract}: verification cell contains unknown fields: {sorted(unknown)}"
        )
    owner = _text(raw.get("owner"), f"{contract}: owner")
    cataloged_owner = repository.require_owner(contract)
    if owner != cataloged_owner.name:
        raise ValueError(
            f"{contract}: owner must be {cataloged_owner.name!r}, got {owner!r}"
        )
    cell = _text(raw.get("cell"), f"{contract}: cell")
    if cell != cell_root.name:
        raise ValueError(
            f"{contract}: cell must match its directory name {cell_root.name!r}"
        )
    role = _text(raw.get("role"), f"{contract}: role")
    canonical_source = _file(
        raw.get("canonical_source"),
        cell_root=cell_root,
        project_root=root,
        field=f"{contract}: canonical_source",
        inside_cell=True,
    )
    dut = _text(raw.get("dut"), f"{contract}: dut")
    simulator = _text(raw.get("simulator"), f"{contract}: simulator")
    compile_sources = _files(
        raw.get("compile_sources", []),
        cell_root=cell_root,
        project_root=root,
        field=f"{contract}: compile_sources",
    )
    support_files = _files(
        raw.get("support_files", []),
        cell_root=cell_root,
        project_root=root,
        field=f"{contract}: support_files",
    )
    include_dirs = raw.get("include_dirs", ())
    if not isinstance(include_dirs, (list, tuple)) or any(not isinstance(item, str) for item in include_dirs):
        raise ValueError("include_dirs must be cell-relative directories")
    directories = []
    for item in include_dirs:
        directory = (cell_root / item).resolve()
        if not directory.is_relative_to(root):
            raise ValueError("include_dirs must stay inside the project")
        directories.append(directory.relative_to(root).as_posix())
    defines = raw.get("defines", {})
    if not isinstance(defines, Mapping):
        raise ValueError("defines must be a table")
    hdl = HdlCompilation(_text(raw.get("top"), "top"),
                         tuple(path.relative_to(root).as_posix() for path in (*compile_sources, canonical_source)),
                         tuple(path.relative_to(root).as_posix() for path in support_files if path.suffix.lower() in {".vh", ".svh"}),
                         tuple(directories), tuple(sorted(defines.items())))
    hdl.validate()
    contracts = _files(
        raw.get("contracts", []),
        cell_root=cell_root,
        project_root=root,
        field=f"{contract}: contracts",
    )
    source_documents = {contract: freeze_toml_document(raw)}
    for declared_contract in contracts:
        declared_owner = repository.require_owner(declared_contract).name
        if declared_owner != owner:
            raise ValueError(
                f"{contract}: declared contract owner must be {owner!r}, "
                f"got {declared_owner!r} for {declared_contract}"
            )
        declared_raw = (
            None
            if contract_documents is None
            else contract_documents.get(declared_contract)
        )
        if declared_raw is None:
            declared_raw = read_toml(declared_contract)
        declared_kind = _text(
            declared_raw.get("contract_kind"),
            f"{declared_contract}: contract_kind",
        )
        declared_scope = _text(
            declared_raw.get("path_scope"),
            f"{declared_contract}: path_scope",
        )
        require_config_header(
            declared_raw,
            declared_contract,
            contract_kind=declared_kind,
            path_scope=declared_scope,
            owner=owner,
            schema=contract_schema(declared_kind),
        )
        source_documents[declared_contract] = freeze_toml_document(declared_raw)
    runner_raw = raw.get("runner")
    runner = (
        None
        if runner_raw is None
        else _file(
            runner_raw,
            cell_root=cell_root,
            project_root=root,
            field=f"{contract}: runner",
            inside_cell=True,
        )
    )
    ams_raw = raw.get("ams")
    if simulator.lower() == "xcelium-ams":
        if ams_raw is None:
            raise ValueError(
                f"{contract}: xcelium-ams verification cell must declare ams"
            )
        ams = _parse_xcelium_ams_configuration(
            ams_raw,
            contract=contract,
            cell_root=cell_root,
            project_root=root,
            owner=owner,
            repository=repository,
        )
        if isinstance(ams.circuit, XceliumAmsReleaseCircuit):
            integration_raw = (
                None
                if contract_documents is None
                else contract_documents.get(ams.circuit.contract)
            )
            if integration_raw is None:
                integration_raw = read_toml(ams.circuit.contract)
            require_config_header(
                integration_raw,
                ams.circuit.contract,
                contract_kind="ip-component",
                path_scope="owner",
                owner=owner,
                schema=contract_schema("ip-component"),
            )
            source_documents[ams.circuit.contract] = freeze_toml_document(
                integration_raw
            )
    else:
        if ams_raw is not None:
            raise ValueError(
                f"{contract}: ams is valid only when simulator is xcelium-ams"
            )
        ams = None
    source_inputs = (
        canonical_source,
        *compile_sources,
        *support_files,
        *contracts,
        *(ams.circuit.source_inputs if ams is not None else ()),
    )
    if len(set(source_inputs)) != len(source_inputs):
        raise ValueError(
            f"{contract}: source, support, and contract inputs must be disjoint"
        )
    success_marker_raw = raw.get("success_marker")
    success_marker = (
        None
        if success_marker_raw is None
        else _text(success_marker_raw, f"{contract}: success_marker")
    )
    return VerificationCellSpec(
        path=contract,
        repository=RepositoryIdentity.for_owner(repository, owner),
        owner=owner,
        cell=cell,
        role=role,
        canonical_source=canonical_source,
        dut=dut,
        simulator=simulator,
        compile_sources=compile_sources,
        hdl=hdl,
        support_files=support_files,
        contracts=contracts,
        runner=runner,
        success_marker=success_marker,
        ams=ams,
        source_documents=MappingProxyType(source_documents),
    )


def parse_verification_cell(
    path: Path,
    document: Mapping[str, Any],
    *,
    project: Project,
    contract_documents: Mapping[Path, Mapping[str, Any]] | None = None,
) -> VerificationCellSpec:
    """Validate one already read ``verification-cell`` document."""

    repository = project
    contract = _verification_cell_path(path, repository)
    return _parse_verification_cell(
        contract,
        document,
        repository=repository,
        contract_documents=contract_documents,
    )


def load_verification_cell(
    path: Path,
    *,
    project: Project,
) -> VerificationCellSpec:
    """Load and validate one ``contract_kind = verification-cell`` document."""

    repository = project
    contract = _verification_cell_path(path, repository)
    return _parse_verification_cell(
        contract,
        read_toml(contract),
        repository=repository,
    )
