"""Audited XStream/Calibre DRC and LVS for generated OA layouts."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Any, Mapping, Sequence

from sigilicon.artifacts import atomic_write_json, read_nofollow_text
from sigilicon.canonical import canonical_json
from sigilicon.domain.netlist import render_canonical_cdl, resolve_netlist_hierarchy
from sigilicon.domain.physical_verification import (
    CheckedLayoutIdentity,
    CheckedSourceIdentity,
    DrcEvidence,
    DrcViolation,
    LvsEvidence,
    LvsMismatch,
    PhysicalVerificationPolicy,
    PhysicalVerificationEvidence,
    drc_evidence_id,
    lvs_evidence_id,
    PhysicalVerificationStatus,
    VerificationCompletion,
    drc_evidence_from_json,
    load_physical_verification_policy,
    lvs_evidence_from_json,
)
from sigilicon.external_tools import (
    owned_directory,
    owned_input_file,
    run_process_group,
)
from sigilicon.flow.adapter_result import complete_staged_run
from sigilicon.flow.evidence import FactSource
from sigilicon.flow.model import (
    ActionContext,
    AdapterExecution,
    AdapterResult,
    CollectedActionResult,
    FlowExecutionError,
    ProducedArtifact,
)
from sigilicon.flow.physical_verification import (
    CALIBRE_PHYSICAL_VERIFICATION_ADAPTER,
    DRC_ACTION,
    DRC_EVIDENCE_KIND,
    LVS_ACTION,
    LVS_EVIDENCE_KIND,
)
from sigilicon.layout.ir import LayoutPlan
from sigilicon.layout.materialization_execution import (
    MaterializationReceipt,
    materialization_receipt_from_json,
    materialization_receipt_id,
    validate_receipt_bound_layout,
)
from sigilicon.layout.spec import LayoutSpec
from sigilicon.workflows.run_artifacts import RunArtifacts


_DRC_RESULT = re.compile(
    r"^RULECHECK (?P<name>.+?) \.+ TOTAL Result Count = (?P<count>\d+)\s+\(\d+\)$",
    re.MULTILINE,
)
_ORIGINAL_LAYER = re.compile(
    r"^LAYER (?P<name>\S+) \.+ TOTAL Original Geometry Count = "
    r"(?P<count>\d+)\s+\(\d+\)$",
    re.MULTILINE,
)
@dataclass(frozen=True)
class ReceiptBoundLayoutSourceInputs:
    """Validated materialization identities shared by downstream physical Actions."""

    receipt: MaterializationReceipt
    receipt_identity: str
    layout: CheckedLayoutIdentity
    layout_path: Path
    source: CheckedSourceIdentity | None
    source_path: Path | None


@dataclass(frozen=True)
class ReceiptBoundVerificationInputs(ReceiptBoundLayoutSourceInputs):
    """Receipt-bound layout/source identities plus an owner verification policy."""

    policy: PhysicalVerificationPolicy


def _replace_exact(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label} changed; expected one exact occurrence, got {count}")
    return text.replace(old, new, 1)


def _disable_define(text: str, name: str, *, expected: int = 1) -> str:
    pattern = re.compile(
        rf"^#DEFINE[ \t]+{re.escape(name)}(?P<tail>[ \t].*)?$",
        re.MULTILINE,
    )
    result, count = pattern.subn(r"//#DEFINE " + name + r"\g<tail>", text)
    if count != expected:
        raise RuntimeError(
            f"Calibre DRC option {name} changed; expected {expected} enabled definition(s), got {count}"
        )
    return result


def render_drc_run_deck(
    source: str,
    *,
    layout_path: str,
    primary: str,
    results_path: str,
    summary_path: str,
    disabled_defines: Mapping[str, int] | None = None,
) -> str:
    """Resolve the foundry macro-cell DRC profile without adding waivers."""

    result = source
    for name, expected in (disabled_defines or {}).items():
        result = _disable_define(result, name, expected=expected)
    for old, new, label in (
        ('LAYOUT PATH "GDSFILENAME"', f'LAYOUT PATH "{layout_path}"', "DRC layout path"),
        ('LAYOUT PRIMARY "TOPCELLNAME"', f'LAYOUT PRIMARY "{primary}"', "DRC primary"),
        ('DRC RESULTS DATABASE "DRC_RES.db"', f'DRC RESULTS DATABASE "{results_path}"', "DRC results"),
        (
            'DRC SUMMARY REPORT "DRC.rep"  // HIER',
            f'DRC SUMMARY REPORT "{summary_path}"  // HIER',
            "DRC summary",
        ),
    ):
        result = _replace_exact(result, old, new, label=label)
    return result


def render_lvs_run_deck(
    source: str,
    *,
    layout_path: str,
    source_path: str,
    primary: str,
    work_dir: str,
) -> str:
    """Resolve the foundry LVS deck for one immutable layout/source pair."""

    replacements = (
        ('LAYOUT PRIMARY "lvs_top"', f'LAYOUT PRIMARY "{primary}"', "LVS layout primary"),
        ('LAYOUT PATH "lvs_top.gds"', f'LAYOUT PATH "{layout_path}"', "LVS layout path"),
        ('SOURCE PRIMARY "lvs_top"', f'SOURCE PRIMARY "{primary}"', "LVS source primary"),
        ('SOURCE PATH "lvs_top.cdl"', f'SOURCE PATH "{source_path}"', "LVS source path"),
        (
            'DRC RESULTS DATABASE "calibre_drc.db" ASCII // ASCII or GDSII',
            f'DRC RESULTS DATABASE "{work_dir}/lvs-calibre-drc.db" ASCII // ASCII or GDSII',
            "LVS DRC results",
        ),
        (
            'DRC SUMMARY REPORT "calibre_drc.sum"',
            f'DRC SUMMARY REPORT "{work_dir}/lvs-calibre-drc.sum"',
            "LVS DRC summary",
        ),
        (
            'ERC RESULTS DATABASE "calibre_erc.db" ASCII // ASCII or GDSII',
            f'ERC RESULTS DATABASE "{work_dir}/calibre_erc.db" ASCII // ASCII or GDSII',
            "LVS ERC results",
        ),
        (
            'ERC SUMMARY REPORT "calibre_erc.sum"',
            f'ERC SUMMARY REPORT "{work_dir}/calibre_erc.sum"',
            "LVS ERC summary",
        ),
        ('LVS REPORT "lvs.rep"', f'LVS REPORT "{work_dir}/lvs.rep"', "LVS report"),
        (
            '  //MASK SVDB DIRECTORY "svdb" QUERY',
            f'  //MASK SVDB DIRECTORY "{work_dir}/svdb" QUERY',
            "disabled LVS SVDB directory",
        ),
        (
            '  MASK SVDB DIRECTORY "svdb" QUERY',
            f'  MASK SVDB DIRECTORY "{work_dir}/svdb" QUERY',
            "LVS SVDB directory",
        ),
    )
    result = source
    for old, new, label in replacements:
        result = _replace_exact(result, old, new, label=label)
    return result


def render_canonical_source_cdl(spec: LayoutSpec) -> str:
    """Render LVS source only from the declared canonical source closure."""

    hierarchy = resolve_netlist_hierarchy(
        spec.source_snapshots,
        top=spec.cell,
        primitive_masters=spec.primitive_masters,
    )
    if hierarchy.definitions[spec.cell].ports != spec.ports:
        raise RuntimeError("resolved LVS hierarchy changed the canonical top interface")
    primitive_interfaces = []
    for master in sorted(hierarchy.primitive_counts):
        terminals = spec.pdk.oa.primitive_subcircuits.get(master)
        if terminals is None:
            continue
        primitive_interfaces.extend(
            (
                f".SUBCKT {master} {' '.join(terminals)}",
                f".ENDS {master}",
                "",
            )
        )
    return "\n".join(primitive_interfaces) + render_canonical_cdl(
        hierarchy,
        primitive_subcircuit_masters=spec.pdk.oa.primitive_subcircuits,
    )


def parse_drc_summary(
    text: str,
    *,
    configuration_warnings: Sequence[str] = (),
    waiver_layers: Sequence[str] = (),
) -> dict[str, object]:
    configured_warnings = frozenset(configuration_warnings)
    configured_waiver_layers = frozenset(waiver_layers)
    counts = {match["name"]: int(match["count"]) for match in _DRC_RESULT.finditer(text)}
    if not counts:
        raise RuntimeError("Calibre DRC summary contains no rulecheck totals")
    total_match = re.search(r"^TOTAL DRC Results Generated:\s+(\d+) \(\d+\)$", text, re.MULTILINE)
    if total_match is None:
        raise RuntimeError("Calibre DRC summary has no total result count")
    total = int(total_match.group(1))
    configuration_warning_count = sum(
        counts.get(name, 0) for name in configured_warnings
    )
    violation_count = sum(
        count for name, count in counts.items() if name not in configured_warnings
    )
    # Foundry deck revisions differ on whether warning-only rulechecks are
    # included in "TOTAL DRC Results Generated".  Accept exactly those two
    # documented forms while still refusing any unexplained count mismatch.
    if total not in {
        violation_count,
        violation_count + configuration_warning_count,
    }:
        raise RuntimeError("Calibre DRC per-rule counts do not match the total")
    runtime_section = re.search(
        r"^--- RUNTIME WARNINGS(?: ---)?\s*$\n(?:^---\s*$\n)?"
        r"(?P<body>.*?)^--- ORIGINAL LAYER STATISTICS(?: ---)?\s*$",
        text,
        re.MULTILINE | re.DOTALL,
    )
    runtime_warnings = (
        tuple(
            line.strip()
            for line in runtime_section.group("body").splitlines()
            if line.strip() and set(line.strip()) != {"-"}
        )
        if runtime_section is not None
        else ()
    )
    layers = {
        match["name"]: int(match["count"])
        for match in _ORIGINAL_LAYER.finditer(text)
    }
    missing_waiver_layers = configured_waiver_layers - set(layers)
    if missing_waiver_layers:
        raise RuntimeError(
            "Calibre DRC summary omitted waiver-layer statistics: "
            + ", ".join(sorted(missing_waiver_layers))
        )
    waiver_layers_used = any(layers[name] != 0 for name in configured_waiver_layers)
    used_waiver_layers = tuple(
        sorted(name for name in configured_waiver_layers if layers[name] != 0)
    )
    return {
        "passed": violation_count == 0 and not waiver_layers_used,
        "result_count": total,
        "configuration_warning_count": configuration_warning_count,
        "runtime_warning_count": len(runtime_warnings),
        "runtime_warnings": runtime_warnings,
        "violation_count": violation_count,
        "waiver_layers_used": waiver_layers_used,
        "used_waiver_layers": used_waiver_layers,
        "configuration_warning_rules": tuple(sorted(configured_warnings)),
        "nonzero_rulechecks": {
            name: count for name, count in sorted(counts.items()) if count
        },
    }


def parse_lvs_report(text: str, *, primary: str) -> dict[str, object]:
    match = re.search(
        rf"^\s*(CORRECT|INCORRECT|NOT COMPARED)\s+{re.escape(primary)}\s+{re.escape(primary)}\s*$",
        text,
        re.MULTILINE,
    )
    if match is None:
        raise RuntimeError("Calibre LVS report has no top-cell comparison result")
    comparison = match.group(1)
    return {"passed": comparison == "CORRECT", "comparison_result": comparison}


def drc_evidence_from_summary(
    text: str,
    *,
    layout: CheckedLayoutIdentity,
    backend: str,
    exit_code: int,
    configuration_warnings: Sequence[str] = (),
    waiver_layers: Sequence[str] = (),
) -> DrcEvidence:
    """Project the canonical DRC parser result into typed completion evidence."""

    parsed = parse_drc_summary(
        text,
        configuration_warnings=configuration_warnings,
        waiver_layers=waiver_layers,
    )
    warnings = frozenset(configuration_warnings)
    violations = tuple(
        DrcViolation(str(rule), int(count))
        for rule, count in parsed["nonzero_rulechecks"].items()
        if rule not in warnings
    ) + tuple(
        DrcViolation(f"waiver-layer:{layer}", 1)
        for layer in parsed["used_waiver_layers"]
    )
    status = (
        PhysicalVerificationStatus.EXECUTION_FAILED
        if exit_code != 0
        else PhysicalVerificationStatus.CLEAN
        if parsed["passed"]
        else PhysicalVerificationStatus.VIOLATED
    )
    return DrcEvidence(
        status=status,
        layout=layout,
        completion=VerificationCompletion(
            backend=backend,
            executed=True,
            report_parsed=True,
            exit_code=exit_code,
        ),
        violations=violations,
        message=(
            "DRC report is clean"
            if status is PhysicalVerificationStatus.CLEAN
            else "DRC report contains violations"
            if status is PhysicalVerificationStatus.VIOLATED
            else "DRC backend execution failed"
        ),
    )


def lvs_evidence_from_report(
    text: str,
    *,
    primary: str,
    layout: CheckedLayoutIdentity,
    source: CheckedSourceIdentity,
    backend: str,
    exit_code: int,
) -> LvsEvidence:
    """Project the canonical LVS parser result into typed completion evidence."""

    parsed = parse_lvs_report(text, primary=primary)
    status = (
        PhysicalVerificationStatus.EXECUTION_FAILED
        if exit_code != 0
        else PhysicalVerificationStatus.CLEAN
        if parsed["passed"]
        else PhysicalVerificationStatus.VIOLATED
    )
    mismatches = (
        ()
        if parsed["passed"]
        else (LvsMismatch(str(parsed["comparison_result"]), 1),)
    )
    return LvsEvidence(
        status=status,
        layout=layout,
        source=source,
        completion=VerificationCompletion(
            backend=backend,
            executed=True,
            report_parsed=True,
            exit_code=exit_code,
        ),
        mismatches=mismatches,
        message=(
            "LVS report is clean"
            if status is PhysicalVerificationStatus.CLEAN
            else "LVS report contains mismatches"
            if status is PhysicalVerificationStatus.VIOLATED
            else "LVS backend execution failed"
        ),
    )


def _required_qualifier(
    context: ActionContext,
    role: str,
    name: str,
) -> str:
    value = context.input(role).qualifiers.get(name)
    if not isinstance(value, str) or not value:
        raise FlowExecutionError(
            f"{role} artifact requires string qualifier {name!r}"
        )
    return value


def _check_qualifiers(
    context: ActionContext,
    role: str,
    expected: Mapping[str, str],
) -> None:
    for name, value in expected.items():
        if _required_qualifier(context, role, name) != value:
            raise FlowExecutionError(
                f"{role} artifact qualifier {name!r} disagrees with checked identity"
            )


def _validate_source_structure(path: Path, name: str) -> None:
    text = read_nofollow_text(path)
    declarations = {
        match.group("name")
        for match in re.finditer(
            r"(?im)^\s*\.?subckt\s+(?P<name>[^\s]+)",
            text,
        )
    }
    if name not in declarations:
        raise FlowExecutionError(
            f"canonical source has no declared subcircuit {name!r}"
        )


def load_receipt_bound_layout_source_inputs(
    context: ActionContext,
    *,
    require_source: bool,
) -> ReceiptBoundLayoutSourceInputs:
    """Close receipt, GDSII, source, plan, result, and job identity."""

    receipt_artifact = context.input("receipt")
    layout_artifact = context.input("layout")
    if receipt_artifact.producer != layout_artifact.producer:
        raise FlowExecutionError(
            "layout and Materialization Receipt must have the same producer"
        )
    try:
        receipt_text = receipt_artifact.path.read_text(encoding="utf-8")
        receipt = materialization_receipt_from_json(receipt_text)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise FlowExecutionError(f"invalid Materialization Receipt: {exc}") from exc
    receipt_identity = materialization_receipt_id(receipt)
    if canonical_json(receipt) != receipt_text:
        raise FlowExecutionError(
            "Materialization Receipt file is not canonical typed JSON"
        )
    validation = validate_receipt_bound_layout(
        receipt,
        layout_artifact.path,
        run_root=context.run_root,
    )
    if not validation.valid:
        raise FlowExecutionError(
            "receipt-bound layout failed validation: "
            + "; ".join(issue.code for issue in validation.issues)
        )
    assert receipt.layout is not None
    common = {
        "owner": receipt.target.owner,
        "name": receipt.target.name,
        "format": receipt.target.format.value,
        "job-identity": receipt.provenance.job_identity,
        "result-identity": receipt.provenance.result_identity,
        "plan-identity": receipt.provenance.plan_identity,
        "receipt-identity": receipt_identity,
        "status": receipt.status.value,
        "backend": receipt.completion.backend,
    }
    _check_qualifiers(context, "receipt", common)
    _check_qualifiers(
        context,
        "layout",
        {**common, "layout-identity": receipt.layout.content_identity},
    )
    layout = CheckedLayoutIdentity(
        artifact_identity=receipt.layout.content_identity,
        plan_identity=receipt.provenance.plan_identity,
        result_identity=receipt.provenance.result_identity,
        owner=receipt.target.owner,
        name=receipt.target.name,
        receipt_identity=receipt_identity,
        job_identity=receipt.provenance.job_identity,
        format=receipt.target.format.value,
    )

    source = None
    source_path = None
    if require_source:
        source_artifact = context.input("source")
        source_identity = _required_qualifier(
            context, "source", "source-identity"
        )
        source_owner = _required_qualifier(context, "source", "owner")
        source_name = _required_qualifier(context, "source", "name")
        _validate_source_structure(source_artifact.path, source_name)
        _check_qualifiers(
            context,
            "source",
            {
                "source-identity": source_identity,
                "owner": receipt.target.owner,
                "name": receipt.target.name,
            },
        )
        source = CheckedSourceIdentity(
            artifact_identity=source_identity,
            owner=source_owner,
            name=source_name,
        )
        source_path = source_artifact.path

    return ReceiptBoundLayoutSourceInputs(
        receipt=receipt,
        receipt_identity=receipt_identity,
        layout=layout,
        layout_path=layout_artifact.path,
        source=source,
        source_path=source_path,
    )


def load_receipt_bound_verification_inputs(
    context: ActionContext,
) -> ReceiptBoundVerificationInputs:
    """Close receipt-bound physical identity and the owner verification policy."""

    if context.action.kind not in {DRC_ACTION, LVS_ACTION}:
        raise FlowExecutionError(
            "receipt-bound verification requires a DRC or LVS Action"
        )
    base = load_receipt_bound_layout_source_inputs(
        context,
        require_source=context.action.kind == LVS_ACTION,
    )

    policy_artifact = context.input("verification-policy")
    policy_identity = _required_qualifier(
        context, "verification-policy", "policy-identity"
    )
    _check_qualifiers(
        context,
        "verification-policy",
        {
            "owner": base.receipt.target.owner,
            "policy-identity": policy_identity,
        },
    )
    try:
        policy = load_physical_verification_policy(
            policy_artifact.path,
            owner=base.receipt.target.owner,
        )
    except (OSError, ValueError, TypeError) as exc:
        raise FlowExecutionError(
            f"invalid physical-verification policy: {exc}"
        ) from exc
    return ReceiptBoundVerificationInputs(
        receipt=base.receipt,
        receipt_identity=base.receipt_identity,
        layout=base.layout,
        layout_path=base.layout_path,
        source=base.source,
        source_path=base.source_path,
        policy=policy,
    )


def _find_executable(name: str, explicit: Path | None, candidates: Sequence[Path]) -> Path:
    values = ([explicit] if explicit is not None else []) + list(candidates)
    discovered = shutil.which(name)
    if discovered:
        values.append(Path(discovered))
    for value in values:
        if value is None:
            continue
        absolute = Path(os.path.abspath(value))
        if absolute.is_file() and os.access(absolute, os.X_OK):
            return absolute
    raise FileNotFoundError(f"{name} was not found; configure it in the PDK layout table")



def find_calibre(explicit: Path | None = None) -> Path:
    candidates = []
    for variable in ("CALIBRE_HOME", "MGC_HOME"):
        home = os.environ.get(variable)
        if home:
            candidates.append(Path(home) / "bin" / "calibre")
    return _find_executable("calibre", explicit, candidates)


def _prepend(environment: dict[str, str], name: str, value: Path) -> None:
    existing = environment.get(name, "")
    environment[name] = str(value) + (os.pathsep + existing if existing else "")


def calibre_environment(executable: Path) -> dict[str, str]:
    """Construct the bounded Calibre runtime environment for a resolved binary."""

    environment = dict(os.environ)
    home = executable.parent.parent
    environment["CALIBRE_HOME"] = str(home)
    environment["MGC_HOME"] = str(home)
    environment["MGC_LIB_PATH"] = str(home / "lib")
    environment["USE_CALIBRE_VCO"] = "aok"
    client_lib = home / "shared" / "pkgs" / "icv" / "tools" / "calibre_client" / "lib" / "64"
    if client_lib.is_dir():
        _prepend(environment, "LD_LIBRARY_PATH", client_lib)
    return environment



def run_calibre_verification(
    record: RunArtifacts,
    spec: LayoutSpec,
    plan: LayoutPlan,
    check: str,
    *,
    calibre: Path,
    gds: Path,
    timeout: int,
) -> dict[str, object]:
    policy = spec.physical_verification
    if policy is None:
        raise ValueError(
            "physical verification requires an owner policy selected by the OA assembly"
        )
    source_deck = spec.layout_pdk.drc_deck if check == "drc" else spec.layout_pdk.lvs_deck
    staged_source_deck = record.copy_file(
        "inputs",
        ("foundry.drc" if check == "drc" else "foundry.lvs",),
        source_deck,
    )
    source_text = read_nofollow_text(staged_source_deck)
    source_cdl: Path | None = None
    if check == "lvs":
        for index, snapshot in enumerate(spec.source_snapshots):
            record.write_text(
                "inputs",
                ("canonical-source", f"{index:02d}-{snapshot.source_path.name}"),
                snapshot.text,
            )
        record.write_json(
            "inputs",
            ("canonical-source", "provenance.json"),
            {
                "top": spec.cell,
                "sources": [
                    snapshot.source_path.relative_to(spec.project_root).as_posix()
                    for snapshot in spec.source_snapshots
                ],
            },
        )
        source_cdl = record.write_text(
            "inputs",
            ("source.cdl",),
            render_canonical_source_cdl(spec),
        )
        source_cdl.chmod(0o444)

    work = record.directory("work")
    results = record.directory("outputs")
    canonical_deck = (
        render_drc_run_deck(
            source_text,
            layout_path=str(gds),
            primary=spec.cell,
            results_path=str(results / "drc-results.db"),
            summary_path=str(results / "drc-summary.rep"),
            disabled_defines=policy.drc_disabled_defines,
        )
        if check == "drc"
        else render_lvs_run_deck(
            source_text,
            layout_path=str(gds),
            source_path=str(source_cdl),
            primary=spec.cell,
            work_dir=str(results),
        )
    )
    record.write_text(
        "inputs", (f"run.{check}",), canonical_deck
    )

    with owned_directory(work) as owned_work, ExitStack() as resources:
        owned_gds = resources.enter_context(owned_input_file(gds))
        owned_source = (
            resources.enter_context(owned_input_file(source_cdl))
            if source_cdl is not None
            else None
        )
        invocation_deck = (
            render_drc_run_deck(
                source_text,
                layout_path=owned_gds.child_named_path,
                primary=spec.cell,
                results_path=str(work / "drc-results.db"),
                summary_path=str(work / "drc-summary.rep"),
                disabled_defines=policy.drc_disabled_defines,
            )
            if check == "drc"
            else render_lvs_run_deck(
                source_text,
                layout_path=owned_gds.child_named_path,
                source_path=owned_source.child_named_path if owned_source else "",
                primary=spec.cell,
                work_dir=str(work),
            )
        )
        invocation_path = record.write_text(
            "work",
            (f"run.tool.{check}",),
            invocation_deck,
        )
        invocation_path.chmod(0o444)
        owned_deck = resources.enter_context(owned_input_file(invocation_path))
        command = (str(calibre), f"-{check}", "-hier", owned_deck.child_named_path)
        record.write_json(
            "inputs",
            ("calibre-command.json",),
            {"argv": list(command), "cwd": str(work), "timeout_seconds": timeout},
        )

        def validate_spawn() -> None:
            owned_gds.require_visible()
            owned_deck.require_visible()
            if owned_source is not None:
                owned_source.require_visible()

        pass_fds = [
            owned_work.fd,
            owned_gds.fd,
            owned_gds.directory_fd,
            owned_deck.fd,
            owned_deck.directory_fd,
        ]
        if owned_source is not None:
            pass_fds.extend((owned_source.fd, owned_source.directory_fd))
        completed = run_process_group(
            command,
            cwd=work,
            env=calibre_environment(calibre),
            timeout=timeout,
            before_spawn=validate_spawn,
            pass_fds=tuple(dict.fromkeys(pass_fds)),
        )
    record.write_text(
        "logs",
        (f"calibre-{check}.log",),
        completed.stdout,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Calibre {check.upper()} exited {completed.returncode}")

    if check == "drc":
        outputs = (
            "drc-results.db",
            "drc-summary.rep",
        )
        copied = {
            name: record.copy_file("outputs", (name,), work / name)
            for name in outputs
        }
        return parse_drc_summary(
            read_nofollow_text(copied["drc-summary.rep"]),
            configuration_warnings=policy.drc_configuration_warnings,
            waiver_layers=policy.drc_waiver_layers,
        )

    outputs = (
        ("lvs.rep", "lvs-report"),
        ("lvs.rep.ext", "lvs-extraction-report"),
        ("calibre_erc.db", "calibre-erc-db"),
        ("calibre_erc.sum", "calibre-erc-summary"),
    )
    copied_lvs: dict[str, Path] = {}
    for source_name, result_name in outputs:
        candidate = work / source_name
        if not candidate.is_file():
            raise RuntimeError(f"Calibre LVS did not produce {source_name}")
        copied_lvs[source_name] = record.copy_file(
            "outputs", (result_name,), candidate
        )
    extracted = work / "svdb" / f"{spec.cell}.sp"
    if not extracted.is_file():
        raise RuntimeError("Calibre LVS did not produce the extracted layout netlist")
    record.copy_file(
        "outputs", ("extracted.sp",), extracted
    )
    result = parse_lvs_report(read_nofollow_text(copied_lvs["lvs.rep"]), primary=spec.cell)
    if result["passed"] and "LVS completed. CORRECT." not in completed.stdout:
        raise RuntimeError("Calibre log does not independently confirm correct LVS completion")
    return result


_CALIBRE_PRIMARY = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")


def _verification_values(
    evidence: DrcEvidence | LvsEvidence,
) -> dict[str, object]:
    prefix = "drc" if isinstance(evidence, DrcEvidence) else "lvs"
    return {
        f"{prefix}-status": evidence.status.value,
        f"{prefix}-clean": evidence.clean,
        f"{prefix}-completed": evidence.completion.proven,
    }


def _nonconclusive_verification_evidence(
    inputs: ReceiptBoundVerificationInputs,
    *,
    check: str,
    backend: str,
    status: PhysicalVerificationStatus,
    executed: bool,
    exit_code: int | None,
    message: str,
) -> DrcEvidence | LvsEvidence:
    completion = VerificationCompletion(
        backend=backend,
        executed=executed,
        report_parsed=False,
        exit_code=exit_code,
    )
    if check == "drc":
        return DrcEvidence(status, inputs.layout, completion, (), message)
    assert inputs.source is not None
    return LvsEvidence(
        status,
        inputs.layout,
        inputs.source,
        completion,
        (),
        message,
    )


def copy_regular_backend_output(source: Path, destination: Path, label: str) -> Path:
    """Copy one tool output only when it is a non-symlink regular file."""

    try:
        metadata = source.lstat()
    except OSError as exc:
        raise RuntimeError(f"Calibre did not produce {label}") from exc
    if not stat.S_ISREG(metadata.st_mode) or source.is_symlink():
        raise RuntimeError(f"Calibre {label} is not a regular file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    return destination


class CalibrePhysicalVerificationAdapter:
    """Run receipt-bound GDSII DRC/LVS and project authoritative reports."""

    def run(self, context: ActionContext) -> AdapterResult:
        return complete_staged_run(
            context,
            validate_inputs=self._validate_inputs,
            prepare=self._prepare,
            execute=self._execute,
            collect_result=self._collect_result,
        )

    def _configuration(self, context: ActionContext) -> int:
        if context.action_config:
            raise FlowExecutionError(
                "Calibre physical-verification Action config must be empty"
            )
        if set(context.adapter_config) - {"timeout_seconds"}:
            raise FlowExecutionError(
                "Calibre physical-verification Adapter config accepts only "
                "'timeout_seconds'"
            )
        timeout = context.adapter_config.get("timeout_seconds", 600)
        if type(timeout) is not int or timeout <= 0:
            raise FlowExecutionError(
                "Calibre physical-verification timeout must be a positive integer"
            )
        return timeout

    def _resources(self, context: ActionContext) -> tuple[str, Path, Path]:
        capability = context.capabilities.get("tool.calibre")
        if capability is None or capability.executable is None:
            raise FlowExecutionError(
                "Calibre capability requires a resolved executable"
            )
        executable = capability.executable
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise FlowExecutionError("resolved Calibre executable is unavailable")
        asset = context.platform_assets.get("physical-verification")
        if asset is None or asset.kind != "platform.calibre-verification":
            raise FlowExecutionError(
                "Calibre requires a resolved physical-verification deck view"
            )
        member_role = "drc-deck" if context.action.kind == DRC_ACTION else "lvs-deck"
        member = asset.member(member_role)
        if member is None or not member.location.is_file():
            raise FlowExecutionError(
                f"physical-verification deck view omitted {member_role!r}"
            )
        return capability.identity, executable, member.location

    def _validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        diagnostics: list[str] = []
        try:
            inputs = load_receipt_bound_verification_inputs(context)
            if _CALIBRE_PRIMARY.fullmatch(inputs.receipt.target.name) is None:
                raise FlowExecutionError(
                    "materialization target is not a legal Calibre primary name"
                )
            self._configuration(context)
            self._resources(context)
        except (FlowExecutionError, OSError, ValueError, TypeError) as exc:
            diagnostics.append(str(exc))
        return tuple(diagnostics)

    def _prepare(self, context: ActionContext) -> None:
        (context.output_root / "reports").mkdir()

    def _invoke(
        self,
        context: ActionContext,
        inputs: ReceiptBoundVerificationInputs,
        *,
        executable: Path,
        deck_path: Path,
        timeout: int,
    ):
        check = "drc" if context.action.kind == DRC_ACTION else "lvs"
        work = context.work_root
        source_text = read_nofollow_text(deck_path)
        with owned_directory(work) as owned_work, ExitStack() as resources:
            owned_layout = resources.enter_context(
                owned_input_file(inputs.layout_path)
            )
            owned_source = (
                resources.enter_context(owned_input_file(inputs.source_path))
                if inputs.source_path is not None
                else None
            )
            invocation = (
                render_drc_run_deck(
                    source_text,
                    layout_path=owned_layout.child_named_path,
                    primary=inputs.receipt.target.name,
                    results_path=str(work / "drc-results.db"),
                    summary_path=str(work / "drc-summary.rep"),
                    disabled_defines=inputs.policy.drc_disabled_defines,
                )
                if check == "drc"
                else render_lvs_run_deck(
                    source_text,
                    layout_path=owned_layout.child_named_path,
                    source_path=(
                        owned_source.child_named_path if owned_source is not None else ""
                    ),
                    primary=inputs.receipt.target.name,
                    work_dir=str(work),
                )
            )
            invocation_path = work / f"run.tool.{check}"
            invocation_path.write_text(invocation, encoding="utf-8")
            invocation_path.chmod(0o444)
            owned_deck = resources.enter_context(owned_input_file(invocation_path))
            command = (
                str(executable),
                f"-{check}",
                "-hier",
                owned_deck.child_named_path,
            )
            atomic_write_json(
                work / "calibre-command.json",
                {
                    "argv": list(command),
                    "cwd": str(work),
                    "timeout_seconds": timeout,
                },
            )

            def validate_spawn() -> None:
                owned_layout.require_visible()
                owned_deck.require_visible()
                if owned_source is not None:
                    owned_source.require_visible()

            pass_fds = [
                owned_work.fd,
                owned_layout.fd,
                owned_layout.directory_fd,
                owned_deck.fd,
                owned_deck.directory_fd,
            ]
            if owned_source is not None:
                pass_fds.extend((owned_source.fd, owned_source.directory_fd))
            return run_process_group(
                command,
                cwd=work,
                env=calibre_environment(executable),
                timeout=timeout,
                before_spawn=validate_spawn,
                pass_fds=tuple(dict.fromkeys(pass_fds)),
            )

    def _parsed_evidence(
        self,
        context: ActionContext,
        inputs: ReceiptBoundVerificationInputs,
        *,
        backend: str,
        stdout: str,
    ) -> DrcEvidence | LvsEvidence:
        reports = context.output_root / "reports"
        work = context.work_root
        if context.action.kind == DRC_ACTION:
            copy_regular_backend_output(
                work / "drc-results.db",
                reports / "drc-results.db",
                "DRC results database",
            )
            summary = copy_regular_backend_output(
                work / "drc-summary.rep",
                reports / "drc-summary.rep",
                "DRC summary report",
            )
            return drc_evidence_from_summary(
                read_nofollow_text(summary),
                layout=inputs.layout,
                backend=backend,
                exit_code=0,
                configuration_warnings=inputs.policy.drc_configuration_warnings,
                waiver_layers=inputs.policy.drc_waiver_layers,
            )

        names = (
            ("lvs.rep", "lvs-report"),
            ("lvs.rep.ext", "lvs-extraction-report"),
            ("calibre_erc.db", "calibre-erc-db"),
            ("calibre_erc.sum", "calibre-erc-summary"),
        )
        copied = {
            source_name: copy_regular_backend_output(
                work / source_name,
                reports / output_name,
                output_name,
            )
            for source_name, output_name in names
        }
        extracted = work / "svdb" / f"{inputs.receipt.target.name}.sp"
        copy_regular_backend_output(
            extracted,
            reports / "extracted.sp",
            "extracted layout netlist",
        )
        assert inputs.source is not None
        evidence = lvs_evidence_from_report(
            read_nofollow_text(copied["lvs.rep"]),
            primary=inputs.receipt.target.name,
            layout=inputs.layout,
            source=inputs.source,
            backend=backend,
            exit_code=0,
        )
        if evidence.clean and "LVS completed. CORRECT." not in stdout:
            raise RuntimeError(
                "Calibre log does not independently confirm correct LVS completion"
            )
        return evidence

    def _execute(self, context: ActionContext) -> AdapterExecution:
        inputs = load_receipt_bound_verification_inputs(context)
        timeout = self._configuration(context)
        backend, executable, deck = self._resources(context)
        check = "drc" if context.action.kind == DRC_ACTION else "lvs"
        try:
            completed = self._invoke(
                context,
                inputs,
                executable=executable,
                deck_path=deck,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            evidence = _nonconclusive_verification_evidence(
                inputs,
                check=check,
                backend=backend,
                status=PhysicalVerificationStatus.BACKEND_UNAVAILABLE,
                executed=False,
                exit_code=None,
                message=f"Calibre backend unavailable: {exc}",
            )
        except Exception as exc:
            evidence = _nonconclusive_verification_evidence(
                inputs,
                check=check,
                backend=backend,
                status=PhysicalVerificationStatus.EXECUTION_FAILED,
                executed=True,
                exit_code=None,
                message=f"Calibre execution failed: {exc}",
            )
        else:
            log_path = context.log_root / f"calibre-{check}.log"
            log_path.write_text(completed.stdout, encoding="utf-8")
            if completed.returncode != 0:
                evidence = _nonconclusive_verification_evidence(
                    inputs,
                    check=check,
                    backend=backend,
                    status=PhysicalVerificationStatus.EXECUTION_FAILED,
                    executed=True,
                    exit_code=completed.returncode,
                    message=f"Calibre {check.upper()} exited {completed.returncode}",
                )
            else:
                try:
                    evidence = self._parsed_evidence(
                        context,
                        inputs,
                        backend=backend,
                        stdout=completed.stdout,
                    )
                except (OSError, RuntimeError, ValueError, TypeError) as exc:
                    evidence = _nonconclusive_verification_evidence(
                        inputs,
                        check=check,
                        backend=backend,
                        status=PhysicalVerificationStatus.EXECUTION_FAILED,
                        executed=True,
                        exit_code=0,
                        message=f"Calibre report collection failed: {exc}",
                    )
        evidence_path = context.output_path(
            "evidence",
            "drc-evidence.json" if isinstance(evidence, DrcEvidence) else "lvs-evidence.json",
        )
        evidence_path.write_text(evidence.canonical_json(), encoding="utf-8")
        return AdapterExecution.succeeded()

    def _collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        inputs = load_receipt_bound_verification_inputs(context)
        is_drc = context.action.kind == DRC_ACTION
        path = context.output_path(
            "evidence", "drc-evidence.json" if is_drc else "lvs-evidence.json"
        )
        try:
            evidence = (
                drc_evidence_from_json(path.read_text(encoding="utf-8"))
                if is_drc
                else lvs_evidence_from_json(path.read_text(encoding="utf-8"))
            )
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise FlowExecutionError(
                f"invalid receipt-bound physical-verification evidence: {exc}"
            ) from exc
        if evidence.layout != inputs.layout:
            raise FlowExecutionError(
                "physical-verification evidence changed the checked layout identity"
            )
        if isinstance(evidence, LvsEvidence) and evidence.source != inputs.source:
            raise FlowExecutionError(
                "LVS evidence changed the checked source identity"
            )
        evidence_identity = (
            drc_evidence_id(evidence)
            if isinstance(evidence, DrcEvidence)
            else lvs_evidence_id(evidence)
        )
        facts = context.action.fact_schema.project(
            _verification_values(evidence),
            source=FactSource(context.action.kind, context.node_id, evidence_identity),
        )
        qualifiers = {
            "owner": inputs.layout.owner,
            "name": inputs.layout.name,
            "layout-identity": inputs.layout.artifact_identity,
            "receipt-identity": inputs.receipt_identity,
            "job-identity": str(inputs.layout.job_identity),
            "result-identity": str(inputs.layout.result_identity),
            "plan-identity": inputs.layout.plan_identity,
            "status": evidence.status.value,
            "backend": evidence.completion.backend,
        }
        if isinstance(evidence, LvsEvidence):
            qualifiers["source-identity"] = evidence.source.artifact_identity
        reports = context.output_root / "reports"
        report_evidence = tuple(
            path
            for path in (
                context.log_root / (
                    "calibre-drc.log" if is_drc else "calibre-lvs.log"
                ),
                *(sorted(reports.iterdir()) if reports.is_dir() else ()),
            )
            if path.is_file()
        )
        return CollectedActionResult(
            status="valid",
            artifacts=(
                ProducedArtifact(
                    "evidence",
                    DRC_EVIDENCE_KIND if is_drc else LVS_EVIDENCE_KIND,
                    path,
                    qualifiers={
                        **qualifiers,
                        "evidence-identity": (
                            drc_evidence_id(evidence)
                            if isinstance(evidence, DrcEvidence)
                            else lvs_evidence_id(evidence)
                        ),
                    },
                ),
            ),
            facts=facts,
            evidence=report_evidence,
        )
