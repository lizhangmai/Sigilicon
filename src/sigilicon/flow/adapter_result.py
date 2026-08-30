"""Complete a staged backend invocation behind the one-method Adapter seam."""

from __future__ import annotations

from collections.abc import Callable

from sigilicon.flow.model import (
    ActionContext,
    AdapterExecution,
    AdapterResult,
    AdapterResultError,
    CollectedActionResult,
    FlowExecutionError,
)


def complete_staged_run(
    context: ActionContext,
    *,
    validate_inputs: Callable[[ActionContext], tuple[str, ...]],
    prepare: Callable[[ActionContext], None],
    execute: Callable[[ActionContext], AdapterExecution],
    collect_result: Callable[
        [ActionContext, AdapterExecution], CollectedActionResult
    ],
) -> AdapterResult:
    """Preserve execution/evidence state while hiding backend-local stages."""

    diagnostics = validate_inputs(context)
    if diagnostics:
        raise FlowExecutionError("; ".join(diagnostics))
    prepare(context)
    execution = execute(context)
    if not isinstance(execution, AdapterExecution):
        raise FlowExecutionError("Adapter returned an invalid execution result")
    if execution.status != "succeeded":
        return AdapterResult(execution)
    try:
        collected = collect_result(context, execution)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        raise AdapterResultError(execution, exc) from exc
    if not isinstance(collected, CollectedActionResult):
        raise FlowExecutionError("Adapter returned an invalid collected result")
    return AdapterResult(execution, collected)


__all__ = ["complete_staged_run"]
