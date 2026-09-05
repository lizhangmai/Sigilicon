"""Direct structural macro link against one immutable release."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from sigilicon.contracts import read_toml
from sigilicon.domain.ip_integration import parse_locked_ip_release
from sigilicon.external_tools import (
    ProcessRequest,
    managed_process,
    owned_directory,
    owned_input_file,
)
from sigilicon.execution._model import Resources
from sigilicon.execution._workspace import ExecutionWorkspace
from sigilicon.release_store import (
    ReleaseRef,
    ReleaseStore,
    release_store_resource,
)
from sigilicon.adapters.release.ip_packaging import validate_ip_release_package


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_TOOL_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
_VERSION_BANNER = re.compile(r"\bVersion\s+([A-Za-z0-9][A-Za-z0-9._+-]*)\b")


def _sha256_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while block := os.pread(descriptor, 1024 * 1024, offset):
        digest.update(block)
        offset += len(block)
    return digest.hexdigest()


def _structural_report(
    payload: bytes,
    plan: "StructuralLinkPlan",
) -> tuple[int, int] | None:
    fields: dict[str, str] = {}
    for line in payload.decode("utf-8", errors="replace").splitlines():
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name in fields:
            return None
        fields[name] = value
    expected_text = {
        "scope": "structural-link-only",
        "timing_characterized": "false",
        "power_characterized": "false",
        "area_characterized": "false",
        "top": plan.top,
        "parameter_overrides": ",".join(
            f"{name}={value}" for name, value in plan.parameter_overrides.items()
        ),
        "macro_cell": plan.macro_cell,
    }
    if any(fields.get(name) != value for name, value in expected_text.items()):
        return None
    try:
        return int(fields["macro_instances"]), int(fields["unresolved_references"])
    except (KeyError, ValueError):
        return None


def _toml(path: Path, label: str) -> dict[str, Any]:
    try:
        return read_toml(path)
    except ValueError as exc:
        raise ValueError(f"cannot read structural-link {label}") from exc


def _text(document: Mapping[str, Any], name: str, label: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"structural-link {label} requires text {name!r}")
    return value


def _observed_library_compiler_version(output: str) -> str | None:
    versions = {
        match.group(1)
        for line in output.splitlines()
        if (match := _VERSION_BANNER.search(line)) is not None
    }
    return next(iter(versions)) if len(versions) == 1 else None


@dataclass(frozen=True)
class StructuralLinkPlan:
    owner: str
    variant: str
    top: str
    rtl_sources: tuple[Path, ...]
    compile_script: Path
    link_script: Path
    library_name: str
    macro_cell: str
    parameter_overrides: Mapping[str, int]
    expected_macro_instances: int
    expected_unresolved_references: int
    library_compiler_version: str
    release_liberty: Path
    release_id: str
    release_source_commit: str
    release_store: str
    release_manifest_sha256: str
    release_liberty_sha256: str
    release_sources: tuple[Path, ...]


@dataclass(frozen=True)
class StructuralLinkExecution:
    passed: bool
    status: str
    facts: Mapping[str, object]


def plan_structural_link(
    *,
    owner: str,
    dependency: str,
    dependency_lock_path: Path,
    variant_path: Path,
    variant: str,
    rtl_sources: tuple[Path, ...],
    compile_script: Path,
    link_script: Path,
    library_name: str,
    macro_cell: str,
    parameter_overrides: Mapping[str, int],
    expected_macro_instances: int,
    expected_unresolved_references: int,
    library_compiler_version: str,
    release_export: str,
    liberty_view: str,
    resources: Resources,
) -> StructuralLinkPlan:
    """Validate direct operation inputs and one exact locked release."""

    if not _IDENTIFIER.fullmatch(library_name) or not _IDENTIFIER.fullmatch(macro_cell):
        raise ValueError("structural-link library and macro names must be identifiers")
    if not parameter_overrides or any(
        not isinstance(name, str)
        or not _IDENTIFIER.fullmatch(name)
        or type(value) is not int
        or value <= 0
        for name, value in parameter_overrides.items()
    ):
        raise ValueError("structural-link parameters must be positive integers")
    if (
        type(expected_macro_instances) is not int
        or expected_macro_instances <= 0
        or type(expected_unresolved_references) is not int
        or expected_unresolved_references < 0
    ):
        raise ValueError("structural-link expected results are invalid")
    if not dependency or not release_export or not liberty_view:
        raise ValueError("structural-link release selection is incomplete")
    if _TOOL_VERSION.fullmatch(library_compiler_version) is None:
        raise ValueError("structural-link Library Compiler version is invalid")

    variant_document = _toml(variant_path, "variant")
    if (
        variant_document.get("schema") != 2
        or variant_document.get("contract_kind") != "ip-operating-variant"
        or variant_document.get("owner") != owner
    ):
        raise ValueError("structural-link variant identity is invalid")
    try:
        top = variant_document["filesets"]["synthesis"]["top_module"]
    except (KeyError, TypeError) as exc:
        raise ValueError("structural-link variant omits its synthesis top") from exc
    if not isinstance(top, str) or not _IDENTIFIER.fullmatch(top):
        raise ValueError("structural-link synthesis top is invalid")
    try:
        selected_variant = variant_document["integration"]["variant"]
        selected_views = variant_document["filesets"]["synthesis"][
            "dependency_views"
        ][dependency]
    except (KeyError, TypeError) as exc:
        raise ValueError("structural-link variant omits its release selection") from exc
    if selected_variant != variant or selected_views != [liberty_view]:
        raise ValueError("structural-link variant release selection is inconsistent")

    lock = _toml(dependency_lock_path, "dependency lock")
    if (
        lock.get("schema") != 3
        or lock.get("contract_kind") != "ip-dependency-lock"
        or lock.get("owner") != owner
    ):
        raise ValueError("structural-link dependency lock identity is invalid")
    dependencies = lock.get("dependency")
    matches = (
        [
            item
            for item in dependencies
            if isinstance(item, Mapping) and item.get("name") == dependency
        ]
        if isinstance(dependencies, list)
        else []
    )
    if len(matches) != 1:
        raise ValueError("structural-link dependency lock does not select one provider")
    pinned = parse_locked_ip_release(
        matches[0], "structural-link dependency lock entry"
    )
    ref = ReleaseRef(pinned.store, pinned.manifest_sha256)
    release_store_root = Path(
        resources.require_destination(release_store_resource(ref.store))
    )
    audited = ReleaseStore(release_store_root).open(
        ref,
        validate=validate_ip_release_package,
    )
    release_manifest = audited.manifest_path
    manifest = audited.manifest
    manifest_digest = ref.manifest_sha256
    if (
        manifest.get("ip_name") != dependency
        or manifest.get("owner") != dependency
        or manifest.get("release_id") != pinned.release_id
        or manifest.get("source_commit") != pinned.source_commit
    ):
        raise ValueError("structural-link release identity differs from its lock")
    maturity = manifest.get("maturity")
    if (
        not isinstance(maturity, Mapping)
        or maturity.get("level") != pinned.maturity
    ):
        raise ValueError("structural-link release maturity is invalid")
    try:
        liberty = audited.view(release_export, liberty_view)
    except RuntimeError as exc:
        raise ValueError(
            "structural-link release does not contain one macro Liberty"
        ) from exc
    release_liberty = liberty.path
    liberty_digest = liberty.sha256
    if not rtl_sources or len(set(rtl_sources)) != len(rtl_sources):
        raise ValueError("structural-link requires unique RTL sources")
    if any(path.suffix.lower() not in {".sv", ".v"} for path in rtl_sources):
        raise ValueError("structural-link RTL sources must be Verilog/SystemVerilog")

    return StructuralLinkPlan(
        owner=owner,
        variant=variant,
        top=top,
        rtl_sources=rtl_sources,
        compile_script=compile_script,
        link_script=link_script,
        library_name=library_name,
        macro_cell=macro_cell,
        parameter_overrides=dict(parameter_overrides),
        expected_macro_instances=expected_macro_instances,
        expected_unresolved_references=expected_unresolved_references,
        library_compiler_version=library_compiler_version,
        release_liberty=release_liberty,
        release_id=pinned.release_id,
        release_source_commit=pinned.source_commit,
        release_store=ref.store,
        release_manifest_sha256=manifest_digest,
        release_liberty_sha256=liberty_digest,
        release_sources=(release_manifest, release_liberty),
    )


def execute_structural_link(
    plan: StructuralLinkPlan,
    *,
    artifacts: ExecutionWorkspace,
    resources: Resources,
    library_compiler: str,
    design_compiler: str,
    environment: Mapping[str, str],
    timeout: int,
) -> StructuralLinkExecution:
    """Run Library Compiler and DC while every input and output root is held."""

    work = artifacts.directory("work")
    checkpoint_name = f"{plan.top}.ddc"
    child_environment = dict(environment)
    child_environment.pop("LD_PRELOAD", None)
    child_environment.update(
        {
            "SIGILICON_STRUCTURAL_LIBRARY": plan.library_name,
            "SIGILICON_STRUCTURAL_MACRO_CELL": plan.macro_cell,
            "SIGILICON_STRUCTURAL_TOP": plan.top,
            "SIGILICON_STRUCTURAL_PARAMETERS": ",".join(
                f"{name}={value}" for name, value in plan.parameter_overrides.items()
            ),
        }
    )
    with ExitStack() as stack:
        held_work = stack.enter_context(owned_directory(work))
        held_lc = stack.enter_context(resources.owned_tool(library_compiler))
        held_dc = stack.enter_context(resources.owned_tool(design_compiler))
        held_compile = stack.enter_context(
            owned_input_file(plan.compile_script, require_single_link=False)
        )
        held_link = stack.enter_context(
            owned_input_file(plan.link_script, require_single_link=False)
        )
        held_liberty = stack.enter_context(
            owned_input_file(plan.release_liberty, require_single_link=False)
        )
        if _sha256_fd(held_liberty.fd) != plan.release_liberty_sha256:
            raise RuntimeError(
                "structural-link Liberty changed between planning and execution"
            )
        held_rtl = [
            stack.enter_context(owned_input_file(path, require_single_link=False))
            for path in plan.rtl_sources
        ]
        sources_file = artifacts.write_text(
            "work",
            ("sources.f",),
            "".join(f"{held.child_named_path}\n" for held in held_rtl),
        )
        held_sources = stack.enter_context(
            owned_input_file(sources_file, require_single_link=False)
        )
        child_environment.update(
            {
                "SIGILICON_STRUCTURAL_LIBERTY": held_liberty.child_named_path,
                "SIGILICON_STRUCTURAL_DB": f"{held_work.child_path}/structural-macro.db",
                "SIGILICON_STRUCTURAL_SOURCES": held_sources.child_named_path,
                "SIGILICON_STRUCTURAL_REPORT": f"{held_work.child_path}/structural-link.rpt",
                "SIGILICON_STRUCTURAL_CHECKPOINT": f"{held_work.child_path}/{plan.top}.ddc",
            }
        )
        def visible() -> None:
            held_work.require_visible()
            for item in (
                held_lc,
                held_dc,
                held_compile,
                held_link,
                held_liberty,
                held_sources,
                *held_rtl,
            ):
                item.require_visible()

        lc = managed_process.run(ProcessRequest(
            argv=(*held_lc.command, "-f", held_compile.child_named_path),
            executable=held_lc.executable,
            cwd=Path(held_work.child_path),
            environment=child_environment,
            timeout_seconds=timeout,
            before_spawn=visible,
            pass_fds=(held_work.fd,),
        ))
        artifacts.write_text("outputs", ("library-compiler.stdout.log",), lc.stdout)
        artifacts.write_text("outputs", ("library-compiler.stderr.log",), lc.stderr or "")
        macro_db = held_work.read_child_bytes(
            "structural-macro.db",
            missing_ok=True,
        )
        lc_marker = f"SIGILICON_STRUCTURAL_DB_PASS library={plan.library_name}"
        observed_lc_version = _observed_library_compiler_version(lc.stdout)
        lc_clean = (
            lc.returncode == 0
            and observed_lc_version == plan.library_compiler_version
            and lc_marker in lc.stdout
            and macro_db is not None
            and len(macro_db) > 0
            and not any(
                line.startswith(("Error:", "Fatal:"))
                for line in f"{lc.stdout}\n{lc.stderr or ''}".splitlines()
            )
        )
        dc = None
        report = None
        checkpoint = None
        if lc_clean:
            dc = managed_process.run(ProcessRequest(
                argv=(*held_dc.command, "-f", held_link.child_named_path),
                executable=held_dc.executable,
                cwd=Path(held_work.child_path),
                environment=child_environment,
                timeout_seconds=timeout,
                before_spawn=visible,
                pass_fds=(held_work.fd,),
            ))
            artifacts.write_text("outputs", ("design-compiler.stdout.log",), dc.stdout)
            artifacts.write_text("outputs", ("design-compiler.stderr.log",), dc.stderr or "")
            report = held_work.read_child_bytes(
                "structural-link.rpt",
                missing_ok=True,
            )
            checkpoint = held_work.read_child_bytes(
                checkpoint_name,
                missing_ok=True,
            )

    observed = _structural_report(report, plan) if report is not None else None
    marker = (
        f"SIGILICON_STRUCTURAL_LINK_PASS top={plan.top} "
        f"macro_instances={plan.expected_macro_instances} "
        f"unresolved={plan.expected_unresolved_references}"
    )
    dc_clean = bool(
        dc is not None
        and dc.returncode == 0
        and marker in dc.stdout
        and checkpoint is not None
        and len(checkpoint) > 0
        and observed
        == (
            plan.expected_macro_instances,
            plan.expected_unresolved_references,
        )
        and not any(
            line.startswith(("Error:", "Fatal:"))
            for line in f"{dc.stdout}\n{dc.stderr or ''}".splitlines()
        )
    )
    passed = lc_clean and dc_clean
    if passed:
        assert macro_db is not None
        assert report is not None
        assert checkpoint is not None
        artifacts.write_bytes("outputs", ("structural-macro.db",), macro_db)
        artifacts.write_bytes("outputs", ("structural-link.rpt",), report)
        artifacts.write_bytes("outputs", (checkpoint_name,), checkpoint)
    facts: dict[str, object] = {
        "passed": passed,
        "library_compiler_version": observed_lc_version,
        "required_library_compiler_version": plan.library_compiler_version,
        "macro_instance_count": observed[0] if passed and observed is not None else None,
        "unresolved_reference_count": (
            observed[1] if passed and observed is not None else None
        ),
        "timing_characterized": False,
        "power_characterized": False,
        "area_characterized": False,
        "product_qualification_conclusion": False,
    }
    artifacts.write_json(
        "outputs",
        ("structural-link-evidence.json",),
        {
            "schema": 1,
            "contract_kind": "structural-link-evidence",
            "owner": plan.owner,
            "variant": plan.variant,
            "top": plan.top,
            "status": "linked" if passed else "execution_failed",
            "parameter_overrides": dict(plan.parameter_overrides),
            "macro_cell": plan.macro_cell,
            "expected_macro_instances": plan.expected_macro_instances,
            "expected_unresolved_references": plan.expected_unresolved_references,
            "release_id": plan.release_id,
            "release_source_commit": plan.release_source_commit,
            "release_store": plan.release_store,
            "manifest_sha256": plan.release_manifest_sha256,
            "macro_liberty_sha256": plan.release_liberty_sha256,
            **facts,
        },
    )
    return StructuralLinkExecution(
        passed=passed,
        status="linked" if passed else "execution_failed",
        facts=facts,
    )


__all__ = [
    "StructuralLinkExecution",
    "StructuralLinkPlan",
    "execute_structural_link",
    "plan_structural_link",
]
