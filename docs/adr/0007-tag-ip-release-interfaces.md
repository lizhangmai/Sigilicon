---
status: accepted
---

# Tag IP release interfaces independently from release lifecycle

The common IP release contract owns source identity, maturity progression,
collateral inventory, immutable packaging, audit, and publication. It does not
assume that every exported interface is an OpenAccess macro.

Each export therefore contains one tagged interface payload:

- `oa-mixed-signal` binds an OA library/cell/view identity to distinct physical
  and transaction interfaces. Existing schema-1 contracts that contain
  `[exports.oa]` infer this tag so repository-owned contracts do not need a
  coordinated migration.
- `rtl` binds a public interface contract to a synthesizable top module and the
  collateral role carrying that module. This form cannot declare an OA identity
  or `source.oa_assembly`.

The selected interface kind owns development boundary validation, capability
availability, qualified-view semantics, and offline packaged-interface audit.
Only contracts containing an OA export extend their source closure through an
OA assembly. A pure RTL release must be plannable, buildable, and auditable
without loading OA manifests or design sources.

This is a source API change: callers inspect `IpExport.interface` and narrow the
tagged union instead of reading OA fields directly from every `IpExport`.
Release manifests remain schema 1; newly planned exports include
`interface.kind`, while the offline auditor continues to accept older OA
manifests that predate the tag.
