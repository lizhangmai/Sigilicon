"""Calibre verification of immutable GDS and CDL, independent of their producer."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
import os
import re
from typing import Mapping, Sequence

from sigilicon.artifacts import SafeTree, read_nofollow_bytes, read_nofollow_text
from sigilicon.domain.physical_verification import (
    CheckedLayoutIdentity, CheckedSourceIdentity, DrcEvidence, DrcViolation,
    LvsEvidence, LvsMismatch, PhysicalVerificationEvidence, PhysicalVerificationPolicy,
    PhysicalVerificationStatus, VerificationCompletion,
)
from sigilicon.domain.platform import VerificationDeck
from sigilicon.execution._resources import Resources
from sigilicon.execution._workspace import ExecutionWorkspace
from sigilicon.external_tools import (
    ProcessRequest, cadence_subprocess_env, managed_process, owned_directory, owned_input_file,
)
import hashlib


@dataclass(frozen=True)
class VerificationRequest:
    owner: str
    cell: str
    check: str
    plan_identity: str
    policy: PhysicalVerificationPolicy
    deck: VerificationDeck

    def __post_init__(self) -> None:
        if (not isinstance(self.cell, str) or not self.cell or self.cell in {".", ".."}
                or any(character in "/\\" or character.isspace() or ord(character) < 32
                       or ord(character) == 127 for character in self.cell)):
            raise ValueError("Calibre cell must be a non-empty logical name without path separators or whitespace")


def render_run_deck(source: str, request: VerificationRequest, **parameters: str) -> str:
    """Apply only platform-declared exact edits; changed decks fail closed."""
    result = source
    if request.check == "drc":
        for name, expected in request.policy.drc_disabled_defines.items():
            result = _disable_define(result, name, expected=expected)
    for substitution in request.deck.substitutions:
        result = substitution.apply(result, parameters)
    return result


_DRC_RESULT = re.compile(
    r"^RULECHECK (?P<name>.+?) \.+ TOTAL Result Count = (?P<count>\d+)\s+\(\d+\)$",
    re.MULTILINE,
)
_ORIGINAL_LAYER = re.compile(
    r"^LAYER (?P<name>\S+) \.+ TOTAL Original Geometry Count = "
    r"(?P<count>\d+)\s+\(\d+\)$",
    re.MULTILINE,
)
_ADAPTER = "mentor.calibre"


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


def _failed_evidence(
    request: VerificationRequest,
    layout: CheckedLayoutIdentity,
    source: CheckedSourceIdentity | None,
    *,
    check: str,
    exit_code: int | None,
    message: str,
) -> PhysicalVerificationEvidence:
    completion = VerificationCompletion(_ADAPTER, True, False, exit_code)
    if check == "drc":
        return DrcEvidence(
            PhysicalVerificationStatus.EXECUTION_FAILED,
            layout,
            completion,
            (),
            message,
        )
    return LvsEvidence(
        PhysicalVerificationStatus.EXECUTION_FAILED,
        layout,
        source,
        completion,
        (),
        message,
    )


def _parsed_evidence(
    request: VerificationRequest,
    layout: CheckedLayoutIdentity,
    source: CheckedSourceIdentity | None,
    *,
    check: str,
    report: str,
) -> PhysicalVerificationEvidence:
    completion = VerificationCompletion(_ADAPTER, True, True, 0)
    if check == "drc":
        assert request.policy is not None
        parsed = parse_drc_summary(
            report,
            configuration_warnings=(
                request.policy.drc_configuration_warnings
            ),
            waiver_layers=request.policy.drc_waiver_layers,
        )
        warnings = frozenset(
            request.policy.drc_configuration_warnings
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
            layout,
            completion,
            violations,
            "DRC report is clean"
            if status is PhysicalVerificationStatus.CLEAN
            else "DRC report contains violations",
        )
    parsed = parse_lvs_report(report, primary=request.cell)
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
        layout,
        source,
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
        candidate = SafeTree(work).file(source_name, "Calibre output")
        copied[source_name] = record.copy_file("outputs", (result_name,), candidate.path)
    return copied


def run_calibre_verification(
    record: ExecutionWorkspace,
    request: VerificationRequest,
    *,
    deck_source: str,
    resources: Resources,
    gds: Path,
    source_cdl: Path | None,
    timeout: int,
) -> PhysicalVerificationEvidence:
    check = request.check
    if check not in {"drc", "lvs"} or (check == "lvs" and source_cdl is None):
        raise ValueError("physical verification requires a supported check and LVS source")
    layout = CheckedLayoutIdentity(
        artifact_identity=_sha256(read_nofollow_bytes(gds)), plan_identity=request.plan_identity,
        result_identity=None, owner=request.owner, name=request.cell, format="gdsii")
    source = (CheckedSourceIdentity(_sha256(read_nofollow_bytes(source_cdl)), request.owner, request.cell)
              if source_cdl is not None else None)
    staged_deck = record.write_text(
        "inputs",
        ("foundry.drc" if check == "drc" else "foundry.lvs",),
        deck_source,
    )
    source_text = read_nofollow_text(staged_deck)
    work = record.directory("work")
    canonical_deck = render_run_deck(
        source_text, request, layout_path=str(gds), source_path=str(source_cdl or ""),
        primary=request.cell, work_dir=str(record.output_root),
        results_path=str(record.output_root / "drc-results.db"),
        summary_path=str(record.output_root / "drc-summary.rep"),
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
        invocation = render_run_deck(
            source_text, request, layout_path=owned_gds.child_named_path,
            source_path=owned_source.child_named_path if owned_source else "",
            primary=request.cell, work_dir=str(child_work),
            results_path=str(child_work / "drc-results.db"),
            summary_path=str(child_work / "drc-summary.rep"),
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
                "check": check,
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
            request,
            layout,
            source,
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
                    (f"svdb/{request.cell}.sp", "extracted.sp"),
                ),
            )
            report = read_nofollow_text(copied["lvs.rep"])
            if (
                parse_lvs_report(report, primary=request.cell)["passed"]
                and "LVS completed. CORRECT."
                not in f"{completed.stdout}\n{completed.stderr}"
            ):
                raise RuntimeError(
                    "Calibre log does not independently confirm correct LVS completion"
                )
        return _parsed_evidence(
            request,
            layout,
            source,
            check=check,
            report=report,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return _failed_evidence(
            request,
            layout,
            source,
            check=check,
            exit_code=0,
            message=f"Calibre {check.upper()} evidence is incomplete: {exc}",
        )
