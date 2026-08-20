"""Contract and source-boundary checks for one non-OA verification cell."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sigilicon.domain.config_contracts import read_toml, require_config_header


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
        "dependencies",
        "contracts",
        "runner",
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
    project_root: Path
    cell: str
    role: str
    canonical_source: Path
    dut: str
    simulator: str
    dependencies: tuple[Path, ...]
    contracts: tuple[Path, ...]
    runner: Path | None

    def as_dict(self) -> dict[str, Any]:
        root = self.project_root

        def relative(path: Path) -> str:
            return path.relative_to(root).as_posix()

        return {
            "cell": self.cell,
            "role": self.role,
            "canonical_source": relative(self.canonical_source),
            "dut": self.dut,
            "simulator": self.simulator,
            "dependencies": [relative(path) for path in self.dependencies],
            "contracts": [relative(path) for path in self.contracts],
            "runner": relative(self.runner) if self.runner is not None else None,
        }


def load_verification_cell(
    path: Path,
    *,
    project_root: Path,
) -> VerificationCellSpec:
    """Load and validate one ``contract_kind = verification-cell`` document."""

    contract = path.resolve()
    root = project_root.resolve()
    if not contract.is_relative_to(root) or not contract.is_file():
        raise ValueError("verification cell contract must be project-owned")
    cell_root = contract.parent
    raw = read_toml(contract)
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
    dependencies = _files(
        raw.get("dependencies", []),
        cell_root=cell_root,
        project_root=root,
        field=f"{contract}: dependencies",
    )
    contracts = _files(
        raw.get("contracts", []),
        cell_root=cell_root,
        project_root=root,
        field=f"{contract}: contracts",
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
    return VerificationCellSpec(
        path=contract,
        project_root=root,
        cell=cell,
        role=role,
        canonical_source=canonical_source,
        dut=dut,
        simulator=simulator,
        dependencies=dependencies,
        contracts=contracts,
        runner=runner,
    )
