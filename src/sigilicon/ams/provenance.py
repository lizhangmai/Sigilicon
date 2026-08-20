"""Stable fingerprint for generated AMS state."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Mapping

from sigilicon.ams.spec import AmsSpec
from sigilicon.domain.provenance import design_fingerprint, digest


def ams_fingerprint(spec: AmsSpec) -> str:
    return digest(
        {
            "design": design_fingerprint(spec.design),
            "testbench": spec.testbench,
            "simulation": asdict(spec.simulation),
            "vectors": [asdict(vector) for vector in spec.vectors],
            "model_file": str(spec.design.pdk.model_file),
            "model_file_sha256": hashlib.sha256(
                spec.design.pdk.model_file.read_bytes()
            ).hexdigest(),
            "model_section": spec.design.pdk.model_section,
        }
    )


def normalize_run_variables(
    variables: Mapping[str, str] | None,
) -> dict[str, str]:
    """Return a deterministic, JSON-safe representation of run overrides."""

    return dict(sorted((variables or {}).items()))


def ams_run_fingerprint(
    spec: AmsSpec,
    *,
    backend: str,
    variables: Mapping[str, str] | None = None,
) -> str:
    """Fingerprint one execution context without changing the setup identity."""

    return digest(
        {
            "setup_fingerprint": ams_fingerprint(spec),
            "backend": backend,
            "variables": normalize_run_variables(variables),
        }
    )
