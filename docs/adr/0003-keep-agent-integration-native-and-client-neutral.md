---
status: accepted
---

# Keep agent integration native, clean-room, and client-neutral

Sigilicon will expose its own public Modules through a standards-compliant native MCP server and focused workflow Skills without importing, wrapping, translating, or emulating PANDA code, prompts, handlers, JSON contracts, binaries, or environment conventions. MCP, CLI, and Python remain peer clients of the same Sigilicon Interface, so deleting the MCP integration removes protocol access but no domain behavior; this trades compatibility shortcuts for auditable ownership and prevents a second circuit-design implementation from forming in protocol handlers.
