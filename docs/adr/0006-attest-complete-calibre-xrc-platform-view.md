---
status: accepted
---

# Attest the complete Calibre xRC platform view at the PEX seam

Calibre xRC extraction depends on a foundry deck and its private sibling support
files, not on a standalone Quantus technology file.  The production PEX Adapter
therefore consumes the semantic `platform.pex` view with `pex-deck` and
`pex-support-root` members.  It rejects symlinks and special files, enforces file
and byte budgets, stages and content-attests the complete support tree, and then
runs the fixed PHDB, PDB/RC, and formatter sequence without a shell.  The base
Flow registry keeps PEX as an extension seam; the public workflow assembly owns
the Calibre xRC implementation and projects only receipt-bound typed evidence.

An extracted artifact is published only when every stage has its authoritative
completion marker, both extraction stages report zero xRC errors, the expected
regular sidecars exist, the top-level ports agree with canonical source, and the
flattened output contains parasitic elements.  Raw tool outputs remain evidence;
the published self-contained netlist replaces only its volatile `Created`
comment so identical electrical output has a stable content identity.  A missing
or malformed stage emits non-conclusive `execution_failed` or
`backend_unavailable` evidence and never emits a parasitic netlist.  This adds
bounded staging cost, but avoids private PDK paths in public contracts, dangling
sidecar dependencies, false success, and backend-specific logic in FlowEngine.
