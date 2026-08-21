"""Coordination for parallel coding agents, living in the same map as the code.

Ported from comms (Go). The split is deliberate and load-bearing:

  * The MAP is derived. It is thrown away and rebuilt whenever the code changes.
  * The LOG is truth. It is append-only, never rewritten, and must never be lost.

So claims, tasks and findings are written to their own JSONL log and only
PROJECTED onto the map. Putting them in graph.json would lose them on the next
rebuild, and graph.json is rewritten whole-file, which is wrong for many writers.
"""
