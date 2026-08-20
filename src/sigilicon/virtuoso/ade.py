"""Native Cadence config and Maestro materialization adapter."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any
import uuid

from sigilicon.virtuoso.capability import dispatch_oa_mutation
from sigilicon.virtuoso.confirmation import require_bridge_confirmation
from sigilicon.virtuoso.maestro import build_owned_maestro_setup_transaction_skill
from sigilicon.virtuoso.oa import (
    audit_cellview_delta_skill,
    own_synchronous_cellview_delta_skill,
    skill_quote,
)

def _load_native_setup_skill(source: Path, invocation: str) -> str:
    """Load one source-owned SKILL setup and invoke a named entry point."""

    if not source.is_file():
        raise ValueError(f"native ADE setup source does not exist: {source}")
    return f'''let((loaded invoked)
  loaded = errset(load({skill_quote(str(source))}) nil)
  unless(loaded && car(loaded)
    error("native ADE setup source load failed"))
  invoked = errset({invocation} nil)
  unless(invoked && car(invoked)
    error(sprintf(nil "native ADE setup entry point failed: %L" invoked)))
  t
)'''


def _native_setup_entry_point(
    source: Path,
    *,
    declared: str,
) -> str:
    """Verify one project-declared native setup entry point."""

    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read native ADE setup source: {source}") from exc
    if re.search(rf"\bprocedure\({re.escape(declared)}\s*\(", text) is None:
        raise ValueError(
            f"native ADE setup does not define declared entry point {declared}: "
            f"{source}"
        )
    return declared


def _native_setup_test_name(source: Path) -> str:
    """Extract the one source-owned test identity used by a native setup."""

    text = source.read_text(encoding="utf-8")
    names = tuple(
        re.findall(r"\bmaeCreateTest\(\s*\"([A-Za-z_][A-Za-z0-9_$]*)\"", text)
    )
    if len(names) != 1:
        raise ValueError(
            "native Maestro setup must declare exactly one maeCreateTest identity: "
            f"{source}"
        )
    return names[0]


def create_oa_native_config_view(
    client: Any,
    spec: Any,
    *,
    reference_libraries: tuple[str, ...],
    operation: Any,
    timeout: int = 180,
) -> None:
    """Materialize a config from the canonical Cadence SKILL setup source."""

    native_setup = spec.native_setup
    if native_setup is None:
        raise ValueError("native config materialization requires schema-3 setup")
    references = " ".join(reference_libraries)
    entry_point = _native_setup_entry_point(
        native_setup.source,
        declared=native_setup.config_procedure,
    )
    invocation = (
        f"{entry_point}({skill_quote(spec.library)} "
        f"{skill_quote(spec.cell)} {skill_quote(spec.dut)} "
        f"{skill_quote(spec.top_view)} {skill_quote(references)})"
    )
    source = _load_native_setup_skill(native_setup.source, invocation)
    source = own_synchronous_cellview_delta_skill(
        source,
        label=f"native config setup {spec.library}/{spec.cell}",
    )
    result = require_bridge_confirmation(
        operation,
        f"create native config {spec.library}/{spec.cell}",
        lambda: dispatch_oa_mutation(
            operation,
            client,
            library=spec.library,
            cell=spec.cell,
            phase="create native config view SKILL dispatch",
            callback=lambda: client.execute_skill(
                audit_cellview_delta_skill(
                    source,
                    label=f"native config setup {spec.library}/{spec.cell}",
                    mutation_target=(spec.library, (spec.cell,)),
                ),
                timeout=timeout,
            ),
        ),
    )
    if result.errors:
        raise RuntimeError(
            f"failed to create native config {spec.library}/{spec.cell}: "
            f"{result.errors[0]}"
        )


def create_oa_native_maestro_view(
    client: Any,
    spec: Any,
    *,
    operation: Any,
    timeout: int = 300,
) -> None:
    """Materialize a Maestro setup by executing the canonical SKILL source."""

    native_setup = spec.native_setup
    if native_setup is None:
        raise ValueError("native Maestro materialization requires schema-3 setup")
    scope_token = uuid.uuid4().hex
    operation.record_ownership_scope(
        "maestro-setup",
        scope_token=scope_token,
        library=spec.library,
        cell=spec.cell,
    )
    entry_point = _native_setup_entry_point(
        native_setup.source,
        declared=native_setup.maestro_procedure,
    )
    canonical_test = _native_setup_test_name(native_setup.source)
    platform_model = native_setup.pdk.simulation.default
    invocation = (
        f"{entry_point}(session {skill_quote(spec.library)} "
        f"{skill_quote(spec.cell)} {skill_quote(str(platform_model.file))} "
        f"{skill_quote(platform_model.single_section)})"
    )
    body = _load_native_setup_skill(native_setup.source, invocation)
    source = own_synchronous_cellview_delta_skill(
        build_owned_maestro_setup_transaction_skill(
            spec.library,
            spec.cell,
            scope_token=scope_token,
            body=body,
            canonical_test=canonical_test,
        ),
        label=f"native Maestro setup {spec.library}/{spec.cell}",
    )
    result = require_bridge_confirmation(
        operation,
        f"create native Maestro setup {spec.library}/{spec.cell}",
        lambda: dispatch_oa_mutation(
            operation,
            client,
            library=spec.library,
            cell=spec.cell,
            phase="create native Maestro view SKILL dispatch",
            callback=lambda: client.execute_skill(source, timeout=timeout),
        ),
    )
    if result.errors:
        raise RuntimeError(
            f"failed to create native Maestro {spec.library}/{spec.cell}: "
            f"{result.errors[0]}"
        )
