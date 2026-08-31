# Physical-design contract baseline

Sigilicon owns a small, reusable physical-design model and the orchestration
seams around external implementation tools. It does not contain a placement,
routing, optimization, or physical-closure engine.

The solver-independent interface is:

```python
result = adapter.run(job)
```

`PhysicalDesignJob` is the complete normalized input: design, technology facts,
constraints, and requested stages. Its identity is the digest of that complete
canonical value. `PhysicalDesignResult` records only tool-independent
observations: placements, routes, constraint outcomes, diagnostics, metrics,
terminal status, and provenance binding it to the exact Job.

The external Adapter owns tool invocation and translation. Synopsys FC, another
commercial implementation tool, or a project-owned implementation may satisfy
the same `physical-design.solve` Action. Sigilicon supplies no fallback solver
and never interprets a failed external tool as a successful physical result.

## Ownership and independence

The stable model remains independent of:

- process vendor, node, and PDK;
- standard-cell, custom, analog, array, or mixed-signal design style;
- OA, LEF/DEF, GDS, OASIS, and generator object models;
- project and IP ownership;
- implementation and signoff tool vendor.

Technology and design owners normalize source facts before the Job seam.
Project recipes explicitly select the Adapter; registration is availability,
not fallback, qualification, or owner opt-in.

## Materialization

`compile_materialization_plan(job, result, target)` lowers one exact Job/Result
pair into database-neutral write instructions. Only a succeeded, closed result
can produce an executable plan. Exhausted results may retain diagnostic geometry;
unsupported, failed, open, or independently invalid results are rejected.

Executable plans cross the separate `physical-design.materialize` Action. Its
Adapter emits a persistent layout and a `MaterializationReceipt` binding owner,
target, operation, backend completion, and complete Job/Result/Plan/layout
identities. Sigilicon registers no synthetic production materializer.

OA/XStream materialization is an OA workflow implementation. It consumes the
same stable plan and receipt contracts; it is not a physical-design solver and
does not add tool policy to the Job or Result.

## Verification

Physical verification remains independent of both solving and materialization.
`physical-verification.drc` and `physical-verification.lvs` require the exact
managed layout and receipt from one producer. LVS additionally requires the
canonical checked source. Evidence records backend execution, parsed reports,
findings, and exact source/layout identities.

Exit code zero without a parsed authoritative report is execution failure, not
clean evidence. DRC/LVS, PEX, post-layout analysis, and qualification never
recover conclusions from generic metric names.

## Capability rule

Every requested stage has an explicit observable outcome: succeeded, failed,
unsupported, or exhausted. Only typed, identity-bound evidence may cross to a
downstream Action. Offline and fake Adapters are contract-test fixtures and can
never establish product qualification or signoff.
