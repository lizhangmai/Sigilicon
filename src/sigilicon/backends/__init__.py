"""Trusted capability backends for the typed execution kernel."""

from sigilicon.backends.cadence import cadence_backends
from sigilicon.backends.synopsys import synopsys_backends


def trusted_backends():
    """Return the explicit built-in backend set used by operator CLIs."""

    return (*synopsys_backends(), *cadence_backends())


__all__ = ["cadence_backends", "synopsys_backends", "trusted_backends"]
