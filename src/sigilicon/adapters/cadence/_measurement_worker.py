"""Isolated owner program for direct Spectre deck generation and evaluation."""

from contextlib import redirect_stdout
import json
from pathlib import Path
import sys
from types import ModuleType


def main() -> int:
    request = json.loads(Path(sys.argv[1]).read_text())
    module = ModuleType("_sigilicon_measurement")
    module.__file__ = request["source_name"]
    sys.modules[module.__name__] = module
    with redirect_stdout(sys.stderr):
        exec(compile(request["source_text"], module.__file__, "exec"), module.__dict__)
        result = module.measurement(request["request"])
    if not isinstance(result, dict):
        raise TypeError("measurement program must return a JSON object")
    sys.stdout.write(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
