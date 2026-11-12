#!/usr/bin/env python3
"""Exit non-zero unless the git tag is `v` + comms_graph.__version__.

Reads the version from the source file instead of importing the package so it
runs on a bare CI runner before anything is installed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

INIT = Path(__file__).resolve().parent.parent / "python" / "comms_graph" / "__init__.py"


def source_version(init: Path = INIT) -> str:
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', init.read_text(encoding="utf-8"), re.M)
    if not m:
        raise SystemExit(f"error: no __version__ in {init}")
    return m.group(1)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: check_release_tag.py <tag>   e.g. v0.5.0", file=sys.stderr)
        return 2
    version = source_version()
    if argv[0] != f"v{version}":
        print(f"error: tag {argv[0]} does not match __version__ {version}; "
              f"bump comms_graph/__init__.py or tag v{version}", file=sys.stderr)
        return 1
    print(f"tag {argv[0]} matches __version__ {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
