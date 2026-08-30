"""Dependency-free fake Adapters for the M1 Flow vertical slice."""

from __future__ import annotations

import time

from sigilicon.flow.model import (
    ActionContext,
    ActionContract,
    AdapterSelection,
    AdapterResult,
    ArtifactPort,
    CollectedActionResult,
    FlowExecutionError,
    ExecutionProfile,
    ProducedArtifact,
)
from sigilicon.flow.registry import FlowRegistry


class _SourceAdapter:
    def run(self, context: ActionContext) -> AdapterResult:
        if not isinstance(context.action_config.get("text"), str):
            raise FlowExecutionError("text must be a string")
        context.output_path("source", "value.txt").write_text(
            str(context.action_config["text"]),
            encoding="utf-8",
        )
        return AdapterResult.succeeded(
            CollectedActionResult(
                artifacts=(
                    ProducedArtifact(
                        "source",
                        "text.plain",
                        context.output_path("source", "value.txt"),
                        qualifiers=context.action_config.get("qualifiers", {}),
                    ),
                )
            )
        )


class _TransformAdapter:
    def run(self, context: ActionContext) -> AdapterResult:
        source = context.input("input").path.read_text(encoding="utf-8")
        output = context.output_path("transformed", "value.txt")
        output.write_text(
            source.upper(),
            encoding="utf-8",
        )
        return AdapterResult.succeeded(
            CollectedActionResult(
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
        )


class _VerifyAdapter:
    def run(self, context: ActionContext) -> AdapterResult:
        if not isinstance(context.action_config.get("expected"), str):
            raise FlowExecutionError("expected must be a string")
        value = context.input("candidate").path.read_text(encoding="utf-8")
        expected = str(context.action_config["expected"])
        context.output_path("report", "verification.txt").write_text(
            f"actual={value}\nexpected={expected}\n",
            encoding="utf-8",
        )
        accepted = value == expected
        return AdapterResult.succeeded(
            CollectedActionResult(
                artifacts=(
                    ProducedArtifact(
                        "report",
                        "report.text",
                        context.output_path("report", "verification.txt"),
                    ),
                ),
                facts={"accepted": accepted},
            ),
            details={"accepted": accepted},
        )


class _WaitAdapter:
    """Bounded cancellation fixture; it has no engineering authority."""

    def run(self, context: ActionContext) -> AdapterResult:
        seconds = context.action_config.get("seconds")
        if type(seconds) is not int or not 1 <= seconds <= 30:
            raise FlowExecutionError("seconds must be an integer between 1 and 30")
        time.sleep(int(context.action_config["seconds"]))
        return AdapterResult.succeeded()


def register_fake_actions(registry: FlowRegistry) -> None:
    """Register the explicit test-only vertical slice into one registry."""

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


def fake_registry() -> FlowRegistry:
    """Build a fresh registry containing only the fake test Action seams."""

    registry = FlowRegistry()
    register_fake_actions(registry)
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


__all__ = ["fake_profile", "fake_registry", "register_fake_actions"]
