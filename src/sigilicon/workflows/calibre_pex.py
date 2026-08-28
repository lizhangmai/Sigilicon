"""Receipt-bound Calibre xRC parasitic extraction.

The Adapter owns orchestration and evidence projection only.  The foundry deck
remains a private platform asset, Calibre remains a resolved site capability,
and no PEX result is treated as qualification or signoff evidence.
"""

from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Mapping

from sigilicon.artifacts import atomic_write_json, read_nofollow_text
from sigilicon.domain.physical_verification import VerificationCompletion
from sigilicon.domain.post_layout import (
    DerivedArtifactIdentity,
    PexEvidence,
    PexStatus,
    pex_evidence_id,
    pex_evidence_from_json,
)
from sigilicon.external_tools import owned_directory, owned_input_file, run_process_group
from sigilicon.flow.model import (
    ActionContext,
    AdapterExecution,
    CollectedActionResult,
    FlowExecutionError,
    ProducedArtifact,
)
from sigilicon.flow.post_layout import PEX_ACTION, PEX_EVIDENCE_KIND, PEX_NETLIST_KIND
from sigilicon.workflows.layout_verification import (
    ReceiptBoundLayoutSourceInputs,
    calibre_environment,
    copy_regular_backend_output,
    load_receipt_bound_layout_source_inputs,
)


CALIBRE_XRC_PEX_ADAPTER = "calibre-xrc-pex"
_PRIMARY = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_INCLUDE = re.compile(
    r'^[ \t]*\.include[ \t]+"(?P<name>[^"]+)"[ \t]*$',
    re.IGNORECASE | re.MULTILINE,
)
_SUBCKT = re.compile(
    r"^[ \t]*\.subckt[ \t]+(?P<name>\S+)(?P<ports>[^\r\n]*)$",
    re.IGNORECASE | re.MULTILINE,
)
_PARASITIC_ELEMENT = re.compile(r"^[ \t]*[rc][^ \t\r\n]*[ \t]+", re.IGNORECASE | re.MULTILINE)
_CREATED_HEADER = re.compile(r'^\* Created:[^\r\n]*$', re.MULTILINE)
_XRC_ERRORS_ZERO = re.compile(
    r"^[ \t]*xRC Errors[ \t]*=[ \t]*0[ \t]*$",
    re.MULTILINE,
)
_MAX_SUPPORT_FILES = 10_000
_MAX_SUPPORT_BYTES = 256 * 1024 * 1024


def _safe_svrf_value(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or '"' in value
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError(f"{label} is not a safe SVRF quoted value")
    return value


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    pattern = re.compile(rf"^(?P<indent>[ \t]*){re.escape(old)}", re.MULTILINE)
    rendered, count = pattern.subn(
        lambda match: f"{match.group('indent')}{new}",
        text,
    )
    if count != 1:
        raise RuntimeError(
            f"Calibre xRC {label} changed; expected one active exact occurrence, got {count}"
        )
    return rendered


def render_calibre_xrc_deck(
    source: str,
    *,
    layout_path: str,
    source_path: str,
    primary: str,
) -> str:
    """Resolve only the audited invocation fields in the foundry xRC deck."""

    if not isinstance(source, str):
        raise TypeError("Calibre xRC deck must be text")
    if _PRIMARY.fullmatch(primary) is None:
        raise ValueError("Calibre xRC primary is not a legal cell name")
    layout_path = _safe_svrf_value(layout_path, "layout path")
    source_path = _safe_svrf_value(source_path, "source path")
    rendered = source
    replacements = (
        ('LAYOUT PRIMARY "lvs_top"', f'LAYOUT PRIMARY "{primary}"', "layout primary"),
        ('LAYOUT PATH "lvs_top.gds"', f'LAYOUT PATH "{layout_path}"', "layout path"),
        ('SOURCE PRIMARY "lvs_top"', f'SOURCE PRIMARY "{primary}"', "source primary"),
        ('SOURCE PATH "lvs_top.cdl"', f'SOURCE PATH "{source_path}"', "source path"),
        (
            'PEX NETLIST                    "net.dist"',
            'PEX NETLIST                    "extracted.pex"',
            "distributed netlist output",
        ),
        (
            'PEX NETLIST SIMPLE             "net.simple"',
            'PEX NETLIST SIMPLE             "extracted.simple.pex"',
            "simple netlist output",
        ),
    )
    for old, new, label in replacements:
        rendered = _replace_once(rendered, old, new, label)
    return rendered


def _read_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    os.lseek(descriptor, 0, os.SEEK_SET)
    while block := os.read(descriptor, 1024 * 1024):
        chunks.append(block)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return b"".join(chunks)


def _support_manifest(root: Path, *, content: bool) -> Mapping[str, tuple[int, bytes]]:
    """Reject aliases/special files and return a bounded support-tree identity."""

    root_metadata = root.lstat()
    if not stat.S_ISDIR(root_metadata.st_mode) or root.is_symlink():
        raise FlowExecutionError("PEX support root must be a non-symlink directory")
    manifest: dict[str, tuple[int, bytes]] = {}
    total_bytes = 0

    def visit(directory: Path) -> None:
        nonlocal total_bytes
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise FlowExecutionError("cannot inspect PEX support tree") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise FlowExecutionError(
                    f"PEX support tree contains symlink {relative!r}"
                )
            if stat.S_ISDIR(metadata.st_mode):
                visit(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise FlowExecutionError(
                    f"PEX support tree contains special file {relative!r}"
                )
            if len(manifest) >= _MAX_SUPPORT_FILES:
                raise FlowExecutionError("PEX support tree exceeds its file budget")
            total_bytes += metadata.st_size
            if total_bytes > _MAX_SUPPORT_BYTES:
                raise FlowExecutionError("PEX support tree exceeds its byte budget")
            identity = b""
            if content:
                directory_descriptor = -1
                try:
                    directory_descriptor = os.open(
                        directory,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    )
                    descriptor = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                        dir_fd=directory_descriptor,
                    )
                except OSError as exc:
                    if directory_descriptor >= 0:
                        os.close(directory_descriptor)
                    raise FlowExecutionError(
                        f"cannot attest PEX support file {relative!r}"
                    ) from exc
                try:
                    opened = os.fstat(descriptor)
                    if not stat.S_ISREG(opened.st_mode) or (
                        opened.st_dev,
                        opened.st_ino,
                        opened.st_size,
                        opened.st_mtime_ns,
                    ) != (
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_size,
                        metadata.st_mtime_ns,
                    ):
                        raise FlowExecutionError(
                            f"PEX support file changed during attestation: {relative!r}"
                        )
                    identity = _read_descriptor(descriptor)
                finally:
                    os.close(descriptor)
                    os.close(directory_descriptor)
            manifest[relative] = (metadata.st_size, identity)

    visit(root)
    if not manifest:
        raise FlowExecutionError("PEX support tree is empty")
    return manifest


def _stage_support_tree(source: Path, destination: Path) -> None:
    before = _support_manifest(source, content=True)
    shutil.copytree(source, destination, symlinks=True)
    staged = _support_manifest(destination, content=True)
    after = _support_manifest(source, content=True)
    if before != after or staged != before:
        raise FlowExecutionError("PEX support tree changed while it was staged")


def _top_ports(text: str, primary: str, label: str) -> tuple[str, ...]:
    matches = [match for match in _SUBCKT.finditer(text) if match.group("name") == primary]
    if len(matches) != 1:
        raise RuntimeError(
            f"{label} must contain exactly one top-level {primary!r} subcircuit"
        )
    ports = tuple(matches[0].group("ports").split())
    if not ports or any("=" in port for port in ports):
        raise RuntimeError(f"{label} has an unsupported top-level port declaration")
    if len(ports) != len(set(ports)):
        raise RuntimeError(f"{label} repeats a top-level port")
    return ports


def _self_contained_parasitics(
    main: Path,
    sidecar: Path,
    primary_sidecar: Path,
    *,
    source_text: str,
    primary: str,
) -> str:
    text = read_nofollow_text(main)
    allowed = {
        sidecar.name: read_nofollow_text(sidecar),
        primary_sidecar.name: read_nofollow_text(primary_sidecar),
    }
    observed = [match.group("name") for match in _INCLUDE.finditer(text)]
    if len(observed) != len(allowed) or set(observed) != set(allowed):
        raise RuntimeError("Calibre PEX include closure changed")

    def replace(match: re.Match[str]) -> str:
        return allowed[match.group("name")].rstrip("\n")

    flattened = _INCLUDE.sub(replace, text)
    if _INCLUDE.search(flattened):
        raise RuntimeError("Calibre PEX artifact retained an external include")
    if set(_top_ports(flattened, primary, "Calibre PEX output")) != set(
        _top_ports(source_text, primary, "canonical source")
    ):
        raise RuntimeError("Calibre PEX top-level ports disagree with canonical source")
    if _PARASITIC_ELEMENT.search(flattened) is None:
        raise RuntimeError("Calibre PEX output contains no resistor or capacitor")
    flattened = _CREATED_HEADER.sub(
        "* Created: normalized by Sigilicon",
        flattened,
    )
    return flattened.rstrip("\n") + "\n"


def _facts(evidence: PexEvidence) -> dict[str, object]:
    return {
        "pex-status": evidence.status.value,
        "pex-completed": evidence.completion.proven,
    }


def _failed_evidence(
    inputs: ReceiptBoundLayoutSourceInputs,
    *,
    backend: str,
    status: PexStatus,
    executed: bool,
    exit_code: int | None,
    message: str,
) -> PexEvidence:
    assert inputs.source is not None
    return PexEvidence(
        status,
        inputs.layout,
        inputs.source,
        VerificationCompletion(backend, executed, False, exit_code),
        None,
        message,
    )


class CalibreXrcPexAdapter:
    """Run the fixed Calibre xRC PHDB/PDB/formatter pipeline."""

    def _configuration(self, context: ActionContext) -> int:
        if context.action_config:
            raise FlowExecutionError("Calibre xRC PEX Action config must be empty")
        if set(context.adapter_config) - {"timeout_seconds"}:
            raise FlowExecutionError(
                "Calibre xRC PEX Adapter config accepts only 'timeout_seconds'"
            )
        timeout = context.adapter_config.get("timeout_seconds", 600)
        if type(timeout) is not int or timeout <= 0:
            raise FlowExecutionError("Calibre xRC PEX timeout must be positive")
        return timeout

    def _resources(self, context: ActionContext) -> tuple[str, Path, Path, Path]:
        capability = context.capabilities.get("tool.pex")
        if capability is None or capability.executable is None:
            raise FlowExecutionError("Calibre xRC capability requires an executable")
        executable = capability.executable
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise FlowExecutionError("resolved Calibre xRC executable is unavailable")
        asset = context.platform_assets.get("physical-pex")
        if asset is None or asset.kind != "platform.pex":
            raise FlowExecutionError("Calibre xRC requires a physical-pex platform view")
        deck_member = asset.member("pex-deck")
        support_member = asset.member("pex-support-root")
        if deck_member is None or support_member is None:
            raise FlowExecutionError("physical-pex platform view is incomplete")
        deck = deck_member.location
        support = support_member.location
        if not deck.is_file() or deck.is_symlink():
            raise FlowExecutionError("PEX deck must be a non-symlink regular file")
        try:
            relative = deck.relative_to(support)
        except ValueError as exc:
            raise FlowExecutionError("PEX deck is outside its support root") from exc
        if len(relative.parts) != 1:
            raise FlowExecutionError("PEX deck must be a direct support-root member")
        _support_manifest(support, content=False)
        return capability.identity, executable, deck, support

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        diagnostics: list[str] = []
        try:
            if context.action.kind != PEX_ACTION:
                raise FlowExecutionError("Calibre xRC Adapter requires the PEX Action")
            inputs = load_receipt_bound_layout_source_inputs(context, require_source=True)
            if _PRIMARY.fullmatch(inputs.receipt.target.name) is None:
                raise FlowExecutionError("materialized name is not a legal PEX primary")
            self._configuration(context)
            self._resources(context)
        except (FlowExecutionError, OSError, TypeError, ValueError) as exc:
            diagnostics.append(str(exc))
        return tuple(diagnostics)

    def prepare(self, context: ActionContext) -> None:
        (context.output_root / "reports").mkdir()

    def _invoke(
        self,
        context: ActionContext,
        inputs: ReceiptBoundLayoutSourceInputs,
        *,
        executable: Path,
        deck: Path,
        support: Path,
        timeout: int,
    ) -> tuple[tuple[str, str], ...]:
        assert inputs.source_path is not None
        staged = context.work_root / "pex-kit"
        _stage_support_tree(support, staged)
        staged_deck = staged / deck.name
        with owned_directory(staged) as owned_work, ExitStack() as resources:
            owned_layout = resources.enter_context(owned_input_file(inputs.layout_path))
            owned_source = resources.enter_context(owned_input_file(inputs.source_path))
            invocation = render_calibre_xrc_deck(
                read_nofollow_text(staged_deck),
                layout_path=owned_layout.child_named_path,
                source_path=owned_source.child_named_path,
                primary=inputs.receipt.target.name,
            )
            staged_deck.write_text(invocation, encoding="utf-8")
            staged_deck.chmod(0o444)
            owned_deck = resources.enter_context(owned_input_file(staged_deck))
            stages = (
                ("phdb", "-phdb"),
                ("pdb", "-pdb", "-rc"),
                ("fmt", "-fmt", "-all"),
            )
            commands = tuple(
                (
                    str(executable),
                    "-64",
                    "-xrc",
                    *arguments,
                    owned_deck.child_named_path,
                )
                for _name, *arguments in stages
            )
            atomic_write_json(
                context.work_root / "calibre-xrc-commands.json",
                {
                    "stages": [
                        {"name": stage[0], "argv": list(command)}
                        for stage, command in zip(stages, commands, strict=True)
                    ],
                    "timeout_seconds": timeout,
                },
            )

            def validate_spawn() -> None:
                owned_work.require_visible()
                owned_layout.require_visible()
                owned_source.require_visible()
                owned_deck.require_visible()

            pass_fds = tuple(
                dict.fromkeys(
                    (
                        owned_work.fd,
                        owned_layout.fd,
                        owned_layout.directory_fd,
                        owned_source.fd,
                        owned_source.directory_fd,
                        owned_deck.fd,
                        owned_deck.directory_fd,
                    )
                )
            )
            results: list[tuple[str, str]] = []
            for stage, command in zip(stages, commands, strict=True):
                name = stage[0]
                completed = run_process_group(
                    command,
                    cwd=staged,
                    env=calibre_environment(executable),
                    timeout=timeout,
                    before_spawn=validate_spawn,
                    pass_fds=pass_fds,
                )
                (context.log_root / f"calibre-xrc-{name}.log").write_text(
                    completed.stdout, encoding="utf-8"
                )
                if completed.returncode != 0:
                    raise _StageFailure(name, completed.returncode)
                results.append((name, completed.stdout))
            return tuple(results)

    def _extracted_evidence(
        self,
        context: ActionContext,
        inputs: ReceiptBoundLayoutSourceInputs,
        *,
        backend: str,
        logs: tuple[tuple[str, str], ...],
    ) -> PexEvidence:
        observed = dict(logs)
        if "--- CALIBRE xRC::PHDB GENERATOR COMPLETED" not in observed["phdb"]:
            raise RuntimeError("PHDB completion marker is absent")
        if (
            "----- CALIBRE xRC::HIERARCHICAL PARASITIC EXTRACTION COMPLETED"
            not in observed["pdb"]
            or _XRC_ERRORS_ZERO.search(observed["pdb"]) is None
        ):
            raise RuntimeError("PDB completion proof is absent")
        if (
            "--- CALIBRE xRC::FORMATTER COMPLETED" not in observed["fmt"]
            or _XRC_ERRORS_ZERO.search(observed["fmt"]) is None
            or 'Ascii file "extracted.pex" created.' not in observed["fmt"]
        ):
            raise RuntimeError("formatter completion proof is absent")

        work = context.work_root / "pex-kit"
        primary = inputs.receipt.target.name
        reports = context.output_root / "reports"
        main = copy_regular_backend_output(
            work / "extracted.pex", reports / "extracted.pex", "PEX netlist"
        )
        sidecar = copy_regular_backend_output(
            work / "extracted.pex.pex",
            reports / "extracted.pex.pex",
            "PEX distributed sidecar",
        )
        primary_sidecar = copy_regular_backend_output(
            work / f"extracted.pex.{primary}.pxi",
            reports / f"extracted.pex.{primary}.pxi",
            "PEX primary sidecar",
        )
        assert inputs.source_path is not None
        flattened = _self_contained_parasitics(
            main,
            sidecar,
            primary_sidecar,
            source_text=read_nofollow_text(inputs.source_path),
            primary=primary,
        )
        parasitics_path = context.output_path("parasitics", "parasitics.pex")
        parasitics_path.write_text(flattened, encoding="utf-8")
        identity = DerivedArtifactIdentity(
            "parasitics",
            PEX_NETLIST_KIND,
            f"{context.run_root.name}:parasitics",
        )
        assert inputs.source is not None
        return PexEvidence(
            PexStatus.EXTRACTED,
            inputs.layout,
            inputs.source,
            VerificationCompletion(backend, True, True, 0),
            identity,
            "Calibre xRC completed receipt-bound parasitic extraction",
        )

    def execute(self, context: ActionContext) -> AdapterExecution:
        inputs = load_receipt_bound_layout_source_inputs(context, require_source=True)
        timeout = self._configuration(context)
        backend, executable, deck, support = self._resources(context)
        try:
            logs = self._invoke(
                context,
                inputs,
                executable=executable,
                deck=deck,
                support=support,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            evidence = _failed_evidence(
                inputs,
                backend=backend,
                status=PexStatus.BACKEND_UNAVAILABLE,
                executed=False,
                exit_code=None,
                message=f"Calibre xRC backend unavailable: {exc}",
            )
        except _StageFailure as exc:
            evidence = _failed_evidence(
                inputs,
                backend=backend,
                status=PexStatus.EXECUTION_FAILED,
                executed=True,
                exit_code=exc.exit_code,
                message=f"Calibre xRC {exc.stage} stage exited {exc.exit_code}",
            )
        except Exception as exc:
            evidence = _failed_evidence(
                inputs,
                backend=backend,
                status=PexStatus.EXECUTION_FAILED,
                executed=True,
                exit_code=None,
                message=f"Calibre xRC execution or collection failed: {exc}",
            )
        else:
            try:
                evidence = self._extracted_evidence(
                    context, inputs, backend=backend, logs=logs
                )
            except (OSError, RuntimeError, UnicodeError, ValueError, TypeError) as exc:
                evidence = _failed_evidence(
                    inputs,
                    backend=backend,
                    status=PexStatus.EXECUTION_FAILED,
                    executed=True,
                    exit_code=0,
                    message=f"Calibre xRC completion could not be proven: {exc}",
                )
        evidence_path = context.output_path("evidence", "pex-evidence.json")
        evidence_path.write_text(evidence.canonical_json(), encoding="utf-8")
        return AdapterExecution.succeeded(details=_facts(evidence))

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        inputs = load_receipt_bound_layout_source_inputs(context, require_source=True)
        path = context.output_path("evidence", "pex-evidence.json")
        try:
            evidence = pex_evidence_from_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise FlowExecutionError(f"invalid receipt-bound PEX evidence: {exc}") from exc
        if evidence.layout != inputs.layout or evidence.source != inputs.source:
            raise FlowExecutionError("PEX evidence changed the checked input identity")
        facts = _facts(evidence)
        if dict(execution.details) != facts:
            raise FlowExecutionError("PEX execution details disagree with evidence")
        common = {
            "owner": inputs.layout.owner,
            "name": inputs.layout.name,
            "layout-identity": inputs.layout.artifact_identity,
            "source-identity": evidence.source.artifact_identity,
            "receipt-identity": inputs.receipt_identity,
            "job-identity": str(inputs.layout.job_identity),
            "result-identity": str(inputs.layout.result_identity),
            "plan-identity": inputs.layout.plan_identity,
            "status": evidence.status.value,
            "backend": evidence.completion.backend,
        }
        evidence_identity = pex_evidence_id(evidence)
        artifacts: list[ProducedArtifact] = [
            ProducedArtifact(
                "evidence",
                PEX_EVIDENCE_KIND,
                path,
                qualifiers={**common, "evidence-identity": evidence_identity},
            )
        ]
        if evidence.status is PexStatus.EXTRACTED:
            if evidence.parasitics is None:
                raise FlowExecutionError("extracted PEX omitted parasitic identity")
            parasitics = context.output_path("parasitics", "parasitics.pex")
            parasitic_text = read_nofollow_text(parasitics)
            primary = inputs.layout.name
            if evidence.parasitics.identity != f"{context.run_root.name}:parasitics":
                raise FlowExecutionError("PEX parasitic artifact identity drifted")
            try:
                _top_ports(parasitic_text, primary, "published PEX artifact")
            except RuntimeError as exc:
                raise FlowExecutionError(
                    f"PEX parasitic artifact structure is invalid: {exc}"
                ) from exc
            if _PARASITIC_ELEMENT.search(parasitic_text) is None:
                raise FlowExecutionError("PEX parasitic artifact contains no parasitics")
            artifacts.append(
                ProducedArtifact(
                    "parasitics",
                    PEX_NETLIST_KIND,
                    parasitics,
                    qualifiers={
                        **common,
                        "pex-evidence-identity": evidence_identity,
                        "parasitics-identity": evidence.parasitics.identity,
                    },
                )
            )
        reports = context.output_root / "reports"
        logs = tuple(sorted(context.log_root.glob("calibre-xrc-*.log")))
        report_files = tuple(sorted(reports.iterdir())) if reports.is_dir() else ()
        return CollectedActionResult(
            artifacts=tuple(artifacts),
            facts=facts,
            evidence=tuple(path for path in (*logs, *report_files) if path.is_file()),
        )


class _StageFailure(RuntimeError):
    def __init__(self, stage: str, exit_code: int) -> None:
        super().__init__(f"{stage} exited {exit_code}")
        self.stage = stage
        self.exit_code = exit_code


__all__ = [
    "CALIBRE_XRC_PEX_ADAPTER",
    "CalibreXrcPexAdapter",
    "render_calibre_xrc_deck",
]
