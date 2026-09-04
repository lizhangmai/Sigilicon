"""Declarative OA config/Maestro source shared by manual and automated runs."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import tomllib
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

from sigilicon.contracts import (
    freeze_toml_document,
    is_frozen_toml_document,
)
from sigilicon.domain.platform import (
    Platform,
    PlatformSnapshot,
    resolve_platform_snapshot,
)
from sigilicon.domain.native_diagnostics import (
    NativeDiagnosticContract,
    NativeDiagnosticProgram,
    NativeDiagnosticReport,
    load_native_diagnostic_program,
)
from sigilicon.domain.context import RepositoryIdentity
from sigilicon.domain.source import TextSourceSnapshot, load_text_source_snapshot

if TYPE_CHECKING:
    from sigilicon.project import Project


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))


@dataclass(frozen=True)
class OANativeRdbContract:
    """Independent identity model used to audit a native Maestro RDB."""

    path: Path
    point_count: int
    corners: tuple[str, ...]
    tests: tuple[str, ...]
    waveform_outputs: tuple[tuple[str, str], ...]
    scalar_outputs: tuple[tuple[str, str], ...]
    setup_model_identities: tuple[tuple[str, str], ...] = ()
    diagnostic_equivalence: NativeDiagnosticContract | None = None
    diagnostic_program: NativeDiagnosticProgram | None = None
    source_document: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    source_snapshot: TextSourceSnapshot | None = field(default=None, repr=False)
    support_source_snapshots: tuple[TextSourceSnapshot, ...] = field(
        default=(),
        repr=False,
    )

    @property
    def scalar_names(self) -> tuple[str, ...]:
        return tuple(name for name, _expression in self.scalar_outputs)

    @property
    def diagnostic_scalar_names(self) -> tuple[str, ...]:
        if self.diagnostic_equivalence is None:
            return ()
        return tuple(
            name for name, _expression in self.diagnostic_equivalence.scalar_outputs
        )

    @property
    def nullable_scalar_names(self) -> tuple[str, ...]:
        """Calculator outputs where ``nil`` is a reviewed diagnostic result."""

        if self.diagnostic_equivalence is None:
            return ()
        return self.diagnostic_equivalence.nullable_scalar_names

    @property
    def support_sources(self) -> tuple[Path, ...]:
        if self.diagnostic_equivalence is None:
            return ()
        return self.diagnostic_equivalence.support_sources

    @property
    def expected_expression_count(self) -> int:
        return (
            self.point_count
            * len(self.corners)
            * len(self.tests)
            * len(self.scalar_outputs)
        )

    def reconstruct_diagnostic(
        self,
        result: Mapping[str, object],
    ) -> NativeDiagnosticReport | None:
        """Apply this contract's owner processor to one normalized RDB result."""

        if self.diagnostic_equivalence is None:
            return None
        if self.diagnostic_program is None:
            raise RuntimeError("native diagnostic contract has no owner program")
        return self.diagnostic_program.reconstruct(
            result,
            self.diagnostic_equivalence,
            point_count=self.point_count,
            corners=self.corners,
            tests=self.tests,
        )


@dataclass(frozen=True)
class OANativeSetup:
    """Source-owned native ADE/Maestro setup materialized through SKILL."""

    pdk: Platform
    source_snapshot: TextSourceSnapshot
    config_procedure: str
    maestro_procedure: str
    rdb_contract: OANativeRdbContract | None = None

    @property
    def source(self) -> Path:
        return self.source_snapshot.source_path


@dataclass(frozen=True)
class OASimulationSpec:
    """The native-only schema-3 simulation identity consumed by OA workflows."""

    path: Path
    repository: RepositoryIdentity
    library: str
    cell: str
    dut: str
    top_view: str
    simulator: str
    native_setup: OANativeSetup
    contract_schema: int = 3
    source_documents: Mapping[Path, Mapping[str, Any]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    source_snapshot: TextSourceSnapshot | None = field(default=None, repr=False)

    @property
    def project_root(self) -> Path:
        return self.repository.project_root

    @property
    def workspace_root(self) -> Path:
        return self.repository.workspace_root


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} must be an identifier")
    return value


def _table(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a table")
    return value


def _rows(value: object, field: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or not value or not all(
        isinstance(row, Mapping) for row in value
    ):
        raise ValueError(f"{field} must be a non-empty array of tables")
    return tuple(value)


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{field} must be a non-empty string array")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise ValueError(f"{field} contains duplicates")
    return result


def _load_native_rdb_contract(
    path: Path,
    *,
    project_root: Path,
    owner_root: Path,
    setup_text: str,
    architecture_source_documents: Mapping[Path, Mapping[str, Any]] | None,
) -> OANativeRdbContract:
    """Load the source-owned native RDB identity audit model.

    This file is deliberately separate from ``simulation.toml``: it does not
    configure ADE/Maestro or define a qualification rule.  It is an
    independently reviewable expectation used only after Cadence's official
    RDB objects have been read.
    """

    try:
        source_snapshot = load_text_source_snapshot(path)
        raw = tomllib.loads(source_snapshot.text)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, RuntimeError) as exc:
        raise ValueError(f"cannot read native RDB contract {path}: {exc}") from exc
    if set(raw) - {
        "schema",
        "point_count",
        "corners",
        "tests",
        "waveforms",
        "scalars",
        "setup_identity",
        "diagnostic_program",
        "diagnostic_equivalence",
    } or not {
        "schema",
        "point_count",
        "corners",
        "tests",
        "waveforms",
        "scalars",
    }.issubset(raw):
        raise ValueError(
            "native RDB contract fields must be exactly schema, point_count, "
            "corners, tests, waveforms, scalars, with optional setup_identity, "
            "diagnostic_program, and diagnostic_equivalence"
        )
    if raw.get("schema") != 2:
        raise ValueError("native RDB contract schema must be 2")
    point_count = raw.get("point_count")
    if (
        isinstance(point_count, bool)
        or not isinstance(point_count, int)
        or point_count <= 0
    ):
        raise ValueError("native RDB contract point_count must be a positive integer")
    corners = tuple(
        _identifier(value, "native RDB contract corner")
        for value in _strings(raw.get("corners"), "native RDB contract corners")
    )
    tests = tuple(
        _identifier(value, "native RDB contract test")
        for value in _strings(raw.get("tests"), "native RDB contract tests")
    )

    waveform_rows = _rows(raw.get("waveforms"), "native RDB contract waveforms")
    waveform_outputs: list[tuple[str, str]] = []
    for index, row in enumerate(waveform_rows):
        field = f"native RDB contract waveforms[{index}]"
        if set(row) != {"name", "signal"}:
            raise ValueError(f"{field} fields must be exactly name and signal")
        name = _identifier(row.get("name"), f"{field}.name")
        signal = row.get("signal")
        if not isinstance(signal, str) or not signal.startswith("/"):
            raise ValueError(f"{field}.signal must be an absolute OA net name")
        waveform_outputs.append((name, signal))

    scalar_value = raw.get("scalars")
    scalar_rows = (
        ()
        if scalar_value == []
        else _rows(scalar_value, "native RDB contract scalars")
    )
    scalar_outputs: list[tuple[str, str]] = []
    for index, row in enumerate(scalar_rows):
        field = f"native RDB contract scalars[{index}]"
        if set(row) != {"name", "expression"}:
            raise ValueError(
                f"{field} fields must be exactly name and expression"
            )
        name = _identifier(row.get("name"), f"{field}.name")
        expression = row.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError(f"{field}.expression must be non-empty text")
        scalar_outputs.append((name, expression))

    setup_model_identities: tuple[tuple[str, str], ...] = ()
    setup_identity_raw = raw.get("setup_identity")
    if setup_identity_raw is not None:
        setup_identity = _table(
            setup_identity_raw,
            "native RDB contract setup_identity",
        )
        if set(setup_identity) != {"models"}:
            raise ValueError(
                "native RDB contract setup_identity fields must be exactly models"
            )
        model_rows = _rows(
            setup_identity.get("models"),
            "native RDB contract setup_identity.models",
        )
        models: list[tuple[str, str]] = []
        for index, row in enumerate(model_rows):
            field = f"native RDB contract setup_identity.models[{index}]"
            if set(row) != {"file", "section"}:
                raise ValueError(
                    f"{field} fields must be exactly file and section"
                )
            model_file = row.get("file")
            if (
                not isinstance(model_file, str)
                or not model_file
                or Path(model_file).name != model_file
            ):
                raise ValueError(f"{field}.file must be a stable file basename")
            section = _identifier(row.get("section"), f"{field}.section")
            models.append((model_file, section))
        if len(set(models)) != len(models):
            raise ValueError(
                "native RDB contract setup_identity models must be unique"
            )
        setup_model_identities = tuple(models)

    diagnostic_program: NativeDiagnosticProgram | None = None
    program_value = raw.get("diagnostic_program")
    diagnostic_raw = raw.get("diagnostic_equivalence")
    if program_value is not None:
        if diagnostic_raw is None:
            raise ValueError(
                "native RDB diagnostic_program requires diagnostic_equivalence"
            )
        if (
            not isinstance(program_value, str)
            or not program_value
            or Path(program_value).name != program_value
            or Path(program_value).suffix != ".py"
        ):
            raise ValueError(
                "native RDB diagnostic_program must be a testbench-local "
                "Python filename"
            )
        program_source = (path.parent / program_value).resolve()
        if (
            not program_source.is_relative_to(path.parent.resolve())
            or not program_source.is_relative_to(owner_root.resolve())
            or not program_source.is_file()
        ):
            raise ValueError(
                "native RDB diagnostic_program must be an existing "
                "testbench-owned file"
            )
        diagnostic_program = load_native_diagnostic_program(
            program_source,
            project_root=project_root,
        )

    diagnostic_equivalence: NativeDiagnosticContract | None = None
    if diagnostic_raw is not None:
        if diagnostic_program is None:
            raise ValueError(
                "native RDB diagnostic_equivalence requires a testbench-local "
                "diagnostic_program declared by the native RDB contract"
            )
        if architecture_source_documents is not None and (
            not isinstance(architecture_source_documents, _MAPPING_PROXY_TYPE)
            or any(
                not isinstance(source, Path)
                or source != source.resolve()
                or not source.is_relative_to(project_root)
                or not source.is_file()
                or not isinstance(document, Mapping)
                or not is_frozen_toml_document(document)
                for source, document in architecture_source_documents.items()
            )
        ):
            raise ValueError("architecture source inventory identity drift")
        owner_architecture_documents = (
            None
            if architecture_source_documents is None
            else MappingProxyType(
                {
                    source: document
                    for source, document in architecture_source_documents.items()
                    if source.is_relative_to(owner_root.resolve())
                }
            )
        )
        diagnostic_equivalence = diagnostic_program.describe(
            diagnostic_raw,
            contract_path=path,
            source_documents=owner_architecture_documents,
            point_count=point_count,
            tests=tests,
            setup_text=setup_text,
        )

    waveform_names = [name for name, _signal in waveform_outputs]
    explicit_scalar_names = [name for name, _expression in scalar_outputs]
    diagnostic_scalar_outputs = (
        ()
        if diagnostic_equivalence is None
        else diagnostic_equivalence.scalar_outputs
    )
    scalar_outputs.extend(diagnostic_scalar_outputs)
    scalar_names = [name for name, _expression in scalar_outputs]
    if len(set(waveform_names)) != len(waveform_names):
        raise ValueError("native RDB contract waveform names must be unique")
    if len(set(scalar_names)) != len(scalar_names):
        raise ValueError("native RDB contract scalar names must be unique")
    if set(explicit_scalar_names) & {
        name for name, _expression in diagnostic_scalar_outputs
    }:
        raise ValueError(
            "native RDB contract explicit and diagnostic scalar names must be disjoint"
        )
    if set(waveform_names) & set(scalar_names):
        raise ValueError(
            "native RDB contract waveform and scalar names must be disjoint"
        )
    support_source_snapshots: list[TextSourceSnapshot] = []
    support_sources = (
        ()
        if diagnostic_equivalence is None
        else diagnostic_equivalence.support_sources
    )
    for source in support_sources:
        if (
            source != source.resolve()
            or not source.is_relative_to(owner_root.resolve())
            or not source.is_file()
        ):
            raise ValueError(
                "native RDB diagnostic support source must stay inside its owner"
            )
        program_snapshot = (
            None
            if diagnostic_program is None
            else diagnostic_program.source_snapshot
        )
        support_source_snapshots.append(
            program_snapshot
            if program_snapshot is not None
            and program_snapshot.source_path == source
            else load_text_source_snapshot(source)
        )
    return OANativeRdbContract(
        path=path.resolve(),
        point_count=point_count,
        corners=corners,
        tests=tests,
        waveform_outputs=tuple(waveform_outputs),
        scalar_outputs=tuple(scalar_outputs),
        setup_model_identities=setup_model_identities,
        diagnostic_equivalence=diagnostic_equivalence,
        diagnostic_program=diagnostic_program,
        source_document=freeze_toml_document(raw),
        source_snapshot=source_snapshot,
        support_source_snapshots=tuple(support_source_snapshots),
    )


def _validate_native_rdb_contract_source(
    contract: OANativeRdbContract,
    setup_text: str,
) -> None:
    """Require the audit model to name identities actually declared by setup.il."""

    missing: list[str] = []
    diagnostic_scalar_names = set(contract.diagnostic_scalar_names)
    for name, signal in contract.waveform_outputs:
        if f'"{name}"' not in setup_text or f'"{signal}"' not in setup_text:
            missing.append(f"waveform {name}/{signal}")
    for name, expression in contract.scalar_outputs:
        if name in diagnostic_scalar_names:
            continue
        escaped_expression = expression.replace('"', r'\"')
        if (
            f'"{name}"' not in setup_text
            or f'"{escaped_expression}"' not in setup_text
        ):
            missing.append(f"scalar {name}/{expression}")
    for test in contract.tests:
        if f'"{test}"' not in setup_text:
            missing.append(f"test {test}")
    for corner in contract.corners:
        if f'"{corner}"' not in setup_text:
            missing.append(f"corner {corner}")
    for model_file, section in contract.setup_model_identities:
        if model_file not in setup_text or f'"{section}"' not in setup_text:
            missing.append(f"setup model {model_file}/{section}")
    if missing:
        raise ValueError(
            "native RDB contract is not consistent with setup.il: "
            + ", ".join(missing)
        )


def _validate_native_rdb_platform_models(
    contract: OANativeRdbContract,
    pdk: Platform,
) -> None:
    """Require audited setup models to be declared by the selected platform."""

    declared = {
        (model_set.file.name, section)
        for model_set in pdk.simulation.model_sets.values()
        for section in model_set.sections
    }
    unknown = set(contract.setup_model_identities) - declared
    if unknown:
        identities = ", ".join(
            f"{model_file}/{section}"
            for model_file, section in sorted(unknown)
        )
        raise ValueError(
            "native RDB setup models are not declared by the selected platform: "
            + identities
        )


def _load_native_oa_simulation_spec(
    spec_path: Path,
    project_root: Path,
    *,
    context: Project,
    owner_root: Path,
    raw: Mapping[str, Any],
    source_snapshot: TextSourceSnapshot,
    platform_snapshot: PlatformSnapshot | None,
    architecture_source_documents: Mapping[Path, Mapping[str, Any]] | None,
) -> OASimulationSpec:
    """Load the thin contract used by native ADE/Maestro pilot cells.

    The contract deliberately contains no analysis, corner, output, or
    measurement language.  Those semantics live in the source-owned setup
    program and are executed by the Cadence APIs during materialization.
    """

    allowed_root = {"schema", "testbench", "platform", "setup"}
    unknown_root = set(raw) - allowed_root
    if unknown_root:
        raise ValueError(
            "native OA simulation contract contains unsupported fields: "
            f"{sorted(unknown_root)}"
        )
    testbench = _table(raw.get("testbench"), "testbench")
    if set(testbench) != {"library", "cell", "dut", "source_view", "simulator"}:
        raise ValueError(
            "native testbench fields must be exactly library, cell, dut, "
            "source_view, and simulator"
        )
    platform = _table(raw.get("platform"), "platform")
    if set(platform) != {"pdk"}:
        raise ValueError("native platform fields must be exactly pdk")
    setup = _table(raw.get("setup"), "setup")
    if set(setup) != {"source", "config_procedure", "maestro_procedure"}:
        raise ValueError(
            "native setup fields must be exactly source, config_procedure, "
            "and maestro_procedure"
        )

    library = _identifier(testbench.get("library"), "testbench.library")
    cell = _identifier(testbench.get("cell"), "testbench.cell")
    dut = _identifier(testbench.get("dut"), "testbench.dut")
    source_view = _identifier(testbench.get("source_view"), "testbench.source_view")
    simulator = _identifier(testbench.get("simulator"), "testbench.simulator")
    if simulator not in {"spectre", "ams"}:
        raise ValueError("testbench.simulator must be spectre or ams")
    pdk = resolve_platform_snapshot(
        context,
        _identifier(platform.get("pdk"), "platform.pdk"),
        snapshot=platform_snapshot,
    )
    setup_value = setup.get("source")
    if not isinstance(setup_value, str) or not setup_value:
        raise ValueError("setup.source must be a cell-relative path")
    setup_source = (spec_path.parent / setup_value).resolve()
    cell_root = spec_path.parent.resolve()
    if not setup_source.is_relative_to(cell_root) or not setup_source.is_file():
        raise ValueError("setup.source must stay inside its testbench cell")
    if not setup_source.is_relative_to(owner_root.resolve()):
        raise ValueError("setup.source must stay inside its cataloged owner")
    config_procedure = _identifier(
        setup.get("config_procedure"), "setup.config_procedure"
    )
    maestro_procedure = _identifier(
        setup.get("maestro_procedure"), "setup.maestro_procedure"
    )
    setup_snapshot = load_text_source_snapshot(setup_source)
    setup_text = setup_snapshot.text
    for field, procedure in (
        ("setup.config_procedure", config_procedure),
        ("setup.maestro_procedure", maestro_procedure),
    ):
        if re.search(rf"\bprocedure\({re.escape(procedure)}\s*\(", setup_text) is None:
            raise ValueError(f"{field} is not declared by setup.source")
    rdb_contract_path = (cell_root / "native_rdb.toml").resolve()
    rdb_contract = (
        _load_native_rdb_contract(
            rdb_contract_path,
            project_root=project_root,
            owner_root=owner_root,
            setup_text=setup_text,
            architecture_source_documents=architecture_source_documents,
        )
        if rdb_contract_path.is_file()
        else None
    )
    if rdb_contract is not None:
        _validate_native_rdb_contract_source(rdb_contract, setup_text)
        _validate_native_rdb_platform_models(rdb_contract, pdk)
    source_documents = {spec_path: freeze_toml_document(raw)}
    if rdb_contract is not None:
        source_documents[rdb_contract.path] = rdb_contract.source_document
    return OASimulationSpec(
        path=spec_path,
        repository=RepositoryIdentity.capture(context),
        library=library,
        cell=cell,
        dut=dut,
        top_view=source_view,
        simulator=simulator,
        native_setup=OANativeSetup(
            pdk=pdk,
            source_snapshot=setup_snapshot,
            config_procedure=config_procedure,
            maestro_procedure=maestro_procedure,
            rdb_contract=rdb_contract,
        ),
        source_documents=MappingProxyType(source_documents),
        source_snapshot=source_snapshot,
    )


def load_oa_simulation_spec(
    path: Path,
    *,
    project: Project,
    platform: PlatformSnapshot | None = None,
    architecture_source_documents: Mapping[Path, Mapping[str, Any]] | None = None,
) -> OASimulationSpec:
    """Load only the source-owned schema-3 thin native simulation contract."""

    spec_path = path.resolve()
    context = project
    root = context.project_root
    if not spec_path.is_file() or not spec_path.is_relative_to(root):
        raise ValueError("OA simulation spec must be a project-owned file")
    owner = context.require_owner(spec_path)
    owner_root = owner.root
    try:
        source_snapshot = load_text_source_snapshot(spec_path)
        raw = tomllib.loads(source_snapshot.text)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, RuntimeError) as exc:
        raise ValueError(f"cannot read OA simulation spec {spec_path}: {exc}") from exc
    if raw.get("schema") != 3:
        raise ValueError("OA simulation schema must be exactly 3")
    return _load_native_oa_simulation_spec(
        spec_path,
        root,
        context=context,
        owner_root=owner_root,
        raw=raw,
        source_snapshot=source_snapshot,
        platform_snapshot=platform,
        architecture_source_documents=architecture_source_documents,
    )
