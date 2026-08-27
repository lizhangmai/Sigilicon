"""Pure flow domain logic with no Cadence or bridge dependencies."""

from sigilicon.domain.design import DesignSpec, load_design_spec
from sigilicon.domain.platform import PdkConfig, load_platform
from sigilicon.domain.physical_verification import (
    CheckedLayoutIdentity,
    CheckedSourceIdentity,
    DrcEvidence,
    DrcViolation,
    LvsEvidence,
    LvsMismatch,
    PhysicalVerificationStatus,
    VerificationCompletion,
)

__all__ = [
    "CheckedLayoutIdentity",
    "CheckedSourceIdentity",
    "DesignSpec",
    "DrcEvidence",
    "DrcViolation",
    "LvsEvidence",
    "LvsMismatch",
    "PdkConfig",
    "PhysicalVerificationStatus",
    "VerificationCompletion",
    "load_design_spec",
    "load_platform",
]
