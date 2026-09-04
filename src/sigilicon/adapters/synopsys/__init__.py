"""Trusted Synopsys adapter registry."""

from sigilicon.adapters.synopsys.dc_adapter import DcAdapter
from sigilicon.adapters.synopsys.fc_adapter import FcAdapter
from sigilicon.adapters.synopsys.hspice_adapter import HspiceAdapter
from sigilicon.adapters.synopsys.structural_adapter import StructuralLinkAdapter
from sigilicon.adapters.synopsys.vcs_adapter import VcsAdapter


def synopsys_adapters() -> tuple[
    VcsAdapter,
    DcAdapter,
    FcAdapter,
    HspiceAdapter,
    StructuralLinkAdapter,
]:
    return (
        VcsAdapter(),
        DcAdapter(),
        FcAdapter(),
        HspiceAdapter(),
        StructuralLinkAdapter(),
    )


__all__ = [
    "DcAdapter",
    "FcAdapter",
    "HspiceAdapter",
    "StructuralLinkAdapter",
    "VcsAdapter",
    "synopsys_adapters",
]
