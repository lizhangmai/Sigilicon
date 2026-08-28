"""Dependency-free fake Adapters for the M1 Flow vertical slice."""

from __future__ import annotations

import time

from sigilicon.flow.model import (
    ActionContext,
    ActionContract,
    AdapterSelection,
    AdapterExecution,
    ArtifactPort,
    CollectedActionResult,
    FlowExecutionError,
    ExecutionProfile,
    ProducedArtifact,
)
from sigilicon.flow.registry import FlowRegistry


class _SourceAdapter:
    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        return (
            ()
            if isinstance(context.action_config.get("text"), str)
            else ("text must be a string",)
        )

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        context.output_path("source", "value.txt").write_text(
            str(context.action_config["text"]),
            encoding="utf-8",
        )
        return AdapterExecution.succeeded()

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "source",
                    "text.plain",
                    context.output_path("source", "value.txt"),
                    qualifiers=context.action_config.get("qualifiers", {}),
                ),
            )
        )


class _TransformAdapter:
    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        return ()

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        source = context.input("input").path.read_text(encoding="utf-8")
        context.output_path("transformed", "value.txt").write_text(
            source.upper(),
            encoding="utf-8",
        )
        return AdapterExecution.succeeded()

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        output = context.output_path("transformed", "value.txt")
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "transformed",
                    "text.plain",
                    output,
                    qualifiers=context.input("input").qualifiers,
                ),
            ),
            facts={"length": len(output.read_text(encoding="utf-8"))},
        )


class _VerifyAdapter:
    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        return (
            ()
            if isinstance(context.action_config.get("expected"), str)
            else ("expected must be a string",)
        )

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        value = context.input("candidate").path.read_text(encoding="utf-8")
        expected = str(context.action_config["expected"])
        context.output_path("report", "verification.txt").write_text(
            f"actual={value}\nexpected={expected}\n",
            encoding="utf-8",
        )
        return AdapterExecution.succeeded(details={"accepted": value == expected})

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        accepted = execution.details.get("accepted")
        if not isinstance(accepted, bool):
            raise FlowExecutionError("fake verify execution omitted its result")
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "report",
                    "report.text",
                    context.output_path("report", "verification.txt"),
                ),
            ),
            facts={"accepted": accepted},
        )


class _WaitAdapter:
    """Bounded cancellation fixture; it has no engineering authority."""

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        seconds = context.action_config.get("seconds")
        return (
            ()
            if type(seconds) is int and 1 <= seconds <= 30
            else ("seconds must be an integer between 1 and 30",)
        )

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        time.sleep(int(context.action_config["seconds"]))
        return AdapterExecution.succeeded()

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        return CollectedActionResult()


def fake_registry() -> FlowRegistry:
    """Build a fresh registry containing only the M1 fake Action seams."""

    registry = FlowRegistry()
    registry.register_action(
        ActionContract(
            kind="fake.source",
            outputs=(ArtifactPort("source", "text.plain"),),
            adapters=("fake-source",),
        )
    )
    registry.register_action(
        ActionContract(
            kind="fake.transform",
            inputs=(ArtifactPort("input", "text.plain"),),
            outputs=(ArtifactPort("transformed", "text.plain"),),
            facts=("length",),
            adapters=("fake-transform",),
        )
    )
    registry.register_action(
        ActionContract(
            kind="fake.verify",
            inputs=(ArtifactPort("candidate", "text.plain"),),
            outputs=(ArtifactPort("report", "report.text"),),
            facts=("accepted",),
            adapters=("fake-verify",),
        )
    )
    registry.register_action(
        ActionContract(
            kind="fake.wait",
            adapters=("fake-wait",),
        )
    )
    registry.register_adapter("fake-source", _SourceAdapter())
    registry.register_adapter("fake-transform", _TransformAdapter())
    registry.register_adapter("fake-verify", _VerifyAdapter())
    registry.register_adapter("fake-wait", _WaitAdapter())
    return registry


def fake_profile(owner: str = "example") -> ExecutionProfile:
    """Return the explicit Adapter selections for the fake vertical slice."""

    return ExecutionProfile(
        owner=owner,
        profile_id="fake",
        selections=(
            AdapterSelection("fake.source", "fake-source"),
            AdapterSelection("fake.transform", "fake-transform"),
            AdapterSelection("fake.verify", "fake-verify"),
        ),
    )


__all__ = ["fake_profile", "fake_registry"]
