---
status: accepted
---

# Use one managed execution lifecycle across design styles

Every operation that executes an EDA tool and produces canonical engineering-run evidence will run as a typed Action through `ProjectFlow` and `FlowEngine`. The selected owner and Execution Profile provide the Adapter, while one Flow run exclusively owns capability preflight, workspace-operation binding, logs, artifacts, incidents, evidence, completion, and cleanup. Each invocation has exactly one canonical Sigilicon RunRecord and manifest; backend-native metadata, summaries, and result databases remain ordinary artifacts of that run rather than a second Sigilicon lifecycle. ASIC, analog, mixed-signal, physical-design, and native-OA payloads retain their domain schemas; only their execution and evidence envelope is shared.

OA source/workspace administration remains a separate Module because plan, check, rebuild, and attestation govern a mutable external design database rather than an engineering run. Their reports are workspace/source administration evidence and cannot produce regression, qualification, or signoff conclusions. Native simulation and verification consume the resolved OA workspace through Flow. During migration, direct CLIs may wrap the same backend core with a standalone recorder, but a Flow Adapter must never invoke a workflow that creates another Sigilicon RunRecord or manifest. Compatibility execution paths are deleted after cataloged targets replace them; an owner-mandated human entrypoint may remain as a thin typed-Flow facade.

This rejects both a universal flattened EDA result schema and permanent parallel script/workflow lifecycles. Action declarations may be shared globally, but concrete Adapters are assembled only for the selected owner and profile so unrelated tool families do not become hidden runtime dependencies.
