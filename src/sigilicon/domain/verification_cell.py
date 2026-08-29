"""Contract and source-boundary checks for one non-OA verification cell."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from sigilicon.domain.config_contracts import read_toml, require_config_header
from sigilicon.domain.repository import Project


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
    }
)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


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
    if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
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
    project: Project
    owner: str
    cell: str
    role: str
    canonical_source: Path
    dut: str
    simulator: str
    compile_sources: tuple[Path, ...]
    support_files: tuple[Path, ...]
    contracts: tuple[Path, ...]
    runner: Path | None
    success_marker: str | None

    @property
    def project_root(self) -> Path:
        return self.project.project_root

    @property
    def source_inputs(self) -> tuple[Path, ...]:
        """Files whose identity defines this verification cell invocation."""

        return (
            self.canonical_source,
            *self.compile_sources,
            *self.support_files,
            *self.contracts,
        )

    def as_dict(self) -> dict[str, Any]:
        root = self.project_root

        def relative(path: Path) -> str:
            return path.relative_to(root).as_posix()

        return {
            "owner": self.owner,
            "cell": self.cell,
            "role": self.role,
            "canonical_source": relative(self.canonical_source),
            "dut": self.dut,
            "simulator": self.simulator,
            "compile_sources": [relative(path) for path in self.compile_sources],
            "support_files": [relative(path) for path in self.support_files],
            "contracts": [relative(path) for path in self.contracts],
            "runner": relative(self.runner) if self.runner is not None else None,
            "success_marker": self.success_marker,
        }


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
) -> VerificationCellSpec:
    root = repository.project_root
    cell_root = contract.parent
    require_config_header(
        raw,
        contract,
        contract_kind="verification-cell",
        path_scope="cell",
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
    contracts = _files(
        raw.get("contracts", []),
        cell_root=cell_root,
        project_root=root,
        field=f"{contract}: contracts",
    )
    for declared_contract in contracts:
        declared_owner = repository.require_owner(declared_contract).name
        if declared_owner != owner:
            raise ValueError(
                f"{contract}: declared contract owner must be {owner!r}, "
                f"got {declared_owner!r} for {declared_contract}"
            )
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
        )
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
    source_inputs = (canonical_source, *compile_sources, *support_files, *contracts)
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
        project=repository,
        owner=owner,
        cell=cell,
        role=role,
        canonical_source=canonical_source,
        dut=dut,
        simulator=simulator,
        compile_sources=compile_sources,
        support_files=support_files,
        contracts=contracts,
        runner=runner,
        success_marker=success_marker,
    )


def parse_verification_cell(
    path: Path,
    document: Mapping[str, Any],
    *,
    project: Project | None = None,
    project_root: Path | None = None,
) -> VerificationCellSpec:
    """Validate one already read ``verification-cell`` document."""

    repository = Project.bind(project=project, project_root=project_root)
    contract = _verification_cell_path(path, repository)
    return _parse_verification_cell(
        contract,
        document,
        repository=repository,
    )


def load_verification_cell(
    path: Path,
    *,
    project: Project | None = None,
    project_root: Path | None = None,
) -> VerificationCellSpec:
    """Load and validate one ``contract_kind = verification-cell`` document."""

    repository = Project.bind(project=project, project_root=project_root)
    contract = _verification_cell_path(path, repository)
    return _parse_verification_cell(
        contract,
        read_toml(contract),
        repository=repository,
    )
