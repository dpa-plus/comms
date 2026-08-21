#!/usr/bin/env python3
"""Copy the comms implementation out of the graphify fork into this package.

WHY THIS EXISTS. The Python build is developed inside a graphify checkout,
where it lives at ``graphify/comms/`` and is reached as ``graphify comms``.
Here it is a standalone package, ``comms_graph``, reached as ``comms-graph``.
Every sync therefore has to rewrite imports and command strings, and doing that
by hand went wrong four separate times in one evening:

* a sync taken before a redesign shipped the *previous* board and nobody
  noticed until the directory was read back;
* the rewrite covered ``*.py`` and not ``*.md``, so the briefing kept teaching
  a command that does not exist here — caught only by a test that greps it;
* two test fixes that only make sense in this layout were overwritten by the
  upstream copies, twice;
* a name that had been sanitised here came straight back in, because the
  source it is synced from had not been sanitised too.

None of those were interesting mistakes. They were the same mistake, which is
what a script is for.

    python3 sync-from-graphify.py [--check]

``--check`` reports what would change and exits 1 if anything would, which is
what CI should run: a drift between the two trees is a fact worth failing on.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
SOURCE = HERE.parent.parent / "comms-graph" / "graphify" / "comms"
SOURCE_TESTS = HERE.parent.parent / "comms-graph" / "tests"

#: Superseded second implementations. They are deleted here on purpose and a
#: test asserts they stay deleted, so the sync must never carry them back.
SKIP = {"commands.py", "tools.py"}

#: Applied to every copied file, source and markdown alike. Order matters: the
#: longer package path has to go before the bare command string.
REWRITES = [
    ("from graphify.comms import", "from comms_graph import"),
    ("from graphify.comms.", "from comms_graph."),
    ("import graphify.comms.", "import comms_graph."),
    ('"graphify.comms', '"comms_graph'),
    ("'graphify.comms", "'comms_graph"),
    ("Usage: graphify comms", "Usage: comms-graph"),
    ("`graphify comms`", "`comms-graph`"),
    ("graphify comms ", "comms-graph "),
]

#: Fixes that belong to THIS layout and must survive an overwrite. Each is
#: (file, old, new) and each is idempotent — applied after the copy, every time.
LOCAL_FIXES: list[tuple[str, str, str]] = [
    (
        "tests/test_concurrency.py",
        "REPO_ROOT = Path(__file__).resolve().parents[2]",
        "REPO_ROOT = Path(__file__).resolve().parents[1]",
    ),
    (
        "tests/test_end_to_end.py",
        "PROJECT_ROOT = Path(__file__).resolve().parents[2]",
        "PROJECT_ROOT = Path(__file__).resolve().parents[1]",
    ),
    (
        "tests/test_end_to_end.py",
        "    root = Path(__file__).resolve().parents[2]\n",
        "    root = Path(__file__).resolve().parents[1]\n",
    ),
    (
        "tests/test_end_to_end.py",
        'root / "graphify" / "comms" / ',
        'root / "comms_graph" / ',
    ),
    (
        "tests/test_end_to_end.py",
        'pkg = root / "graphify" / "comms"',
        'pkg = root / "comms_graph"',
    ),
    (
        "tests/test_end_to_end.py",
        'f"graphify/comms/ holds {undeclared} which',
        'f"comms_graph/ holds {undeclared} which',
    ),
    # The wheel test asserts an explicit package list; this project declares
    # them with a find directive, which is equally valid and equally shipping.
    (
        "tests/test_end_to_end.py",
        '''    assert "comms_graph" in setuptools_cfg["packages"], (
        "comms_graph is not in [tool.setuptools] packages, so the whole "
        "subpackage would be missing from the wheel"
    )''',
        '''    declared = setuptools_cfg.get("packages")
    covered = (any(fnmatch.fnmatch("comms_graph", g)
                   for g in declared.get("find", {}).get("include", []))
               if isinstance(declared, dict) else "comms_graph" in (declared or []))
    assert covered, f"comms_graph is not covered by packages (declared: {declared})"''',
    ),
    # Upstream keeps the superseded modules with a banner; here they are gone.
    (
        "tests/test_end_to_end.py",
        '''    for name in ("commands", "tools"):
        text = (root / "comms_graph" / f"{name}.py").read_text(encoding="utf-8")
        assert text.lstrip('"').startswith("SUPERSEDED"), \\
            f"{name}.py lost its superseded banner; a reader will mistake it for live code"''',
        '''    for name in ("commands", "tools"):
        assert not (root / "comms_graph" / f"{name}.py").exists(), (
            f"{name}.py is back. It is a second implementation that nothing dispatches.")''',
    ),
]

#: Refused outright rather than rewritten. A name that has been sanitised out
#: of this repo must not walk back in on the next sync — that happened once
#: already, and the scan caught it about a minute before it would not have.
#: Built at import time so it names THIS machine's home rather than any path
#: that looks like one. The first version matched ``/Users/[a-z]+/`` and refused
#: the sync over ``/Users/someone/`` — a deliberate fixture in a test about
#: ephemeral stores. A guard that cries wolf gets switched off, so it has to be
#: exact about what it is actually protecting against.
_HOME = re.escape(str(pathlib.Path.home()))

#: Any `scheme:TICKET-123` ref that is not the documented placeholder. Written
#: generically on purpose: naming the scheme this guard exists to catch would
#: put that name back in a public file, which is the exact thing it is for.
_REAL_TRACKER_REF = re.compile(r"\b(?!tracker:PROJ-1234\b)[a-z][a-z0-9]{2,}:[A-Z]{2,}-\d+")

FORBIDDEN = [
    (_REAL_TRACKER_REF, "a real tracker id — use the documented placeholder"),
    (re.compile(_HOME + r"/"), "this machine's real home directory"),
]


def render(text: str) -> str:
    for old, new in REWRITES:
        text = text.replace(old, new)
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="report drift and exit 1 rather than writing")
    args = ap.parse_args()

    if not SOURCE.is_dir():
        print(f"error: no source tree at {SOURCE}", file=sys.stderr)
        print("  This script syncs FROM the graphify fork; without it there is "
              "nothing to copy.", file=sys.stderr)
        return 2

    planned: list[tuple[pathlib.Path, str]] = []

    for src in sorted(SOURCE.iterdir()):
        if not src.is_file() or src.name in SKIP:
            continue
        if src.suffix not in (".py", ".md"):
            continue
        planned.append((HERE / "comms_graph" / src.name, render(src.read_text())))

    for sub in ("comms", "comms_view"):
        d = SOURCE_TESTS / sub
        if not d.is_dir():
            continue
        for src in sorted(d.glob("*.py")):
            planned.append((HERE / "tests" / src.name, render(src.read_text())))

    # local fixes, applied to the rendered text before anything is written
    by_path = {p: t for p, t in planned}
    for rel, old, new in LOCAL_FIXES:
        target = HERE / rel
        if target in by_path and old in by_path[target]:
            by_path[target] = by_path[target].replace(old, new)

    problems = []
    for path, text in by_path.items():
        for pattern, what in FORBIDDEN:
            hit = pattern.search(text)
            if hit:
                problems.append(f"{path.name}: {what} — {hit.group(0)!r}")
    if problems:
        print("refusing to sync; the source tree still contains:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print("  Fix it in the SOURCE tree, or it comes back on the next sync.",
              file=sys.stderr)
        return 2

    changed = [p for p, t in by_path.items()
               if not p.exists() or p.read_text() != t]
    if args.check:
        if changed:
            print(f"{len(changed)} file(s) differ from the source tree:")
            for p in sorted(changed):
                print(f"  {p.relative_to(HERE)}")
            return 1
        print("in sync")
        return 0

    for path, text in by_path.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    print(f"synced {len(by_path)} file(s); {len(changed)} changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
