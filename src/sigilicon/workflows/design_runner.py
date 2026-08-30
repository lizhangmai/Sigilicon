"""Execute one exact design runner with a virtualized immutable spec source."""

from __future__ import annotations

from contextlib import contextmanager
import builtins
import io
import os
from pathlib import Path
import sys
from typing import Any, Iterator


def _absolute_lexical(value: object) -> str | None:
    if isinstance(value, int):
        return None
    try:
        return os.path.abspath(os.fspath(value))
    except TypeError:
        return None


@contextmanager
def _virtual_source(path: str, payload: bytes) -> Iterator[None]:
    """Serve exact bytes for one logical read path without changing its base."""

    logical = os.path.abspath(path)
    original_builtin_open = builtins.open
    original_io_open = io.open
    original_resolve = Path.resolve

    def virtual_open(
        file: Any,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
        closefd: bool = True,
        opener: Any = None,
    ) -> Any:
        if _absolute_lexical(file) != logical:
            return original_io_open(
                file,
                mode,
                buffering,
                encoding,
                errors,
                newline,
                closefd,
                opener,
            )
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            raise OSError("bound design spec is read-only")
        source = io.BytesIO(payload)
        if "b" in mode:
            return source
        return io.TextIOWrapper(
            source,
            encoding=encoding or "utf-8",
            errors=errors,
            newline=newline,
        )

    def virtual_builtin_open(*args: Any, **kwargs: Any) -> Any:
        return virtual_open(*args, **kwargs)

    def virtual_resolve(self: Path, strict: bool = False) -> Path:
        if _absolute_lexical(self) == logical:
            return Path(logical)
        return original_resolve(self, strict=strict)

    builtins.open = virtual_builtin_open
    io.open = virtual_open
    Path.resolve = virtual_resolve
    try:
        yield
    finally:
        Path.resolve = original_resolve
        io.open = original_io_open
        builtins.open = original_builtin_open


def main() -> None:
    """Run the descriptor-bound source encoded in this process argv."""

    if len(sys.argv) < 6:
        raise RuntimeError("bound design runner arguments are incomplete")
    runner_source = sys.argv.pop(1)
    runner_logical = os.path.abspath(sys.argv.pop(1))
    package_name = sys.argv.pop(1)
    spec_source = sys.argv.pop(1)
    spec_logical = sys.argv.pop(1)
    with original_open(runner_source, "rb") as stream:
        runner_payload = stream.read()
    if not isinstance(runner_payload, bytes):
        raise RuntimeError("bound design runner is not binary source")
    sys.argv[0] = runner_logical
    sys.path.insert(0, str(Path(runner_logical).parent))
    scope = {
        "__name__": "__main__",
        "__file__": runner_logical,
        "__package__": None if package_name == "-" else package_name,
        "__cached__": None,
    }
    if spec_source == "-" and spec_logical == "-":
        exec(compile(runner_payload, runner_logical, "exec"), scope, scope)
        return
    if spec_source == "-" or not os.path.isabs(spec_logical):
        raise RuntimeError("bound design spec arguments are invalid")
    with original_open(spec_source, "rb") as stream:
        spec_payload = stream.read()
    with _virtual_source(spec_logical, spec_payload):
        exec(compile(runner_payload, runner_logical, "exec"), scope, scope)


original_open = builtins.open


if __name__ == "__main__":
    main()
