"""Declarative OA config/Maestro source shared by manual and automated runs."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib
from typing import Any, Mapping

from sigilicon.domain.platform import PdkConfig, load_platform
from sigilicon.domain.native_diagnostics import (
    NativeDiagnosticContract,
    NativeDiagnosticProcessor,
    load_native_diagnostic_processor,
    load_owner_native_diagnostic_processor,
)
from sigilicon.domain.repository import RepositoryContext


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
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
    diagnostic_processor: NativeDiagnosticProcessor | None = None

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
        if self.diagnostic_processor is None:
            raise RuntimeError("native diagnostic contract has no owner processor")
        return self.diagnostic_processor.nullable_scalar_names(
            self.diagnostic_equivalence
        )

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


@dataclass(frozen=True)
class OANativeSetup:
    """Source-owned native ADE/Maestro setup materialized through SKILL."""

    pdk: PdkConfig
    source: Path
    config_procedure: str
    maestro_procedure: str
    rdb_contract: OANativeRdbContract | None = None


@dataclass(frozen=True)
class OASimulationSpec:
    """The native-only schema-3 simulation identity consumed by OA workflows."""

    path: Path
    project_root: Path
    library: str
    cell: str
    dut: str
    top_view: str
    simulator: str
    native_setup: OANativeSetup
    contract_schema: int = 3


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
    default_diagnostic_processor: NativeDiagnosticProcessor | None,
) -> OANativeRdbContract:
    """Load the source-owned native RDB identity audit model.

    This file is deliberately separate from ``simulation.toml``: it does not
    configure ADE/Maestro or define a qualification rule.  It is an
    independently reviewable expectation used only after Cadence's official
    RDB objects have been read.
    """

    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read native RDB contract {path}: {exc}") from exc
    if set(raw) - {
        "schema",
        "point_count",
        "corners",
        "tests",
        "waveforms",
        "scalars",
        "setup_identity",
        "diagnostic_processor",
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
            "diagnostic_processor, and diagnostic_equivalence"
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

    diagnostic_processor = default_diagnostic_processor
    processor_value = raw.get("diagnostic_processor")
    diagnostic_raw = raw.get("diagnostic_equivalence")
    if processor_value is not None:
        if diagnostic_raw is None:
            raise ValueError(
                "native RDB diagnostic_processor requires diagnostic_equivalence"
            )
        if (
            not isinstance(processor_value, str)
            or not processor_value
            or Path(processor_value).name != processor_value
            or Path(processor_value).suffix != ".py"
        ):
            raise ValueError(
                "native RDB diagnostic_processor must be a testbench-local "
                "Python filename"
            )
        processor_source = (path.parent / processor_value).resolve()
        if (
            not processor_source.is_relative_to(path.parent.resolve())
            or not processor_source.is_relative_to(owner_root.resolve())
            or not processor_source.is_file()
        ):
            raise ValueError(
                "native RDB diagnostic_processor must be an existing "
                "testbench-owned file"
            )
        diagnostic_processor = load_native_diagnostic_processor(processor_source)

    diagnostic_equivalence: NativeDiagnosticContract | None = None
    if diagnostic_raw is not None:
        if diagnostic_processor is None:
            raise ValueError(
                "native RDB diagnostic_equivalence requires a testbench-local "
                "diagnostic_processor declared by the native RDB contract"
            )
        diagnostic_equivalence = diagnostic_processor.load_contract(
            diagnostic_raw,
            contract_path=path,
            project_root=project_root,
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
    if diagnostic_equivalence is not None:
        assert diagnostic_processor is not None
        diagnostic_processor.validate_contract(
            diagnostic_equivalence,
            point_count=point_count,
        )
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
    return OANativeRdbContract(
        path=path.resolve(),
        point_count=point_count,
        corners=corners,
        tests=tests,
        waveform_outputs=tuple(waveform_outputs),
        scalar_outputs=tuple(scalar_outputs),
        setup_model_identities=setup_model_identities,
        diagnostic_equivalence=diagnostic_equivalence,
        diagnostic_processor=diagnostic_processor,
    )


def _validate_native_rdb_contract_source(
    contract: OANativeRdbContract,
    setup_source: Path,
) -> None:
    """Require the audit model to name identities actually declared by setup.il."""

    setup_text = setup_source.read_text(encoding="utf-8")
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
    diagnostic = contract.diagnostic_equivalence
    if diagnostic is not None:
        if contract.diagnostic_processor is None:
            raise RuntimeError("native diagnostic contract has no owner processor")
        missing.extend(
            contract.diagnostic_processor.validate_source(diagnostic, setup_text)
        )
    if missing:
        raise ValueError(
            "native RDB contract is not consistent with setup.il: "
            + ", ".join(missing)
        )


def _validate_native_rdb_platform_models(
    contract: OANativeRdbContract,
    pdk: PdkConfig,
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
    context: RepositoryContext,
    owner_root: Path,
    raw: Mapping[str, Any],
    default_diagnostic_processor: NativeDiagnosticProcessor | None,
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
    pdk = load_platform(
        context,
        _identifier(platform.get("pdk"), "platform.pdk"),
    )
    setup_value = setup.get("source")
    if not isinstance(setup_value, str) or not setup_value:
        raise ValueError("setup.source must be a cell-relative path")
    setup_source = (spec_path.parent / setup_value).resolve()
    cell_root = spec_path.parent.resolve()
    if not setup_source.is_relative_to(cell_root) or not setup_source.is_file():
        raise ValueError("setup.source must stay inside its testbench cell")
    if not setup_source.is_relative_to(owner_root.resolve()):
        raise ValueError("setup.source must stay inside its owning active IP")
    config_procedure = _identifier(
        setup.get("config_procedure"), "setup.config_procedure"
    )
    maestro_procedure = _identifier(
        setup.get("maestro_procedure"), "setup.maestro_procedure"
    )
    setup_text = setup_source.read_text(encoding="utf-8")
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
            default_diagnostic_processor=default_diagnostic_processor,
        )
        if rdb_contract_path.is_file()
        else None
    )
    if rdb_contract is not None:
        _validate_native_rdb_contract_source(rdb_contract, setup_source)
        _validate_native_rdb_platform_models(rdb_contract, pdk)
    return OASimulationSpec(
        path=spec_path,
        project_root=project_root,
        library=library,
        cell=cell,
        dut=dut,
        top_view=source_view,
        simulator=simulator,
        native_setup=OANativeSetup(
            pdk=pdk,
            source=setup_source,
            config_procedure=config_procedure,
            maestro_procedure=maestro_procedure,
            rdb_contract=rdb_contract,
        ),
    )


def load_oa_simulation_spec(
    path: Path,
    *,
    project_root: Path,
) -> OASimulationSpec:
    """Load only the source-owned schema-3 thin native simulation contract."""

    spec_path = path.resolve()
    context = RepositoryContext.from_project_root(project_root)
    root = context.project_root
    if not spec_path.is_file() or not spec_path.is_relative_to(root):
        raise ValueError("OA simulation spec must be a project-owned file")
    owner = context.require_owner(spec_path)
    owner_root = owner.root
    try:
        with spec_path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read OA simulation spec {spec_path}: {exc}") from exc
    if raw.get("schema") != 3:
        raise ValueError("OA simulation schema must be exactly 3")
    return _load_native_oa_simulation_spec(
        spec_path,
        root,
        context=context,
        owner_root=owner_root,
        raw=raw,
        default_diagnostic_processor=load_owner_native_diagnostic_processor(
            context,
            owner_path=spec_path,
        ),
    )
