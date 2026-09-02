"""Direct structural macro link against one immutable release."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tomllib
from typing import Any, Mapping

from sigilicon.artifacts import _inspect_nofollow_file, read_nofollow_text
from sigilicon.external_tools import (
    owned_directory,
    owned_executable,
    owned_input_file,
    run_process_group_capture,
)
from sigilicon.execution.step_files import StepFiles
from sigilicon.workflows.ip_packaging import audit_ip_release_manifest


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
    path: Path,
    plan: "StructuralLinkPlan",
) -> tuple[int, int] | None:
    if not path.is_file() or path.is_symlink():
        return None
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
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
        return tomllib.loads(read_nofollow_text(path))
    except (OSError, UnicodeError, RuntimeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read structural-link {label}") from exc


def _manifest(path: Path) -> tuple[dict[str, Any], str]:
    try:
        snapshot = read_nofollow_text(path)
        value = audit_ip_release_manifest(path)
        if json.loads(snapshot) != value:
            raise RuntimeError("release manifest changed while auditing")
    except (OSError, UnicodeError, RuntimeError, json.JSONDecodeError) as exc:
        raise ValueError("cannot read structural-link release manifest") from exc
    return value, hashlib.sha256(snapshot.encode("utf-8")).hexdigest()


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
    release_manifest: str
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
    liberty_role: str,
    release_manifest: Path,
    release_liberty: Path,
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
    if not dependency or not release_export or not liberty_role:
        raise ValueError("structural-link release selection is incomplete")
    if _TOOL_VERSION.fullmatch(library_compiler_version) is None:
        raise ValueError("structural-link Library Compiler version is invalid")

    variant_document = _toml(variant_path, "variant")
    if (
        variant_document.get("schema") != 1
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
        selected_roles = variant_document["filesets"]["synthesis"][
            "dependency_roles"
        ][dependency]
    except (KeyError, TypeError) as exc:
        raise ValueError("structural-link variant omits its release selection") from exc
    if selected_variant != variant or selected_roles != [liberty_role]:
        raise ValueError("structural-link variant release selection is inconsistent")

    lock = _toml(dependency_lock_path, "dependency lock")
    if (
        lock.get("schema") != 1
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
    pinned = matches[0]
    manifest, manifest_digest = _manifest(release_manifest)
    if manifest_digest != pinned.get("manifest_sha256"):
        raise ValueError("structural-link release manifest differs from its lock")
    if not release_manifest.as_posix().endswith(_text(pinned, "manifest", "lock")):
        raise ValueError("structural-link release manifest path differs from its lock")
    if (
        manifest.get("ip_name") != dependency
        or manifest.get("owner") != dependency
        or manifest.get("release_id") != pinned.get("release_id")
        or manifest.get("source_commit") != pinned.get("source_commit")
    ):
        raise ValueError("structural-link release identity differs from its lock")
    maturity = manifest.get("maturity")
    if (
        not isinstance(maturity, Mapping)
        or maturity.get("level") != pinned.get("maturity")
    ):
        raise ValueError("structural-link release maturity is invalid")
    artifacts = manifest.get("views")
    matches = (
        [
            item
            for item in artifacts
            if isinstance(item, Mapping)
            and item.get("export") == release_export
            and item.get("role") == liberty_role
        ]
        if isinstance(artifacts, list)
        else []
    )
    if len(matches) != 1:
        raise ValueError("structural-link release does not contain one macro Liberty")
    released = matches[0]
    released_path = Path(_text(released, "path", "manifest"))
    if released_path.is_absolute() or any(
        part in {"", ".", ".."} for part in released_path.parts
    ):
        raise ValueError("structural-link Liberty manifest path is unsafe")
    expected_liberty = (release_manifest.parent / released_path).resolve()
    if not expected_liberty.is_relative_to(release_manifest.parent.resolve()):
        raise ValueError("structural-link Liberty escapes its release package")
    if release_liberty.resolve() != expected_liberty:
        raise ValueError("structural-link Liberty path differs from the release manifest")
    try:
        liberty_metadata, liberty_digest = _inspect_nofollow_file(release_liberty)
    except (OSError, RuntimeError) as exc:
        raise ValueError("structural-link Liberty is missing or unsafe") from exc
    if (
        released.get("size") != liberty_metadata.st_size
        or released.get("sha256") != liberty_digest
    ):
        raise ValueError(
            "structural-link Liberty content differs from the release manifest"
        )
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
        release_id=pinned["release_id"],
        release_source_commit=pinned["source_commit"],
        release_manifest=pinned["manifest"],
        release_manifest_sha256=manifest_digest,
        release_liberty_sha256=liberty_digest,
        release_sources=(release_manifest, release_liberty),
    )


def execute_structural_link(
    plan: StructuralLinkPlan,
    *,
    artifacts: StepFiles,
    library_compiler: Path,
    design_compiler: Path,
    environment: Mapping[str, str],
    timeout: int,
) -> StructuralLinkExecution:
    """Run Library Compiler and DC while every input and output root is held."""

    work = artifacts.directory("work")
    macro_db = work / "structural-macro.db"
    report = work / "structural-link.rpt"
    checkpoint = work / f"{plan.top}.ddc"
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
        held_lc = stack.enter_context(owned_executable(library_compiler))
        held_dc = stack.enter_context(owned_executable(design_compiler))
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

        lc = run_process_group_capture(
            [*held_lc.command, "-f", held_compile.child_named_path],
            cwd=Path(held_work.child_path),
            env=child_environment,
            timeout=timeout,
            before_spawn=visible,
            pass_fds=(held_work.fd,),
        )
        artifacts.write_text("outputs", ("library-compiler.stdout.log",), lc.stdout)
        artifacts.write_text("outputs", ("library-compiler.stderr.log",), lc.stderr or "")
        lc_marker = f"SIGILICON_STRUCTURAL_DB_PASS library={plan.library_name}"
        observed_lc_version = _observed_library_compiler_version(lc.stdout)
        lc_clean = (
            lc.returncode == 0
            and observed_lc_version == plan.library_compiler_version
            and lc_marker in lc.stdout
            and macro_db.is_file()
            and not macro_db.is_symlink()
            and macro_db.stat().st_size > 0
            and not any(
                line.startswith(("Error:", "Fatal:"))
                for line in f"{lc.stdout}\n{lc.stderr or ''}".splitlines()
            )
        )
        dc = None
        if lc_clean:
            dc = run_process_group_capture(
                [*held_dc.command, "-f", held_link.child_named_path],
                cwd=Path(held_work.child_path),
                env=child_environment,
                timeout=timeout,
                before_spawn=visible,
                pass_fds=(held_work.fd,),
            )
            artifacts.write_text("outputs", ("design-compiler.stdout.log",), dc.stdout)
            artifacts.write_text("outputs", ("design-compiler.stderr.log",), dc.stderr or "")

    observed = _structural_report(report, plan)
    marker = (
        f"SIGILICON_STRUCTURAL_LINK_PASS top={plan.top} "
        f"macro_instances={plan.expected_macro_instances} "
        f"unresolved={plan.expected_unresolved_references}"
    )
    dc_clean = bool(
        dc is not None
        and dc.returncode == 0
        and marker in dc.stdout
        and checkpoint.is_file()
        and not checkpoint.is_symlink()
        and checkpoint.stat().st_size > 0
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
        artifacts.copy_file("outputs", ("structural-macro.db",), macro_db)
        artifacts.copy_file("outputs", ("structural-link.rpt",), report)
        artifacts.copy_file("outputs", (f"{plan.top}.ddc",), checkpoint)
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
            "manifest": plan.release_manifest,
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
