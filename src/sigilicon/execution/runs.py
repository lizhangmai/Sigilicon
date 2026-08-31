"""Immutable Flow run lookup and deletion independent of current recipes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Mapping

from sigilicon.artifacts import read_json_object
from sigilicon.canonical import canonical_digest
from sigilicon.identifiers import RUN_ID_PATTERN
from sigilicon.paths import ArtifactExecutionPaths, ArtifactLayout, ProjectContext


FLOW_RESULT_SCHEMA = 4
FLOW_RUN_MANIFEST_SCHEMA = 2
_IDENTIFIER = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
_OWNER = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*\Z")
_RUN_ID = re.compile(RUN_ID_PATTERN + r"\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class RunStoreError(ValueError):
    """A persisted run selection or record is invalid."""


def _identity(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise RunStoreError(f"invalid {label}: {value!r}")
    return value


def _portable_json(value: object) -> bool:
    if value is None or type(value) in {str, int, float, bool}:
        return True
    if isinstance(value, list):
        return all(_portable_json(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _portable_json(item) for key, item in value.items())
    return False


def _string_list(value: object, *, unique: bool = False) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) and _IDENTIFIER.fullmatch(item) for item in value)
        and (not unique or len(value) == len(set(value)))
    )


def _source_closure(value: object, *, scoped: bool) -> bool:
    fields = {"path", "sha256", "executable"} | ({"scope"} if scoped else set())
    return isinstance(value, list) and all(
        isinstance(item, dict)
        and set(item) == fields
        and isinstance(item["path"], str)
        and bool(item["path"])
        and isinstance(item["sha256"], str)
        and _SHA256.fullmatch(item["sha256"]) is not None
        and type(item["executable"]) is bool
        and (
            not scoped
            or (
                isinstance(item["scope"], str)
                and _IDENTIFIER.fullmatch(item["scope"]) is not None
            )
        )
        for item in value
    )


def _fact_schema(value: object, action: str) -> bool:
    if not isinstance(value, dict) or set(value) != {"action_kind", "fields"}:
        return False
    fields = value["fields"]
    return value["action_kind"] == action and isinstance(fields, list) and all(
        isinstance(field, dict)
        and set(field) == {"name", "kind", "required", "unit", "enum_values"}
        and isinstance(field["name"], str)
        and isinstance(field["kind"], str)
        and type(field["required"]) is bool
        and (field["unit"] is None or isinstance(field["unit"], str))
        and isinstance(field["enum_values"], list)
        and all(isinstance(item, str) for item in field["enum_values"])
        for field in fields
    )


def _action_plan(value: object) -> bool:
    return value is None or (
        isinstance(value, dict)
        and set(value) == {"kind", "record", "sources"}
        and isinstance(value["kind"], str)
        and _IDENTIFIER.fullmatch(value["kind"]) is not None
        and isinstance(value["record"], dict)
        and _portable_json(value["record"])
        and _source_closure(value["sources"], scoped=True)
    )


def _policy(value: object) -> bool:
    return isinstance(value, dict) and set(value) == {"policy_id", "checks"} and (
        isinstance(value["policy_id"], str)
        and _IDENTIFIER.fullmatch(value["policy_id"]) is not None
        and isinstance(value["checks"], list)
        and bool(value["checks"])
        and all(
            isinstance(check, dict)
            and set(check) == {"check_id", "fact", "operator", "expected"}
            and isinstance(check["check_id"], str)
            and isinstance(check["fact"], str)
            and check["operator"] in {"exists", "equals", "at_least", "at_most"}
            and _portable_json(check["expected"])
            for check in value["checks"]
        )
    )


def _node(value: object, topology: set[str]) -> bool:
    required = {
        "id",
        "action",
        "adapter",
        "action_config",
        "adapter_config",
        "required_capabilities",
        "execution_capability",
        "fact_schema",
        "platform_assets",
        "policy",
        "dependencies",
        "bindings",
        "source_assets",
        "action_plan",
    }
    if not isinstance(value, dict) or not required.issubset(value):
        return False
    action = value["action"]
    dependencies = value["dependencies"]
    bindings = value["bindings"]
    platform_assets = value["platform_assets"]
    if not all(_portable_json(item) for item in value.values()):
        return False
    return (
        isinstance(value["id"], str)
        and value["id"] in topology
        and isinstance(action, str)
        and _IDENTIFIER.fullmatch(action) is not None
        and isinstance(value["adapter"], str)
        and _IDENTIFIER.fullmatch(value["adapter"]) is not None
        and isinstance(value["action_config"], dict)
        and isinstance(value["adapter_config"], dict)
        and _string_list(value["required_capabilities"], unique=True)
        and isinstance(value["execution_capability"], str)
        and _fact_schema(value["fact_schema"], action)
        and isinstance(platform_assets, list)
        and all(
            isinstance(asset, dict)
            and set(asset) == {"role", "kind", "members", "identity"}
            and isinstance(asset["role"], str)
            and isinstance(asset["kind"], str)
            and _string_list(asset["members"], unique=True)
            and (asset["identity"] is None or isinstance(asset["identity"], (str, dict)))
            for asset in platform_assets
        )
        and (
            value["policy"] is None
            or (
                isinstance(value["policy"], str)
                and _IDENTIFIER.fullmatch(value["policy"]) is not None
            )
        )
        and _string_list(dependencies, unique=True)
        and set(dependencies).issubset(topology)
        and isinstance(bindings, list)
        and all(
            isinstance(binding, dict)
            and set(binding) == {"input", "producer", "output", "requires"}
            and all(isinstance(binding[field], str) for field in binding)
            and binding["producer"] in topology
            for binding in bindings
        )
        and (value["source_assets"] is None or isinstance(value["source_assets"], dict))
        and _action_plan(value["action_plan"])
        and (
            "evidence" not in value
            or (
                isinstance(value["evidence"], dict)
                and set(value["evidence"]) == {"role", "level", "scope"}
                and all(isinstance(item, str) for item in value["evidence"].values())
            )
        )
    )


def validate_resolved_plan(record: Mapping[str, Any]) -> None:
    """Validate the complete stable structure of a persisted resolved Plan."""

    required = {
        "schema",
        "contract_kind",
        "owner",
        "flow",
        "recipe",
        "target",
        "goals",
        "inputs",
        "topology",
        "nodes",
        "policies",
    }
    allowed = required | {"source_members", "implementation_sources"}
    if (
        not isinstance(record, Mapping)
        or set(record) - allowed
        or not required.issubset(record)
        or record.get("schema") != 1
        or record.get("contract_kind") != "resolved-flow-plan"
    ):
        raise RunStoreError("Resolved Flow Plan has an invalid contract envelope")
    _identity(record.get("owner"), _OWNER, "Resolved Flow Plan owner")
    for field in ("flow", "recipe", "target"):
        _identity(record.get(field), _IDENTIFIER, f"Resolved Flow Plan {field}")
    goals = record.get("goals")
    topology = record.get("topology")
    nodes = record.get("nodes")
    if (
        not _string_list(goals, unique=True)
        or not goals
        or not isinstance(record.get("inputs"), dict)
        or not isinstance(topology, list)
        or any(not isinstance(item, str) or _IDENTIFIER.fullmatch(item) is None for item in topology)
        or len(topology) != len(set(topology))
        or not isinstance(nodes, list)
        or any(not isinstance(item, dict) for item in nodes)
        or not isinstance(record.get("policies"), list)
        or any(not _policy(item) for item in record["policies"])
        or not _portable_json(record.get("inputs"))
    ):
        raise RunStoreError("Resolved Flow Plan has invalid graph fields")
    node_ids = [item.get("id") for item in nodes]
    if (
        node_ids != topology
        or any(not _node(item, set(topology)) for item in nodes)
    ):
        raise RunStoreError("Resolved Flow Plan topology does not match its nodes")
    if "source_members" in record and not _source_closure(
        record["source_members"], scoped=True
    ):
        raise RunStoreError("Resolved Flow Plan has an invalid source closure")
    if "implementation_sources" in record and not _source_closure(
        record["implementation_sources"], scoped=False
    ):
        raise RunStoreError("Resolved Flow Plan has an invalid implementation closure")


def inventory_relative(path: Path, run_root: Path) -> str:
    """Record a managed locator without dereferencing tool-created symlinks."""

    candidate = Path(path).absolute()
    root = run_root.absolute()
    if candidate == root or not candidate.is_relative_to(root):
        raise RunStoreError("managed path escaped the Flow Run")
    return candidate.relative_to(root).as_posix()


def validate_run_inventory(run_root: Path, manifest: Mapping[str, Any]) -> None:
    raw_paths = manifest.get("managed_paths")
    if not isinstance(raw_paths, list) or any(
        not isinstance(value, str) for value in raw_paths
    ):
        raise RunStoreError("Flow Run Manifest has invalid managed paths")
    if len(raw_paths) != len(set(raw_paths)):
        raise RunStoreError("Flow Run Manifest repeats a managed path")
    resolved_run = run_root.resolve()
    declared: set[str] = set()
    for relative_text in raw_paths:
        relative = Path(relative_text)
        if (
            not relative_text
            or relative.is_absolute()
            or "\\" in relative_text
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise RunStoreError(
                f"unsafe managed path in Flow Run Manifest: {relative_text!r}"
            )
        candidate = (run_root / relative).resolve(strict=False)
        if candidate == resolved_run or not candidate.is_relative_to(resolved_run):
            raise RunStoreError(
                f"unsafe managed path in Flow Run Manifest: {relative_text!r}"
            )
        declared.add(relative.as_posix())

    actual: set[str] = set()
    for path in run_root.rglob("*"):
        relative = path.relative_to(run_root).as_posix()
        if relative == "run_manifest.json":
            continue
        if path.is_symlink() and not path.resolve(strict=False).is_relative_to(
            resolved_run
        ):
            raise RunStoreError(
                f"escaping symlink in Flow Run inventory: {relative!r}"
            )
        actual.add(relative)
    missing = sorted(declared - actual)
    untracked = sorted(actual - declared)
    if missing or untracked:
        raise RunStoreError(
            "Flow Run manifest drift: "
            f"missing={missing}, untracked={untracked}"
        )


@dataclass(frozen=True)
class _SelectedRun:
    paths: ArtifactExecutionPaths
    owner: str
    target: str
    operation: str
    run_id: str


@dataclass(frozen=True)
class RunStore:
    """Read or remove an exact persisted run without compiling current source."""

    context: ProjectContext

    def __post_init__(self) -> None:
        if not isinstance(self.context, ProjectContext):
            raise TypeError("RunStore requires an explicit ProjectContext")
        root = self.context.artifact_root
        if root == Path(root.anchor):
            raise RunStoreError("artifact root cannot be a filesystem root")

    @property
    def artifact_root(self) -> Path:
        return self.context.artifact_root

    def _select(
        self,
        *,
        owner: str,
        target: str,
        operation: str,
        run_id: str,
    ) -> _SelectedRun:
        owner_name = _identity(owner, _OWNER, "Flow owner")
        target_name = _identity(target, _IDENTIFIER, "Flow target")
        operation_name = _identity(operation, _IDENTIFIER, "Flow operation")
        identity = _identity(run_id, _RUN_ID, "Flow Run identity")
        paths = ArtifactLayout(self.artifact_root).execution(
            owner=owner_name,
            target=operation_name,
            flow=target_name,
            variant="default",
            identity=identity,
            artifact_kind="flow",
            identity_kind="run_id",
        )
        if not paths.root.resolve(strict=False).is_relative_to(self.artifact_root):
            raise RunStoreError("Flow Run path escaped the artifact root")
        return _SelectedRun(paths, owner_name, target_name, operation_name, identity)

    def _records(
        self,
        selected: _SelectedRun,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        root = selected.paths.root
        if not root.is_dir() or root.is_symlink():
            raise RunStoreError(
                f"missing or unsafe persisted Flow Run: {selected.run_id}"
            )
        try:
            manifest = read_json_object(root / "run_manifest.json", "Flow Run Manifest")
            plan = read_json_object(
                selected.paths.role("inputs") / "resolved_plan.json",
                "Resolved Flow Plan",
            )
            result = read_json_object(
                selected.paths.role("outputs") / "flow_result.json",
                "Flow Result",
            )
        except (OSError, RuntimeError) as exc:
            raise RunStoreError(str(exc)) from exc
        validate_resolved_plan(plan)
        expected_identity = canonical_digest(plan)
        expected = {
            "owner": selected.owner,
            "flow": selected.target,
            "target": selected.operation,
            "run_id": selected.run_id,
            "plan_identity": expected_identity,
        }
        if (
            set(manifest)
            != {
                "schema",
                "contract_kind",
                *expected,
                "managed_paths",
            }
            or manifest["schema"] != FLOW_RUN_MANIFEST_SCHEMA
            or manifest["contract_kind"] != "flow-run-manifest"
            or any(manifest[field] != value for field, value in expected.items())
        ):
            raise RunStoreError("Flow Run Manifest identity or fields drift")
        if (
            result.get("schema") != FLOW_RESULT_SCHEMA
            or result.get("contract_kind") != "flow-result"
            or any(result.get(field) != value for field, value in expected.items())
        ):
            raise RunStoreError("Flow Result identity does not match selected run")
        validate_run_inventory(root, manifest)
        return manifest, plan, result

    def read(
        self,
        *,
        owner: str,
        target: str,
        operation: str,
        run_id: str,
    ) -> dict[str, Any]:
        selected = self._select(
            owner=owner,
            target=target,
            operation=operation,
            run_id=run_id,
        )
        _manifest, _plan, result = self._records(selected)
        return result

    def read_if_present(
        self,
        *,
        owner: str,
        target: str,
        operation: str,
        run_id: str,
    ) -> dict[str, Any] | None:
        """Read an exact persisted Flow Run, or return None before one exists."""

        selected = self._select(
            owner=owner,
            target=target,
            operation=operation,
            run_id=run_id,
        )
        if not selected.paths.root.exists():
            return None
        if not (selected.paths.root / "run_manifest.json").exists():
            return None
        _manifest, _plan, result = self._records(selected)
        return result

    def clean(
        self,
        *,
        owner: str,
        target: str,
        operation: str,
        run_id: str,
    ) -> None:
        selected = self._select(
            owner=owner,
            target=target,
            operation=operation,
            run_id=run_id,
        )
        manifest, _plan, _result = self._records(selected)
        root = selected.paths.root
        declared = {
            relative: root / Path(relative)
            for relative in manifest["managed_paths"]
        }
        for relative in sorted(
            declared,
            key=lambda value: len(Path(value).parts),
            reverse=True,
        ):
            path = declared[relative]
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        (root / "run_manifest.json").unlink()
        root.rmdir()


__all__ = [
    "FLOW_RESULT_SCHEMA",
    "FLOW_RUN_MANIFEST_SCHEMA",
    "RunStore",
    "RunStoreError",
    "inventory_relative",
    "validate_run_inventory",
    "validate_resolved_plan",
]
