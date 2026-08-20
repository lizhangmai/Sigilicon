"""Audited XStream/Calibre DRC and LVS for generated OA layouts."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
from typing import Any, Mapping, Sequence

from sigilicon.artifacts import (
    ArtifactManifestError,
    ArtifactRecord,
    file_sha256,
    load_manifest,
    new_identity,
    read_nofollow_text,
)
from sigilicon.external_tools import (
    cadence_subprocess_env,
    owned_directory,
    owned_input_file,
    run_process_group,
)
from sigilicon.domain.netlist import render_canonical_cdl, resolve_netlist_hierarchy
from sigilicon.domain.provenance import digest, netlist_electrical_fingerprint
from sigilicon.layout.generator import build_layout_plan
from sigilicon.layout.ir import LayoutPlan
from sigilicon.layout.provenance import (
    layout_hierarchy_fingerprints,
    layout_verification_fingerprint,
)
from sigilicon.layout.spec import LayoutSpec, load_layout_spec
from sigilicon.paths import ProjectContext
from sigilicon.virtuoso.layout_generation import validate_layout_plan
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation


_DRC_RESULT = re.compile(
    r"^RULECHECK (?P<name>.+?) \.+ TOTAL Result Count = (?P<count>\d+)\s+\(\d+\)$",
    re.MULTILINE,
)
_ORIGINAL_LAYER = re.compile(
    r"^LAYER (?P<name>\S+) \.+ TOTAL Original Geometry Count = "
    r"(?P<count>\d+)\s+\(\d+\)$",
    re.MULTILINE,
)
_XSTREAM_COMPLETE = re.compile(
    r"Translation completed\.\s+'0' error\(s\) and '0' warning\(s\) found\."
)
@dataclass(frozen=True)
class LayoutVerificationResult:
    check: str
    passed: bool
    run_id: str
    run_dir: Path
    manifest_path: Path
    layout_fingerprint: str
    details: Mapping[str, object]


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
        primitive_masters=spec.layout_pdk.primitive_masters,
    )
    if hierarchy.definitions[spec.cell].ports != spec.ports:
        raise RuntimeError("resolved LVS hierarchy changed the canonical top interface")
    primitive_interfaces = []
    for master in sorted(hierarchy.primitive_counts):
        terminals = spec.layout_pdk.primitive_subcircuits.get(master)
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
        primitive_subcircuit_masters=spec.layout_pdk.primitive_subcircuits,
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
    return {
        "passed": violation_count == 0 and not waiver_layers_used,
        "result_count": total,
        "configuration_warning_count": configuration_warning_count,
        "runtime_warning_count": len(runtime_warnings),
        "runtime_warnings": runtime_warnings,
        "violation_count": violation_count,
        "waiver_layers_used": waiver_layers_used,
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


def find_xstream(explicit: Path | None = None) -> Path:
    home = os.environ.get("CDSHOME")
    candidates = [Path(home) / "tools" / "dfII" / "bin" / "strmout"] if home else []
    return _find_executable("strmout", explicit, candidates)


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


def _xstream_environment(executable: Path) -> dict[str, str]:
    environment = cadence_subprocess_env()
    cds_home = Path(environment.get("CDSHOME", executable.parents[3]))
    environment.setdefault("CDSHOME", str(cds_home))
    environment.setdefault("CDSROOT", str(cds_home))
    environment.setdefault("CDS_INST_DIR", str(cds_home))
    environment.setdefault("OA_HOME", str(cds_home / "oa_v22.62.021"))
    _prepend(environment, "LD_LIBRARY_PATH", cds_home / "tools.lnx86" / "lib")
    return environment


def _calibre_environment(executable: Path) -> dict[str, str]:
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


def _generation_reference(paths: ProjectContext, spec: LayoutSpec, fingerprint: str) -> str:
    attempts = (
        paths.artifact_root
        / "designs"
        / spec.library
        / spec.cell
        / "layout"
        / spec.view
        / "generate"
        / "attempts"
    )
    matches: list[tuple[str, Path]] = []
    if attempts.is_dir():
        for manifest_path in attempts.glob("*/manifest.json"):
            try:
                manifest = load_manifest(manifest_path)
            except ArtifactManifestError:
                continue
            if (
                manifest.get("status") == "succeeded"
                and manifest.get("operation") == "generate-layout"
                and manifest.get("fingerprints", {}).get("run") == fingerprint
            ):
                matches.append((str(manifest.get("completed_at") or ""), manifest_path))
    if not matches:
        raise RuntimeError(
            f"no successful layout-generation artifact matches {spec.view} fingerprint {fingerprint}"
        )
    selected = max(matches)[1]
    return selected.relative_to(paths.artifact_root).as_posix()


def _scoped_layout_fingerprint(
    spec: LayoutSpec,
    plan: LayoutPlan,
    check: str,
) -> str:
    """Hash only geometry/connectivity relevant to the selected check.

    A source-owned OA library supplies the generated-master closure so child
    cell renames are represented by child structure instead of by name.  A
    standalone layout spec safely falls back to exact unresolved master names.
    """

    context = ProjectContext.from_project_root(spec.project_root)
    try:
        relative_spec = spec.path.relative_to(context.ip_root)
    except ValueError:
        return layout_verification_fingerprint(plan, scope=check)
    if len(relative_spec.parts) <= 1:
        return layout_verification_fingerprint(plan, scope=check)
    manifest = context.ip_config(relative_spec.parts[0], "oa.toml")
    if not manifest.is_file():
        return layout_verification_fingerprint(plan, scope=check)
    from sigilicon.workflows.oa_library import plan_oa_library_rebuild

    library_plan = plan_oa_library_rebuild(
        manifest,
        project_root=spec.project_root,
        library=spec.library,
    )
    plans = tuple(step.plan for step in library_plan.layouts)
    hierarchy = layout_hierarchy_fingerprints(plans, scope=check)
    key = (plan.library, plan.cell, plan.view)
    matching = next(
        (step.plan for step in library_plan.layouts if step.plan.fingerprint == plan.fingerprint),
        None,
    )
    if matching is not None:
        return hierarchy[key]
    return layout_verification_fingerprint(
        plan,
        scope=check,
        master_fingerprints=hierarchy,
    )


def _verification_scope(
    spec: LayoutSpec,
    plan: LayoutPlan,
    check: str,
) -> dict[str, str]:
    result = {"layout": _scoped_layout_fingerprint(spec, plan, check)}
    if check == "lvs":
        hierarchy = resolve_netlist_hierarchy(
            spec.source_snapshots,
            top=spec.cell,
            primitive_masters=spec.layout_pdk.primitive_masters,
        )
        result["electrical"] = netlist_electrical_fingerprint(
            hierarchy,
            primitive_masters=spec.layout_pdk.primitive_masters,
        )
    return result


def _run_fingerprint(
    spec: LayoutSpec,
    plan: LayoutPlan,
    check: str,
    *,
    verification_scope: Mapping[str, str] | None = None,
) -> str:
    deck = spec.layout_pdk.drc_deck if check == "drc" else spec.layout_pdk.lvs_deck
    payload = {
        "check": check,
        "verification_scope": dict(
            verification_scope or _verification_scope(spec, plan, check)
        ),
        "deck_sha256": file_sha256(deck),
        "layermap_sha256": file_sha256(spec.layout_pdk.layermap),
        "pdk_configuration_sha256": spec.layout_pdk.configuration_sha256,
        "xstream_flatten_pcells": spec.layout_pdk.xstream_flatten_pcells,
        "xstream_suppressed_warnings": spec.layout_pdk.xstream_suppressed_warnings,
        "drc_disabled_defines": dict(spec.layout_pdk.drc_disabled_defines),
        "drc_configuration_warnings": spec.layout_pdk.drc_configuration_warnings,
        "drc_waiver_layers": spec.layout_pdk.drc_waiver_layers,
        "pcell_policy": {
            "finger_count_parameter": spec.layout_pdk.pcell_policy.finger_count_parameter,
            "source_terminal": spec.layout_pdk.pcell_policy.source_terminal,
            "drain_terminal": spec.layout_pdk.pcell_policy.drain_terminal,
            "source_alias_prefix": spec.layout_pdk.pcell_policy.source_alias_prefix,
            "drain_alias_prefix": spec.layout_pdk.pcell_policy.drain_alias_prefix,
            "cdf_callback_parameter": spec.layout_pdk.pcell_policy.cdf_callback_parameter,
            "cdf_callback_bypass_parameters": spec.layout_pdk.pcell_policy.cdf_callback_bypass_parameters,
        },
    }
    return digest(payload)


def _run_xstream(
    record: ArtifactRecord,
    spec: LayoutSpec,
    *,
    xstream: Path,
    timeout: int,
) -> Path:
    staged_layermap = record.copy_file(
        "inputs", ("layermap",), spec.layout_pdk.layermap, label="XStream layer map"
    )
    cds_lib = ProjectContext.from_project_root(spec.project_root).workspace_root / "cds.lib"
    if not cds_lib.is_file():
        raise FileNotFoundError(f"workspace cds.lib does not exist: {cds_lib}")
    record.write_json(
        "inputs",
        ("xstream-external-inputs.json",),
        {"cds_lib": str(cds_lib), "cds_lib_sha256": file_sha256(cds_lib)},
        label="attested XStream workspace inputs",
    )
    work = record.paths.role("work")
    with owned_directory(work) as owned_work, ExitStack() as resources:
        owned_map = resources.enter_context(owned_input_file(staged_layermap))
        owned_cds = resources.enter_context(
            owned_input_file(cds_lib, require_single_link=False)
        )
        command_parts = [
            str(xstream),
            "-library",
            spec.library,
            "-strmFile",
            str(work / "layout.gds"),
            "-runDir",
            str(work),
            "-topCell",
            spec.cell,
            "-view",
            spec.view,
            "-logFile",
            str(work / "strmout.log"),
            "-summaryFile",
            str(work / "strmout.sum"),
            "-techLib",
            spec.pdk.technology_library,
            "-layerMap",
            owned_map.child_named_path,
        ]
        if spec.layout_pdk.xstream_flatten_pcells:
            command_parts.append("-flattenPcells")
        if spec.layout_pdk.xstream_suppressed_warnings:
            command_parts.extend(
                [
                    "-noWarn",
                    " ".join(
                        warning.removeprefix("XSTRM-")
                        for warning in spec.layout_pdk.xstream_suppressed_warnings
                    ),
                ]
            )
        command_parts.extend(
            [
            "-flattenVias",
            "-convertPin",
            "geometryAndText",
            "-cdslib",
            owned_cds.child_named_path,
            ]
        )
        command = tuple(command_parts)
        record.write_json(
            "inputs",
            ("xstream-command.json",),
            {"argv": list(command), "cwd": str(work), "timeout_seconds": timeout},
            label="guarded XStream command",
        )

        def validate_spawn() -> None:
            owned_map.require_visible()
            owned_cds.require_visible()

        completed = run_process_group(
            command,
            cwd=work,
            env=_xstream_environment(xstream),
            timeout=timeout,
            before_spawn=validate_spawn,
            pass_fds=(
                owned_work.fd,
                owned_map.fd,
                owned_map.directory_fd,
                owned_cds.fd,
                owned_cds.directory_fd,
            ),
        )
    record.write_text(
        "logs", ("xstream-stdout.log",), completed.stdout, label="XStream stdout/stderr"
    )
    for name, label in (
        ("strmout.log", "XStream native log"),
        ("strmout.sum", "XStream summary"),
    ):
        candidate = work / name
        if not candidate.is_file():
            raise RuntimeError(f"XStream did not produce {name}")
        record.copy_file("logs", (name,), candidate, label=label)
    if completed.returncode != 0:
        raise RuntimeError(f"XStream exited {completed.returncode}")
    native_log = read_nofollow_text(work / "strmout.log", errors="replace")
    summary = read_nofollow_text(work / "strmout.sum", errors="replace")
    if _XSTREAM_COMPLETE.search(native_log + "\n" + summary + "\n" + completed.stdout) is None:
        raise RuntimeError("XStream summary does not prove a zero-warning translation")
    gds = work / "layout.gds"
    if not gds.is_file() or gds.stat().st_size == 0:
        raise RuntimeError("XStream did not produce a non-empty GDS")
    staged_gds = record.copy_file(
        "inputs", ("layout.gds",), gds, label="XStream layout export"
    )
    staged_gds.chmod(0o444)
    return staged_gds


def _run_calibre(
    record: ArtifactRecord,
    spec: LayoutSpec,
    plan: LayoutPlan,
    check: str,
    *,
    calibre: Path,
    gds: Path,
    timeout: int,
) -> dict[str, object]:
    source_deck = spec.layout_pdk.drc_deck if check == "drc" else spec.layout_pdk.lvs_deck
    staged_source_deck = record.copy_file(
        "inputs",
        ("foundry.drc" if check == "drc" else "foundry.lvs",),
        source_deck,
        label=f"foundry Calibre {check.upper()} deck",
    )
    source_text = read_nofollow_text(staged_source_deck)
    source_cdl: Path | None = None
    if check == "lvs":
        for index, snapshot in enumerate(spec.source_snapshots):
            record.write_text(
                "inputs",
                ("canonical-source", f"{index:02d}-{snapshot.source_path.name}"),
                snapshot.text,
                label="exact canonical Spectre source",
            )
        record.write_json(
            "inputs",
            ("canonical-source", "provenance.json"),
            {
                "top": spec.cell,
                "sources": [
                    {
                        "path": snapshot.source_path.relative_to(
                            spec.project_root
                        ).as_posix(),
                        "sha256": snapshot.sha256,
                    }
                    for snapshot in spec.source_snapshots
                ],
            },
            label="canonical LVS source provenance",
        )
        source_cdl = record.write_text(
            "inputs",
            ("source.cdl",),
            render_canonical_source_cdl(spec),
            label="mechanically converted canonical source CDL",
        )
        source_cdl.chmod(0o444)

    work = record.paths.role("work")
    results = record.paths.role("results")
    canonical_deck = (
        render_drc_run_deck(
            source_text,
            layout_path=str(gds),
            primary=spec.cell,
            results_path=str(results / "drc-results.db"),
            summary_path=str(results / "drc-summary.rep"),
            disabled_defines=spec.layout_pdk.drc_disabled_defines,
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
        "inputs", (f"run.{check}",), canonical_deck, label=f"resolved Calibre {check.upper()} run deck"
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
                disabled_defines=spec.layout_pdk.drc_disabled_defines,
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
            label=f"invocation-only Calibre {check.upper()} deck",
        )
        invocation_path.chmod(0o444)
        owned_deck = resources.enter_context(owned_input_file(invocation_path))
        command = (str(calibre), f"-{check}", "-hier", owned_deck.child_named_path)
        record.write_json(
            "inputs",
            ("calibre-command.json",),
            {"argv": list(command), "cwd": str(work), "timeout_seconds": timeout},
            label=f"guarded Calibre {check.upper()} command",
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
            env=_calibre_environment(calibre),
            timeout=timeout,
            before_spawn=validate_spawn,
            pass_fds=tuple(dict.fromkeys(pass_fds)),
        )
    record.write_text(
        "logs",
        (f"calibre-{check}.log",),
        completed.stdout,
        label=f"Calibre {check.upper()} execution log",
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Calibre {check.upper()} exited {completed.returncode}")

    if check == "drc":
        outputs = (
            ("drc-results.db", "Calibre DRC results database"),
            ("drc-summary.rep", "Calibre DRC summary report"),
        )
        copied = {
            name: record.copy_file("results", (name,), work / name, label=label)
            for name, label in outputs
        }
        return parse_drc_summary(
            read_nofollow_text(copied["drc-summary.rep"]),
            configuration_warnings=spec.layout_pdk.drc_configuration_warnings,
            waiver_layers=spec.layout_pdk.drc_waiver_layers,
        )

    outputs = (
        ("lvs.rep", "lvs-report", "Calibre LVS report"),
        ("lvs.rep.ext", "lvs-extraction-report", "Calibre LVS extraction report"),
        ("calibre_erc.db", "calibre-erc-db", "Calibre ERC results"),
        ("calibre_erc.sum", "calibre-erc-summary", "Calibre ERC summary"),
    )
    copied_lvs: dict[str, Path] = {}
    for source_name, result_name, label in outputs:
        candidate = work / source_name
        if not candidate.is_file():
            raise RuntimeError(f"Calibre LVS did not produce {source_name}")
        copied_lvs[source_name] = record.copy_file(
            "results", (result_name,), candidate, label=label
        )
    extracted = work / "svdb" / f"{spec.cell}.sp"
    if not extracted.is_file():
        raise RuntimeError("Calibre LVS did not produce the extracted layout netlist")
    record.copy_file(
        "results", ("extracted.sp",), extracted, label="Calibre extracted layout netlist"
    )
    result = parse_lvs_report(read_nofollow_text(copied_lvs["lvs.rep"]), primary=spec.cell)
    if result["passed"] and "LVS completed. CORRECT." not in completed.stdout:
        raise RuntimeError("Calibre log does not independently confirm correct LVS completion")
    return result


def verify_layout(
    spec: LayoutSpec,
    client: Any,
    *,
    check: str,
    artifact_root: Path | None = None,
    xstream_timeout: int = 120,
    calibre_timeout: int = 600,
    xstream: Path | None = None,
    calibre: Path | None = None,
) -> LayoutVerificationResult:
    if check not in {"drc", "lvs"}:
        raise ValueError("layout verification check must be drc or lvs")
    plan = build_layout_plan(spec)
    if plan.stage != "routed":
        raise ValueError("physical verification requires a routed layout plan")
    paths = ProjectContext.from_project_root(spec.project_root, artifact_root=artifact_root)
    reference = _generation_reference(paths, spec, plan.fingerprint)
    verification_scope = _verification_scope(spec, plan, check)
    run_fingerprint = _run_fingerprint(
        spec,
        plan,
        check,
        verification_scope=verification_scope,
    )
    record = ArtifactRecord.begin(
        paths.artifacts.layout_verification_run(
            spec.library, spec.cell, spec.view, check, new_identity()
        ),
        entities={
            "library": spec.library,
            "cell": spec.cell,
            "view": spec.view,
            "check": check,
        },
        operation=f"calibre-{check}",
        backend="xstream+calibre",
        source_fingerprint=spec.source_fingerprint,
        run_fingerprint=run_fingerprint,
        reference_links={"layout_generation": reference},
    )
    record.copy_file("inputs", ("layout.toml",), spec.path, label="canonical layout intent")
    record.copy_file(
        "inputs",
        ("layout-generator.py",),
        spec.generator_source,
        label="design-owned layout generator source",
    )
    for index, dependency in enumerate(spec.generator_dependencies):
        record.copy_file(
            "inputs",
            ("layout-generator-dependencies", f"{index:02d}-{dependency.name}"),
            dependency,
            label="design-owned layout generator dependency",
        )
    for index, (module, dependency) in enumerate(
        zip(spec.generator_modules, spec.generator_module_sources, strict=True)
    ):
        record.copy_file(
            "inputs",
            ("layout-generator-modules", f"{index:02d}-{dependency.name}"),
            dependency,
            label=f"installed layout generator module {module}",
        )
    record.write_text(
        "inputs", ("layout-plan.json",), plan.canonical_json(), label="generated layout plan"
    )
    xstream_executable = find_xstream(xstream or spec.layout_pdk.xstream_bin)
    calibre_executable = find_calibre(calibre or spec.layout_pdk.calibre_bin)
    completed_stages: list[str] = []
    operation = None
    outcome: dict[str, object] | None = None
    library_path: Path | None = None
    deferred = None
    with (
        record.failure_boundary(
            uncertainty=lambda: operation.uncertain_reason if operation else None,
            partial_failure=lambda: (
                {
                    "completed_stages": list(completed_stages),
                    "failed_stage": "physical-verification",
                    "cell": spec.cell,
                    "view": spec.view,
                    "check": check,
                }
                if completed_stages
                else None
            ),
        ),
        workspace_operation(
            client,
            paths.workspace_root,
            f"verify-layout-{check}",
            policy=OperationPolicy.READ_ONLY,
        ) as operation,
        operation.view_lease(
            spec.library,
            cells=(spec.cell,),
            views=((spec.cell, spec.view),),
        ),
    ):
        operation.register_artifact(record)
        library_path = operation.require_project_library_target(client, spec.library)
        info = client.library.get(spec.library, timeout=30)
        if str(info.technology_library or "") != spec.pdk.technology_library:
            raise RuntimeError(
                f"library {spec.library} uses technology {info.technology_library!r}, "
                f"expected {spec.pdk.technology_library!r}"
            )
        validate_layout_plan(
            client,
            plan,
            pcell_policy=spec.layout_pdk.pcell_policy,
            operation=operation,
            timeout=60,
        )
        completed_stages.append("oa-content-validation")
        gds = _run_xstream(
            record,
            spec,
            xstream=xstream_executable,
            timeout=xstream_timeout,
        )
        completed_stages.append("xstream-export")
        outcome = _run_calibre(
            record,
            spec,
            plan,
            check,
            calibre=calibre_executable,
            gds=gds,
            timeout=calibre_timeout,
        )
        completed_stages.append(f"calibre-{check}")
        record.add_file("work", record.paths.role("work"), label="native verification work directory")

        def commit() -> Path:
            assert outcome is not None
            completion_payload = {
                "library": spec.library,
                "cell": spec.cell,
                "view": spec.view,
                "check": check,
                "layout_fingerprint": plan.fingerprint,
                "verification_scope_fingerprints": verification_scope,
                "run_fingerprint": run_fingerprint,
                "oa_content_fingerprint_confirmed": True,
                "library_path": str(library_path),
                "xstream": str(xstream_executable),
                "calibre": str(calibre_executable),
                **outcome,
            }
            completion = record.write_json(
                "results",
                ("completion.json",),
                completion_payload,
                label="physical verification completion proof",
            )
            return record.succeed(
                completion_evidence=(completion,),
                details={
                    "layout_fingerprint": plan.fingerprint,
                    "verification_scope_fingerprints": verification_scope,
                    **outcome,
                },
            )

        deferred = operation.defer_commit(commit)
    if deferred is None or not deferred.completed or outcome is None:
        raise RuntimeError("layout verification completed without committing its artifact")
    return LayoutVerificationResult(
        check=check,
        passed=bool(outcome["passed"]),
        run_id=record.paths.identity,
        run_dir=record.paths.root,
        manifest_path=record.paths.manifest,
        layout_fingerprint=plan.fingerprint,
        details=outcome,
    )


def execute_layout_verification_spec(
    spec_path: Path,
    project_root: Path,
    client: Any,
    *,
    check: str,
    xstream_timeout: int = 120,
    calibre_timeout: int = 600,
) -> tuple[LayoutSpec, LayoutVerificationResult]:
    spec = load_layout_spec(spec_path, project_root=project_root)
    return spec, verify_layout(
        spec,
        client,
        check=check,
        xstream_timeout=xstream_timeout,
        calibre_timeout=calibre_timeout,
    )
