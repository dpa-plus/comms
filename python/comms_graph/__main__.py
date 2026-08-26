"""Entry point for ``comms-graph``.

Thin on purpose. Everything the command does lives in :mod:`comms_graph.cli`;
this exists so the package has a console script of its own rather than being
routed through a fork of graphify's CLI, which is where it used to live.

Exit status is the contract and is not uniform by accident:

* **0** recorded, or the edit may proceed.
* **1** the answer is no: somebody else holds this ground, or this is a
  self-review. A refusal, not a failure.
* **2** it did not work: bad usage, an unreadable store, a payload that could
  not be parsed. Claude Code treats 2 as BLOCK on a PreToolUse hook, and that
  is deliberate: an edit guard that cannot tell whether an edit is safe must
  not wave it through.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from comms_graph.cli import main as _main

    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return _main(args)
    except KeyboardInterrupt:
        # 130 is the shell convention for SIGINT. Not 2: a person pressing
        # ctrl-c at a board is not a hook that failed to decide.
        return 130
    except BrokenPipeError:
        # `comms-graph board | head` closes the pipe under us. That is the
        # reader's choice, not an error worth a stack trace.
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
