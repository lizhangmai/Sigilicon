"""Managed materialization for pre-resolved canonical source revisions."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import shutil

from sigilicon.artifacts import atomic_write_json
from sigilicon.flow.model import (
    ActionContext,
    AdapterExecution,
    CollectedActionResult,
    FlowExecutionError,
    ProducedArtifact,
    SourceArtifactRevision,
)
from sigilicon.flow.source_revision import stale_source_members


def _sha256(path: Path) -> str:
    hasher = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class SourceAssetsAdapter:
    """Materialize a pinned Source Asset Revision below one Action directory."""

    version = "1"

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        revision = context.source_revision
        if revision is None:
            return ("source-assets Action omitted its resolved source revision",)
        stale = stale_source_members(revision)
        return (
            ()
            if not stale
            else (f"source revision changed after planning: {list(stale)}",)
        )

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        revision = context.source_revision
        if revision is None:
            raise FlowExecutionError("source-assets Action has no source revision")
        for artifact in revision.artifacts:
            output = self._output_path(context, artifact)
            if artifact.materialization == "file":
                member = artifact.members[0]
                shutil.copyfile(member.location, output)
                if _sha256(output) != member.digest:
                    raise FlowExecutionError(
                        f"source member changed while staging: {member.path}"
                    )
                continue
            atomic_write_json(
                output,
                {
                    "schema": 1,
                    "contract_kind": "source-set-manifest",
                    "kind": artifact.kind,
                    "qualifiers": dict(artifact.qualifiers),
                    "fingerprint": artifact.fingerprint,
                    "members": [
                        {"path": member.path, "digest": member.digest}
                        for member in artifact.members
                    ],
                },
            )
        return AdapterExecution.succeeded()

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        revision = context.source_revision
        if revision is None:
            raise FlowExecutionError("source-assets Action has no source revision")
        return CollectedActionResult(
            artifacts=tuple(
                ProducedArtifact(
                    artifact.role,
                    artifact.kind,
                    self._output_path(context, artifact),
                    qualifiers=artifact.qualifiers,
                )
                for artifact in revision.artifacts
            )
        )

    @staticmethod
    def _output_path(
        context: ActionContext,
        artifact: SourceArtifactRevision,
    ) -> Path:
        filename = (
            Path(artifact.members[0].path).name
            if artifact.materialization == "file"
            else "manifest.json"
        )
        return context.output_path(artifact.role, filename)


__all__ = ["SourceAssetsAdapter"]
