"""Direct XStream and Calibre verification of one source-planned OA layout."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
from pathlib import Path
import os
import re
from typing import Any, Callable, Mapping, Sequence

from sigilicon.artifacts import read_nofollow_text
from sigilicon.domain.netlist import render_canonical_cdl, resolve_netlist_hierarchy
from sigilicon.domain.physical_verification import (
    CheckedLayoutIdentity,
    CheckedSourceIdentity,
    DrcEvidence,
    DrcViolation,
    LvsEvidence,
    LvsMismatch,
    PhysicalVerificationEvidence,
    PhysicalVerificationStatus,
    VerificationCompletion,
)
from sigilicon.external_tools import (
    ProcessRequest,
    cadence_subprocess_env,
    managed_process,
    owned_directory,
    owned_input_file,
)
from sigilicon.execution._model import Resources
from sigilicon.layout.ir import LayoutPlan
from sigilicon.layout.spec import LayoutSpec
from sigilicon.virtuoso.layout_generation import validate_layout_plan
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation
from sigilicon.virtuoso.xstream import (
    XStreamExportError,
    XStreamExportRequest,
    run_xstream_export,
)
from sigilicon.workflows.layout_generation import LayoutPlanningResult
from sigilicon.execution._workspace import ExecutionWorkspace


_DRC_RESULT = re.compile(
    r"^RULECHECK (?P<name>.+?) \.+ TOTAL Result Count = (?P<count>\d+)\s+\(\d+\)$",
    re.MULTILINE,
)
_ORIGINAL_LAYER = re.compile(
    r"^LAYER (?P<name>\S+) \.+ TOTAL Original Geometry Count = "
    r"(?P<count>\d+)\s+\(\d+\)$",
    re.MULTILINE,
)
_ADAPTER = "cadence.xstream+calibre"


@dataclass(frozen=True)
class LayoutVerificationResult:
    """One conclusive or nonconclusive physical-verification result."""

    passed: bool
    evidence: PhysicalVerificationEvidence


def _replace_exact(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(
            f"{label} changed; expected one exact occurrence, got {count}"
        )
    return text.replace(old, new, 1)


def _disable_define(text: str, name: str, *, expected: int) -> str:
    pattern = re.compile(
        rf"^#DEFINE[ \t]+{re.escape(name)}(?P<tail>[ \t].*)?$",
        re.MULTILINE,
    )
    result, count = pattern.subn(r"//#DEFINE " + name + r"\g<tail>", text)
    if count != expected:
        raise RuntimeError(
            f"Calibre DRC option {name} changed; expected {expected} "
            f"enabled definition(s), got {count}"
        )
    return result


def render_drc_run_deck(
    source: str,
    *,
    layout_path: str,
    primary: str,
    results_path: str,
    summary_path: str,
    disabled_defines: Mapping[str, int],
) -> str:
    """Resolve the foundry macro-cell profile without introducing waivers."""

    result = source
    for name, expected in disabled_defines.items():
        result = _disable_define(result, name, expected=expected)
    for old, new, label in (
        ('LAYOUT PATH "GDSFILENAME"', f'LAYOUT PATH "{layout_path}"', "DRC layout path"),
        ('LAYOUT PRIMARY "TOPCELLNAME"', f'LAYOUT PRIMARY "{primary}"', "DRC primary"),
        (
            'DRC RESULTS DATABASE "DRC_RES.db"',
            f'DRC RESULTS DATABASE "{results_path}"',
            "DRC results",
        ),
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
    """Render LVS source only from the layout spec's canonical source closure."""

    hierarchy = resolve_netlist_hierarchy(
        spec.source_snapshots,
        top=spec.cell,
        primitive_masters=spec.primitive_masters,
    )
    if hierarchy.definitions[spec.cell].ports != spec.ports:
        raise RuntimeError("resolved LVS hierarchy changed the canonical top interface")
    primitive_interfaces: list[str] = []
    for master in sorted(hierarchy.primitive_counts):
        terminals = spec.pdk.oa.primitive_subcircuits.get(master)
        if terminals is not None:
            primitive_interfaces.extend(
                (f".SUBCKT {master} {' '.join(terminals)}", f".ENDS {master}", "")
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
    """Parse the foundry summary without treating configured warnings as DRC."""

    warning_rules = frozenset(configuration_warnings)
    waived_layers = frozenset(waiver_layers)
    counts = {
        match["name"]: int(match["count"]) for match in _DRC_RESULT.finditer(text)
    }
    if not counts:
        raise RuntimeError("Calibre DRC summary contains no rulecheck totals")
    total_match = re.search(
        r"^TOTAL DRC Results Generated:\s+(\d+) \(\d+\)$",
        text,
        re.MULTILINE,
    )
    if total_match is None:
        raise RuntimeError("Calibre DRC summary has no total result count")
    total = int(total_match.group(1))
    warning_count = sum(counts.get(name, 0) for name in warning_rules)
    violation_count = sum(
        count for name, count in counts.items() if name not in warning_rules
    )
    if total not in {violation_count, violation_count + warning_count}:
        raise RuntimeError("Calibre DRC per-rule counts do not match the total")
    layers = {
        match["name"]: int(match["count"])
        for match in _ORIGINAL_LAYER.finditer(text)
    }
    missing_layers = waived_layers - set(layers)
    if missing_layers:
        raise RuntimeError(
            "Calibre DRC summary omitted waiver-layer statistics: "
            + ", ".join(sorted(missing_layers))
        )
    used_layers = tuple(sorted(name for name in waived_layers if layers[name]))
    return {
        "passed": violation_count == 0 and not used_layers,
        "result_count": total,
        "configuration_warning_count": warning_count,
        "violation_count": violation_count,
        "used_waiver_layers": used_layers,
        "nonzero_rulechecks": {
            name: count for name, count in sorted(counts.items()) if count
        },
    }


def parse_lvs_report(text: str, *, primary: str) -> dict[str, object]:
    match = re.search(
        rf"^\s*(CORRECT|INCORRECT|NOT COMPARED)\s+"
        rf"{re.escape(primary)}\s+{re.escape(primary)}\s*$",
        text,
        re.MULTILINE,
    )
    if match is None:
        raise RuntimeError("Calibre LVS report has no top-cell comparison result")
    comparison = match.group(1)
    return {"passed": comparison == "CORRECT", "comparison_result": comparison}


def _sha256(value: bytes) -> str:
    return "sha256-" + hashlib.sha256(value).hexdigest()


def _layout_identity(
    spec: LayoutSpec,
    plan: LayoutPlan,
    gds: Path,
) -> CheckedLayoutIdentity:
    return CheckedLayoutIdentity(
        artifact_identity=_sha256(gds.read_bytes()),
        plan_identity=_sha256(plan.canonical_json().encode("utf-8")),
        result_identity=None,
        owner=spec.library,
        name=spec.cell,
        format="gdsii",
    )


def _source_identity(spec: LayoutSpec) -> CheckedSourceIdentity:
    return CheckedSourceIdentity(
        artifact_identity=_sha256(render_canonical_source_cdl(spec).encode("utf-8")),
        owner=spec.library,
        name=spec.cell,
    )


def _failed_evidence(
    spec: LayoutSpec,
    plan: LayoutPlan,
    gds: Path,
    *,
    check: str,
    exit_code: int | None,
    message: str,
) -> PhysicalVerificationEvidence:
    completion = VerificationCompletion(_ADAPTER, True, False, exit_code)
    if check == "drc":
        return DrcEvidence(
            PhysicalVerificationStatus.EXECUTION_FAILED,
            _layout_identity(spec, plan, gds),
            completion,
            (),
            message,
        )
    return LvsEvidence(
        PhysicalVerificationStatus.EXECUTION_FAILED,
        _layout_identity(spec, plan, gds),
        _source_identity(spec),
        completion,
        (),
        message,
    )


def _parsed_evidence(
    spec: LayoutSpec,
    plan: LayoutPlan,
    gds: Path,
    *,
    check: str,
    report: str,
) -> PhysicalVerificationEvidence:
    completion = VerificationCompletion(_ADAPTER, True, True, 0)
    if check == "drc":
        assert spec.physical_verification is not None
        parsed = parse_drc_summary(
            report,
            configuration_warnings=(
                spec.physical_verification.drc_configuration_warnings
            ),
            waiver_layers=spec.physical_verification.drc_waiver_layers,
        )
        warnings = frozenset(
            spec.physical_verification.drc_configuration_warnings
        )
        violations = tuple(
            DrcViolation(str(rule), int(count))
            for rule, count in parsed["nonzero_rulechecks"].items()
            if rule not in warnings
        ) + tuple(
            DrcViolation(f"waiver-layer:{layer}", 1)
            for layer in parsed["used_waiver_layers"]
        )
        status = (
            PhysicalVerificationStatus.CLEAN
            if parsed["passed"]
            else PhysicalVerificationStatus.VIOLATED
        )
        return DrcEvidence(
            status,
            _layout_identity(spec, plan, gds),
            completion,
            violations,
            "DRC report is clean"
            if status is PhysicalVerificationStatus.CLEAN
            else "DRC report contains violations",
        )
    parsed = parse_lvs_report(report, primary=spec.cell)
    mismatches = (
        ()
        if parsed["passed"]
        else (LvsMismatch(str(parsed["comparison_result"]), 1),)
    )
    status = (
        PhysicalVerificationStatus.CLEAN
        if parsed["passed"]
        else PhysicalVerificationStatus.VIOLATED
    )
    return LvsEvidence(
        status,
        _layout_identity(spec, plan, gds),
        _source_identity(spec),
        completion,
        mismatches,
        "LVS report is clean"
        if status is PhysicalVerificationStatus.CLEAN
        else "LVS report contains mismatches",
    )


def calibre_environment(
    executable: Path,
    base: Mapping[str, str],
) -> dict[str, str]:
    """Build Calibre's environment from the preflighted resource snapshot."""

    environment = cadence_subprocess_env(base)
    home = executable.parent.parent
    environment["CALIBRE_HOME"] = str(home)
    environment["MGC_HOME"] = str(home)
    environment["MGC_LIB_PATH"] = str(home / "lib")
    environment["USE_CALIBRE_VCO"] = "aok"
    client_lib = home / "shared/pkgs/icv/tools/calibre_client/lib/64"
    if client_lib.is_dir():
        current = environment.get("LD_LIBRARY_PATH", "")
        environment["LD_LIBRARY_PATH"] = str(client_lib) + (
            os.pathsep + current if current else ""
        )
    return environment


def _copy_regular_outputs(
    record: ExecutionWorkspace,
    work: Path,
    entries: Sequence[tuple[str, str]],
) -> dict[str, Path]:
    copied: dict[str, Path] = {}
    for source_name, result_name in entries:
        candidate = work / source_name
        if not candidate.is_file() or candidate.is_symlink():
            raise RuntimeError(f"Calibre did not produce regular {source_name}")
        copied[source_name] = record.copy_file("outputs", (result_name,), candidate)
    return copied


def _run_xstream(
    record: ExecutionWorkspace,
    spec: LayoutSpec,
    *,
    layermap_source: str,
    resources: Resources,
    timeout: int,
) -> Path:
    layermap = record.write_text("inputs", ("layermap",), layermap_source)
    cds_lib = spec.project.workspace_root / "cds.lib"
    if not cds_lib.is_file() or cds_lib.is_symlink():
        raise RuntimeError(f"workspace cds.lib is unavailable: {cds_lib}")
    work = record.directory("work")
    try:
        with resources.owned_tool("cadence.xstream") as launcher:
            exported = run_xstream_export(
                XStreamExportRequest(
                    library=spec.library,
                    cell=spec.cell,
                    view=spec.view,
                    technology_library=spec.pdk.oa.technology_library,
                    layer_map=layermap,
                    cds_lib=cds_lib,
                    work_root=work,
                    timeout_seconds=timeout,
                    flatten_pcells=spec.layout_pdk.xstream_flatten_pcells,
                    suppressed_warnings=spec.layout_pdk.xstream_suppressed_warnings,
                ),
                launcher=launcher,
                environment=resources.environment,
            )
    except XStreamExportError as exc:
        for name in ("strmout.log", "strmout.sum"):
            candidate = work / name
            if candidate.is_file() and not candidate.is_symlink():
                record.copy_file("outputs", (name,), candidate)
        if exc.diagnostic_path is not None and exc.diagnostic_path.is_file():
            record.copy_file(
                "outputs", ("xstream-failure.log",), exc.diagnostic_path
            )
        raise
    record.write_text("outputs", ("xstream-stdout.log",), exported.stdout)
    record.write_text("outputs", ("xstream-stderr.log",), exported.stderr)
    record.copy_file("outputs", ("strmout.log",), exported.native_log_path)
    record.copy_file("outputs", ("strmout.sum",), exported.summary_path)
    return record.copy_file("inputs", ("layout.gds",), exported.gds_path)


def _run_calibre(
    record: ExecutionWorkspace,
    spec: LayoutSpec,
    plan: LayoutPlan,
    *,
    check: str,
    deck_source: str,
    resources: Resources,
    gds: Path,
    timeout: int,
) -> PhysicalVerificationEvidence:
    assert spec.physical_verification is not None
    staged_deck = record.write_text(
        "inputs",
        ("foundry.drc" if check == "drc" else "foundry.lvs",),
        deck_source,
    )
    source_text = read_nofollow_text(staged_deck)
    source_cdl: Path | None = None
    if check == "lvs":
        source_cdl = record.write_text(
            "inputs", ("source.cdl",), render_canonical_source_cdl(spec)
        )
    work = record.directory("work")
    canonical_deck = (
        render_drc_run_deck(
            source_text,
            layout_path=str(gds),
            primary=spec.cell,
            results_path=str(record.output_root / "drc-results.db"),
            summary_path=str(record.output_root / "drc-summary.rep"),
            disabled_defines=spec.physical_verification.drc_disabled_defines,
        )
        if check == "drc"
        else render_lvs_run_deck(
            source_text,
            layout_path=str(gds),
            source_path=str(source_cdl),
            primary=spec.cell,
            work_dir=str(record.output_root),
        )
    )
    record.write_text("inputs", (f"run.{check}",), canonical_deck)

    executable = resources.require_tool("mentor.calibre")
    with (
        resources.owned_tool("mentor.calibre") as owned_launcher,
        owned_directory(work) as owned_work,
        ExitStack() as held_inputs,
    ):
        child_work = Path(owned_work.child_path)
        owned_gds = held_inputs.enter_context(owned_input_file(gds))
        owned_source = (
            held_inputs.enter_context(owned_input_file(source_cdl))
            if source_cdl is not None
            else None
        )
        invocation = (
            render_drc_run_deck(
                source_text,
                layout_path=owned_gds.child_named_path,
                primary=spec.cell,
                results_path=str(child_work / "drc-results.db"),
                summary_path=str(child_work / "drc-summary.rep"),
                disabled_defines=spec.physical_verification.drc_disabled_defines,
            )
            if check == "drc"
            else render_lvs_run_deck(
                source_text,
                layout_path=owned_gds.child_named_path,
                source_path=owned_source.child_named_path if owned_source else "",
                primary=spec.cell,
                work_dir=str(child_work),
            )
        )
        invocation_path = record.write_text(
            "work", (f"run.tool.{check}",), invocation
        )
        owned_deck = held_inputs.enter_context(owned_input_file(invocation_path))
        command = (
            *owned_launcher.command,
            f"-{check}",
            "-hier",
            owned_deck.child_named_path,
        )
        record.write_json(
            "inputs",
            ("calibre-command.json",),
            {
                "argv": list(command),
                "cwd": str(child_work),
                "timeout_seconds": timeout,
                "plan_stage": plan.stage,
            },
        )

        def validate_spawn() -> None:
            owned_launcher.require_visible()
            owned_gds.require_visible()
            owned_deck.require_visible()
            if owned_source is not None:
                owned_source.require_visible()

        descriptors = [
            owned_work.fd,
            owned_gds.fd,
            owned_gds.directory_fd,
            owned_deck.fd,
            owned_deck.directory_fd,
        ]
        if owned_source is not None:
            descriptors.extend((owned_source.fd, owned_source.directory_fd))
        completed = managed_process.run(ProcessRequest(
            argv=tuple(command),
            executable=owned_launcher.executable,
            cwd=child_work,
            environment=calibre_environment(executable, resources.environment),
            timeout_seconds=timeout,
            before_spawn=validate_spawn,
            pass_fds=tuple(dict.fromkeys(descriptors)),
        ))
    record.write_text(
        "outputs",
        (f"calibre-{check}.log",),
        completed.stdout + completed.stderr,
    )
    if completed.returncode != 0:
        return _failed_evidence(
            spec,
            plan,
            gds,
            check=check,
            exit_code=completed.returncode,
            message=f"Calibre {check.upper()} exited {completed.returncode}",
        )
    try:
        if check == "drc":
            copied = _copy_regular_outputs(
                record,
                work,
                (
                    ("drc-results.db", "drc-results.db"),
                    ("drc-summary.rep", "drc-summary.rep"),
                ),
            )
            report = read_nofollow_text(copied["drc-summary.rep"])
        else:
            copied = _copy_regular_outputs(
                record,
                work,
                (
                    ("lvs.rep", "lvs-report"),
                    ("lvs.rep.ext", "lvs-extraction-report"),
                    ("calibre_erc.db", "calibre-erc-db"),
                    ("calibre_erc.sum", "calibre-erc-summary"),
                ),
            )
            extracted = work / "svdb" / f"{spec.cell}.sp"
            if not extracted.is_file() or extracted.is_symlink():
                raise RuntimeError("Calibre LVS omitted the extracted layout netlist")
            record.copy_file("outputs", ("extracted.sp",), extracted)
            report = read_nofollow_text(copied["lvs.rep"])
            if (
                parse_lvs_report(report, primary=spec.cell)["passed"]
                and "LVS completed. CORRECT."
                not in f"{completed.stdout}\n{completed.stderr}"
            ):
                raise RuntimeError(
                    "Calibre log does not independently confirm correct LVS completion"
                )
        return _parsed_evidence(
            spec,
            plan,
            gds,
            check=check,
            report=report,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return _failed_evidence(
            spec,
            plan,
            gds,
            check=check,
            exit_code=0,
            message=f"Calibre {check.upper()} evidence is incomplete: {exc}",
        )


def run_layout_verification(
    planning: LayoutPlanningResult,
    client: Any,
    *,
    check: str,
    artifacts: ExecutionWorkspace,
    resources: Resources,
    external_sources: Mapping[Path, str],
    operation_id: str,
    bind_operation: Callable[[Any], None],
    record_uncertainty: Callable[[str], None] | None = None,
    xstream_timeout: int = 120,
    calibre_timeout: int = 600,
) -> LayoutVerificationResult:
    """Verify one routed OA layout under one bound read-only workspace lease."""

    if check not in {"drc", "lvs"}:
        raise ValueError("layout verification check must be drc or lvs")
    spec = planning.spec
    plan = planning.plan
    if spec.physical_verification is None or spec.oa_assembly_manifest is None:
        raise ValueError(
            "layout verification requires an owner physical-verification policy"
        )
    if plan.stage != "routed":
        raise ValueError("layout verification requires a routed layout plan")
    layermap_path = spec.layout_pdk.layermap.resolve()
    deck_path = (
        spec.layout_pdk.drc_deck if check == "drc" else spec.layout_pdk.lvs_deck
    ).resolve()
    try:
        layermap_source = external_sources[layermap_path]
        deck_source = external_sources[deck_path]
    except KeyError as exc:
        raise ValueError(
            f"layout verification lacks a bound external snapshot: {exc.args[0]}"
        ) from exc
    artifacts.write_text(
        "inputs", ("layout-plan.json",), plan.canonical_json()
    )
    operation = None
    deferred = None
    evidence: PhysicalVerificationEvidence | None = None
    try:
        with workspace_operation(
            client,
            spec.project.workspace_root,
            f"verify-layout-{check}",
            policy=OperationPolicy.READ_ONLY,
            operation_id=operation_id,
        ) as operation:
            bind_operation(operation)
            with operation.view_lease(
                spec.library,
                cells=(spec.cell,),
                views=((spec.cell, spec.view),),
            ):
                operation.require_project_library_target(client, spec.library)
                info = client.library.get(spec.library, timeout=30)
                if (
                    str(info.technology_library or "")
                    != spec.pdk.oa.technology_library
                ):
                    raise RuntimeError(
                        f"library {spec.library} uses unexpected technology "
                        f"{info.technology_library!r}"
                    )
                validate_layout_plan(client, plan, operation=operation, timeout=60)
                gds = _run_xstream(
                    artifacts,
                    spec,
                    layermap_source=layermap_source,
                    resources=resources,
                    timeout=xstream_timeout,
                )
                evidence = _run_calibre(
                    artifacts,
                    spec,
                    plan,
                    check=check,
                    deck_source=deck_source,
                    resources=resources,
                    gds=gds,
                    timeout=calibre_timeout,
                )

                def commit() -> Path:
                    assert evidence is not None
                    artifacts.write_json(
                        "outputs",
                        ("completion.json",),
                        {
                            "library": spec.library,
                            "cell": spec.cell,
                            "view": spec.view,
                            "check": check,
                            "status": evidence.status.value,
                            "passed": evidence.clean,
                            "product_qualification_conclusion": False,
                        },
                    )
                    return artifacts.write_text(
                        "outputs",
                        ("typed-evidence.json",),
                        evidence.canonical_json(),
                    )

                deferred = operation.defer_commit(commit)
    except BaseException:
        reason = getattr(operation, "uncertain_reason", None)
        if isinstance(reason, str) and reason and record_uncertainty is not None:
            record_uncertainty(reason)
        raise
    if evidence is None or deferred is None or not deferred.completed:
        raise RuntimeError(
            "layout verification completed without committed typed evidence"
        )
    return LayoutVerificationResult(evidence.clean, evidence)


__all__ = [
    "LayoutVerificationResult",
    "calibre_environment",
    "parse_drc_summary",
    "parse_lvs_report",
    "render_canonical_source_cdl",
    "render_drc_run_deck",
    "render_lvs_run_deck",
    "run_layout_verification",
]
