"""Verified step reuse into a new immutable run; never reopen an old workspace."""

from dataclasses import dataclass, replace
from pathlib import Path

from sigilicon.artifacts import read_json_object
from sigilicon.execution._result import RunResult, StepOutcome
from sigilicon.execution._values import ContractError
from sigilicon.execution.runs import RunStore


@dataclass(frozen=True)
class ResumeInput:
    run: RunResult
    outcomes: tuple[StepOutcome, ...]

    @property
    def record(self):
        return {
            "run_id": self.run.run_id,
            "plan_identity": self.run.plan_identity,
            "steps": [
                {"step": item.step, "artifacts": [
                    {"role": a.role, "kind": a.kind, "size": a.size, "sha256": a.sha256,
                     "path": a.path.relative_to(self.run.run_root).as_posix()}
                    for a in item.result.artifacts]}
                for item in self.outcomes
            ],
        }


def bind_resume(plan, artifact_root: Path, run_id: str):
    from sigilicon.execution._plan import _bind_execution_plan
    previous = RunStore(artifact_root).audit(
        owner=plan.owner, operation=plan.operation, variant=plan.variant, run_id=run_id)
    if not isinstance(previous, RunResult) or previous.status == "uncertain":
        raise ContractError("resume requires a closed run with known execution state")
    old = read_json_object(previous.run_root / "inputs" / "execution-plan.json", "resume plan")
    if old["software"] != plan.software.record:
        raise ContractError("resume execution software differs from the recorded run")
    old_steps = {s["id"]: s for s in old["steps"]}
    old_sources = {s["path"]: s for s in old["sources"]}
    sources = {s.path: s.record for s in plan.sources}
    old_resources = {r["identity"]: r for r in old["resources"]}
    resources = {r.identity: r.record for r in plan.resources}
    outcomes = {o.step: o for o in previous.outcomes}
    selected = []
    reused = set()
    for step in plan.steps:
        outcome = outcomes.get(step.id)
        if outcome is None or outcome.result.status != "succeeded":
            continue
        if any(dep not in reused for dep in step.needs):
            continue
        if (old_steps.get(step.id) != step.record
            or any(old_sources.get(s) != sources[s] for s in step.sources)
            or any(old_resources.get(r) != resources[r] for r in step.resources)):
            raise ContractError(f"resume inputs changed for successful step {step.id}")
        selected.append(outcome)
        reused.add(step.id)
    if not selected:
        raise ContractError("resume has no matching successful steps")
    return _bind_execution_plan(
        replace(plan, resume=ResumeInput(previous, tuple(selected))),
        composition_sources=plan._composition_sources, authority=plan._authority)
