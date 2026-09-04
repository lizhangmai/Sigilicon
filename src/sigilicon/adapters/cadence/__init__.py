"""Trusted Cadence adapter registry."""

from sigilicon.adapters.cadence.ams_adapter import XceliumAmsAdapter
from sigilicon.adapters.cadence.layout_adapter import (
    LayoutAdapter,
    LayoutVerificationAdapter,
)
from sigilicon.adapters.cadence.oa_adapter import (
    NativeOaAdapter,
    OaAttestAdapter,
    OaCheckAdapter,
    OaRebuildAdapter,
)
from sigilicon.adapters.cadence.rtl_adapter import SpectreAdapter, XceliumAdapter


def cadence_adapters() -> tuple[
    SpectreAdapter,
    XceliumAdapter,
    XceliumAmsAdapter,
    NativeOaAdapter,
    OaCheckAdapter,
    OaRebuildAdapter,
    OaAttestAdapter,
    LayoutAdapter,
    LayoutVerificationAdapter,
]:
    return (
        SpectreAdapter(),
        XceliumAdapter(),
        XceliumAmsAdapter(),
        NativeOaAdapter(),
        OaCheckAdapter(),
        OaRebuildAdapter(),
        OaAttestAdapter(),
        LayoutAdapter(),
        LayoutVerificationAdapter(),
    )


__all__ = [
    "LayoutAdapter",
    "LayoutVerificationAdapter",
    "NativeOaAdapter",
    "OaAttestAdapter",
    "OaCheckAdapter",
    "OaRebuildAdapter",
    "SpectreAdapter",
    "XceliumAdapter",
    "XceliumAmsAdapter",
    "cadence_adapters",
]
