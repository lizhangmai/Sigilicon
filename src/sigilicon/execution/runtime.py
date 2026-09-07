"""Bind an owner runner's environment protocol to project runtime resources."""

from __future__ import annotations

from dataclasses import dataclass

from sigilicon.execution._plan import PreflightCheck, RuntimeEnvironment
from sigilicon.execution._values import ContractError
from sigilicon.execution._resources import Resources


@dataclass(frozen=True)
class BoundEnvironment:
    """Resolved child environment and the path kinds that must remain held."""

    values: dict[str, str]
    tools: tuple[str, ...]
    files: tuple[str, ...]
    directories: tuple[str, ...]


def preflight_environment(
    runtime: RuntimeEnvironment,
    resources: Resources,
) -> tuple[PreflightCheck, ...]:
    """Check every resource identity selected by an owner runtime profile."""

    checks: list[PreflightCheck] = []
    labels = {
        "tools": "tool",
        "files": "file",
        "directories": "directory",
        "values": "value",
    }
    require = {
        "tools": resources.require_tool,
        "files": resources.require_file,
        "directories": resources.require_directory,
        "values": resources.require_value,
    }
    for kind in ("tools", "files", "directories", "values"):
        for identity in getattr(runtime, kind).values():
            try:
                require[kind](identity)
            except ContractError:
                ready = False
            else:
                ready = True
            checks.append(
                PreflightCheck(
                    "runtime-resource",
                    identity,
                    "ready" if ready else "blocked",
                    f"configured {labels[kind]}"
                    if ready
                    else f"missing configured {labels[kind]}",
                )
            )
    return tuple(checks)


def bind_environment(
    runtime: RuntimeEnvironment,
    resources: Resources,
) -> BoundEnvironment:
    """Resolve one explicit owner protocol without adding ambient bindings."""

    environment = dict(resources.environment)
    require = {
        "tools": resources.require_tool,
        "files": resources.require_file,
        "directories": resources.require_directory,
        "values": resources.require_value,
    }
    for kind in ("tools", "files", "directories", "values"):
        for name, identity in getattr(runtime, kind).items():
            environment[name] = str(require[kind](identity))
    return BoundEnvironment(
        environment,
        tuple(runtime.tools),
        tuple(runtime.files),
        tuple(runtime.directories),
    )


__all__ = ["BoundEnvironment", "bind_environment", "preflight_environment"]
