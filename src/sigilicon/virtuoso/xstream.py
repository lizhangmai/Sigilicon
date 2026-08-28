"""Guarded XStream GDSII export shared by OA layout workflows."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import os
from pathlib import Path
import re

from sigilicon.artifacts import read_nofollow_text
from sigilicon.external_tools import (
    cadence_subprocess_env,
    owned_directory,
    owned_input_file,
    run_process_group,
)
from sigilicon.layout.materialization_execution import (
    LayoutArtifactFormat,
    MaterializationExecutionError,
    canonicalize_gdsii_timestamps,
    validate_layout_content,
)


_XSTREAM_COMPLETE = re.compile(
    r"Translation completed\.\s+'0' error\(s\) and '0' warning\(s\) found\."
)
_CADENCE_GENERATED_STRUCTURE = re.compile(rb"^(?P<prefix>.+_CDNS_)[0-9]+$")


@dataclass(frozen=True)
class _GdsRecord:
    record_type: int
    data_type: int
    data: bytes


def _gds_records(payload: bytes) -> tuple[tuple[_GdsRecord, ...], bool]:
    records: list[_GdsRecord] = []
    offset = 0
    while offset < len(payload):
        length = int.from_bytes(payload[offset : offset + 2], "big")
        record_type = payload[offset + 2]
        records.append(
            _GdsRecord(
                record_type,
                payload[offset + 3],
                payload[offset + 4 : offset + length],
            )
        )
        offset += length
        if record_type == 0x04:
            break
    return tuple(records), offset < len(payload)


def _gds_name(data: bytes) -> bytes:
    return data[:-1] if data.endswith(b"\0") else data


def _gds_name_data(name: bytes) -> bytes:
    return name + (b"\0" if len(name) % 2 else b"")


def _structure_records(
    records: tuple[_GdsRecord, ...],
) -> dict[bytes, tuple[_GdsRecord, ...]]:
    structures: dict[bytes, tuple[_GdsRecord, ...]] = {}
    start: int | None = None
    for index, record in enumerate(records):
        if record.record_type == 0x05:
            start = index
        elif record.record_type == 0x07 and start is not None:
            structure = records[start : index + 1]
            names = tuple(
                _gds_name(item.data)
                for item in structure
                if item.record_type == 0x06
            )
            if len(names) != 1 or names[0] in structures:
                raise MaterializationExecutionError(
                    "XStream GDSII has ambiguous structure definitions"
                )
            structures[names[0]] = structure
            start = None
    return structures


def _canonical_generated_structure_names(
    structures: dict[bytes, tuple[_GdsRecord, ...]],
) -> dict[bytes, bytes]:
    generated = {
        name: match.group("prefix")
        for name in structures
        if (match := _CADENCE_GENERATED_STRUCTURE.fullmatch(name)) is not None
    }
    normalized: dict[bytes, bytes] = {}
    visiting: set[bytes] = set()

    def normalized_structure(name: bytes) -> bytes:
        if name in normalized:
            return normalized[name]
        if name in visiting:
            raise MaterializationExecutionError(
                "XStream GDSII generated structure hierarchy is cyclic"
            )
        visiting.add(name)
        chunks: list[bytes] = []
        for record in structures[name]:
            chunks.append(bytes((record.record_type, record.data_type)))
            data = record.data
            if record.record_type == 0x06:
                data = generated[name]
            elif record.record_type == 0x12:
                reference = _gds_name(data)
                if reference in generated:
                    data = generated[reference] + normalized_structure(reference)
                elif _CADENCE_GENERATED_STRUCTURE.fullmatch(reference) is not None:
                    raise MaterializationExecutionError(
                        "XStream GDSII references an undefined generated structure"
                    )
            chunks.append(len(data).to_bytes(4, "big"))
            chunks.append(data)
        visiting.remove(name)
        normalized[name] = b"".join(chunks)
        return normalized[name]

    ordered = sorted(
        generated,
        key=lambda name: (generated[name], normalized_structure(name), name),
    )
    replacements = {
        name: generated[name] + f"{index:016x}".encode("ascii")
        for index, name in enumerate(ordered)
    }
    if len(set(replacements.values())) != len(replacements):
        raise MaterializationExecutionError(
            "XStream GDSII generated structure identities are ambiguous"
        )
    return replacements


def canonicalize_xstream_gdsii(payload: bytes) -> bytes:
    """Remove timestamp and generated-PCell identity variance from XStream GDSII."""

    canonical = canonicalize_gdsii_timestamps(payload)
    records, padded = _gds_records(canonical)
    replacements = _canonical_generated_structure_names(
        _structure_records(records)
    )
    if not replacements:
        return canonical
    rewritten: list[bytes] = []
    for record in records:
        data = record.data
        if record.record_type in {0x06, 0x12}:
            name = _gds_name(data)
            if name in replacements:
                data = _gds_name_data(replacements[name])
        rewritten.append(
            (len(data) + 4).to_bytes(2, "big")
            + bytes((record.record_type, record.data_type))
            + data
        )
    result = b"".join(rewritten)
    if padded:
        result += bytes((-len(result)) % 2048)
    validate_layout_content(result, LayoutArtifactFormat.GDSII)
    return result


class XStreamExportError(RuntimeError):
    """XStream did not prove one complete GDSII translation."""

    def __init__(
        self,
        message: str,
        *,
        executed: bool,
        exit_code: int | None,
        diagnostic_path: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.executed = executed
        self.exit_code = exit_code
        self.diagnostic_path = diagnostic_path


@dataclass(frozen=True)
class XStreamExportRequest:
    executable: Path
    library: str
    cell: str
    view: str
    technology_library: str
    layer_map: Path
    cds_lib: Path
    work_root: Path
    timeout_seconds: int = 120
    flatten_pcells: bool = False
    suppressed_warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label, value in (
            ("library", self.library),
            ("cell", self.cell),
            ("view", self.view),
            ("technology library", self.technology_library),
        ):
            if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
                raise ValueError(f"XStream {label} must be non-empty text")
        if type(self.timeout_seconds) is not int or self.timeout_seconds <= 0:
            raise ValueError("XStream timeout must be a positive integer")
        if type(self.flatten_pcells) is not bool:
            raise ValueError("XStream flatten_pcells must be boolean")
        if not isinstance(self.suppressed_warnings, tuple) or any(
            not isinstance(item, str)
            or re.fullmatch(r"XSTRM-[0-9]+", item) is None
            for item in self.suppressed_warnings
        ):
            raise ValueError(
                "XStream suppressed warnings must be typed XSTRM-<number> identities"
            )
        if len(set(self.suppressed_warnings)) != len(self.suppressed_warnings):
            raise ValueError("XStream suppressed warnings contain duplicates")
        # Cadence launchers are commonly multicall symlinks whose invoked path
        # determines their installation root.  Preserve that exact explicit
        # launcher while resolving ordinary data and managed directory paths.
        object.__setattr__(
            self,
            "executable",
            Path(os.path.abspath(self.executable)),
        )
        for name in ("layer_map", "cds_lib", "work_root"):
            object.__setattr__(self, name, Path(getattr(self, name)).resolve())


@dataclass(frozen=True)
class XStreamExportResult:
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    gds_path: Path
    native_log_path: Path
    summary_path: Path


def _prepend(environment: dict[str, str], name: str, value: Path) -> None:
    existing = environment.get(name, "")
    environment[name] = str(value) + (os.pathsep + existing if existing else "")


def xstream_environment(executable: Path) -> dict[str, str]:
    """Construct Cadence's subprocess environment from one explicit launcher."""

    environment = cadence_subprocess_env()
    cds_home = Path(environment.get("CDSHOME", executable.parents[3]))
    environment.setdefault("CDSHOME", str(cds_home))
    environment.setdefault("CDSROOT", str(cds_home))
    environment.setdefault("CDS_INST_DIR", str(cds_home))
    environment.setdefault("OA_HOME", str(cds_home / "oa_v22.62.021"))
    _prepend(environment, "LD_LIBRARY_PATH", cds_home / "tools.lnx86" / "lib")
    return environment


def _write_failure_diagnostic(
    work: Path,
    *,
    exit_code: int,
    stdout: str,
) -> Path:
    """Persist bounded translator evidence before raising on a nonzero exit."""

    sections = [f"exit_code={exit_code}", "", "[stdout]", stdout[-65536:]]
    for name in ("strmout.log", "strmout.sum"):
        path = work / name
        sections.extend(("", f"[{name}]"))
        if not path.is_file() or path.is_symlink():
            sections.append("<not a regular file>")
            continue
        try:
            sections.append(read_nofollow_text(path, errors="replace")[-65536:])
        except OSError as exc:
            sections.append(f"<unreadable: {exc}>")
    diagnostic = work / "xstream-failure.log"
    diagnostic.write_text("\n".join(sections), encoding="utf-8")
    return diagnostic


def run_xstream_export(request: XStreamExportRequest) -> XStreamExportResult:
    """Export one exact OA cellview and require authoritative XStream completion."""

    executable = Path(os.path.abspath(request.executable))
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise XStreamExportError(
            "resolved XStream executable is unavailable",
            executed=False,
            exit_code=None,
        )
    for path, label in (
        (request.layer_map, "XStream layer map"),
        (request.cds_lib, "XStream cds.lib"),
    ):
        if not path.is_file() or path.is_symlink():
            raise XStreamExportError(
                f"{label} is not a regular file: {path}",
                executed=False,
                exit_code=None,
            )
    work = request.work_root
    work.mkdir(parents=True, exist_ok=True)
    with owned_directory(work) as owned_work, ExitStack() as resources:
        owned_map = resources.enter_context(owned_input_file(request.layer_map))
        owned_cds = resources.enter_context(
            owned_input_file(request.cds_lib, require_single_link=False)
        )
        command_parts = [
            str(executable),
            "-library",
            request.library,
            "-strmFile",
            str(work / "layout.gds"),
            "-runDir",
            str(work),
            "-topCell",
            request.cell,
            "-view",
            request.view,
            "-logFile",
            str(work / "strmout.log"),
            "-summaryFile",
            str(work / "strmout.sum"),
            "-techLib",
            request.technology_library,
            "-layerMap",
            owned_map.child_named_path,
        ]
        if request.flatten_pcells:
            command_parts.append("-flattenPcells")
        if request.suppressed_warnings:
            command_parts.extend(
                (
                    "-noWarn",
                    " ".join(
                        warning.removeprefix("XSTRM-")
                        for warning in request.suppressed_warnings
                    ),
                )
            )
        command_parts.extend(
            (
                "-flattenVias",
                "-convertPin",
                "geometryAndText",
                "-cdslib",
                owned_cds.child_named_path,
            )
        )
        command = tuple(command_parts)

        def validate_spawn() -> None:
            owned_map.require_visible()
            owned_cds.require_visible()

        try:
            completed = run_process_group(
                command,
                cwd=work,
                env=xstream_environment(executable),
                timeout=request.timeout_seconds,
                before_spawn=validate_spawn,
                pass_fds=(
                    owned_work.fd,
                    owned_map.fd,
                    owned_map.directory_fd,
                    owned_cds.fd,
                    owned_cds.directory_fd,
                ),
            )
        except FileNotFoundError as exc:
            raise XStreamExportError(
                f"XStream backend unavailable: {exc}",
                executed=False,
                exit_code=None,
            ) from exc
        except Exception as exc:
            raise XStreamExportError(
                f"XStream execution failed: {exc}",
                executed=True,
                exit_code=None,
            ) from exc

    native_log = work / "strmout.log"
    summary = work / "strmout.sum"
    gds = work / "layout.gds"
    if completed.returncode != 0:
        diagnostic = _write_failure_diagnostic(
            work,
            exit_code=completed.returncode,
            stdout=completed.stdout,
        )
        raise XStreamExportError(
            f"XStream exited {completed.returncode}; see managed xstream-failure.log",
            executed=True,
            exit_code=completed.returncode,
            diagnostic_path=diagnostic,
        )
    for path, label in (
        (native_log, "native log"),
        (summary, "summary"),
        (gds, "GDSII output"),
    ):
        if not path.is_file() or path.is_symlink():
            raise XStreamExportError(
                f"XStream did not produce a regular {label}",
                executed=True,
                exit_code=0,
            )
    proof = "\n".join(
        (
            read_nofollow_text(native_log, errors="replace"),
            read_nofollow_text(summary, errors="replace"),
            completed.stdout,
        )
    )
    if _XSTREAM_COMPLETE.search(proof) is None:
        raise XStreamExportError(
            "XStream summary does not prove a zero-warning translation",
            executed=True,
            exit_code=0,
        )
    if gds.stat().st_size <= 0:
        raise XStreamExportError(
            "XStream produced an empty GDSII output",
            executed=True,
            exit_code=0,
        )
    return XStreamExportResult(
        command=command,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        gds_path=gds,
        native_log_path=native_log,
        summary_path=summary,
    )


__all__ = [
    "XStreamExportError",
    "XStreamExportRequest",
    "XStreamExportResult",
    "canonicalize_xstream_gdsii",
    "run_xstream_export",
    "xstream_environment",
]
