"""Virtuoso process environment operations."""

from __future__ import annotations

import os
from typing import Any

from sigilicon.virtuoso.bridge import escape_skill_string

from sigilicon.virtuoso.capability import WorkspaceAuthority, require_workspace_capability
from sigilicon.virtuoso.confirmation import require_bridge_confirmation


def sanitize_virtuoso_license_env(
    client: Any,
    *,
    library: str,
    operation: Any,
    cds_lic_file: str | None = None,
) -> None:
    require_workspace_capability(
        operation,
        client,
        authority=WorkspaceAuthority.PROCESS_ENV_MUTATION,
        library=library,
        library_wide=True,
    )
    result = require_bridge_confirmation(
        operation,
        "clear Virtuoso LM_LICENSE_FILE",
        lambda: client.execute_skill(
            'setShellEnvVar("LM_LICENSE_FILE=")', timeout=20
        ),
    )
    if result.errors:
        raise RuntimeError(result.errors[0])
    cds_lic_file = cds_lic_file or os.environ.get("CDS_LIC_FILE")
    if cds_lic_file:
        setting = escape_skill_string(f"CDS_LIC_FILE={cds_lic_file}")
        result = require_bridge_confirmation(
            operation,
            "set Virtuoso CDS_LIC_FILE",
            lambda: client.execute_skill(
                f'setShellEnvVar("{setting}")', timeout=20
            ),
        )
        if result.errors:
            raise RuntimeError(result.errors[0])
