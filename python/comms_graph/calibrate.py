"""NOT WIRED TO ANY COMMAND — a measurement harness, kept deliberately.

Nothing imports this module and no CLI verb reaches it. Unlike ``commands.py``
and ``tools.py`` it is not superseded: it is the harness that decided whether the
contact warning is honest enough to enable on a given repository, and its
results are what ``docs/COMMS.md`` cites. It earns its place as the record of
how those numbers were obtained, and is run by hand when that question is asked
again.

ORIGINAL MODULE DOCSTRING FOLLOWS.

The step that is allowed to refuse: is the contact warning worth turning on HERE?

WHY THIS EXISTS. ``contact.py`` is advisory and cheap, which makes it tempting to
enable everywhere. It is not honest everywhere. Two failure shapes, both silent:

  * **Empty board.** On a large, loosely-connected codebase a narrow claim has
    almost no neighbours, so nothing is ever flagged. The board looks calm
    because it knows nothing, and a calm board is read as "you are clear".
  * **Mush.** On a tangled codebase every claim touches every other claim, so
    every claim warns about everything. A warning that always fires carries no
    information; people stop reading it, and then it is worse than absent
    because it also cost them a glance each time.

Neither shape announces itself. Both look like a working tool. So before the
warning is switched on for a project, this measures the project.

WHAT IT MEASURES, AND AGAINST WHAT. The project's own git history is the ground
truth: two files that were really changed in the same commit really did belong
together in one person's head. So sample real commits, treat the files they
touched as if they had been claimed, and ask the real ``contact()`` whether it
would have connected them. Then ask the same question of pairs that were NOT
changed together. The number that matters is the ratio between those two rates —
how much more often a flag fires on a genuinely related pair than on an unrelated
one. One means the flag is a coin. That comparison is the whole point: a flag
rate with no baseline cannot support the claim "better than nothing", because
flagging everything also produces a high rate.

THE REFUSAL IS A FIRST-CLASS OUTCOME. ``verdict()`` has five independent ways to
say NO and one way to say YES, and the NO paths are reached by the ordinary flow,
not by an error handler. A calibration step that cannot fail is decoration. If
this prints DO NOT ENABLE on the repository it ships in, that is the tool
working.

WHAT THIS DOES NOT CLAIM. Co-change in git is a proxy for "related", not a
definition of it. Two files edited together in one commit may have been edited
together for administrative reasons (a version bump, a rename sweep), and two
genuinely coupled files may never appear in one commit because the work was split
across days. Both directions of that error are present in every number below, and
they are why the verdict demands a wide margin over chance rather than any margin
at all.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import contact as _contact_mod
from . import scope as _scope
from .contact import contact as _contact
from .resolve import Resolution, file_nodes
from .resolve import resolve as _resolve

#: contact.py is under active development and has already renamed this constant
#: once (MAX_PLACES counted resolved nodes; MAX_SCOPES counts scopes a person
#: named). Read it by whichever name is present rather than pinning to one, so a
#: rename upstream degrades the report's wording instead of breaking the only
#: command that can say "do not ship this".
_CAP = getattr(_contact_mod, "MAX_SCOPES", getattr(_contact_mod, "MAX_PLACES", 3))

#: The one phrase both cap messages share. Used to detect a refusal empirically
#: rather than by re-deriving the cap rule here — this module must measure what
#: contact() does, not a copy of it that can drift out of step.
_CAP_PHRASE = "usefully reason about"

# --------------------------------------------------------------------------
# Defaults. Every one of these is a judgement call, so every one is a flag.
# --------------------------------------------------------------------------

#: Commits to actually simulate. ~100 is enough for the ratio below to have a
#: usable confidence interval and small enough that the run stays under a minute.
DEFAULT_SAMPLE = 100

#: How far back to look for commits worth sampling. Sampling from a window is
#: better than taking the newest N: the newest N is one week of one person's
#: habits, which is not the project.
DEFAULT_WINDOW = 2000

#: A commit touching more files than this is a sweep — a rename, a reformat, a
#: license header — not one piece of work. Including them would manufacture
#: thousands of "co-changed" pairs that no human ever thought of together, and
#: they would flatter or ruin the result depending on the repo's habits.
DEFAULT_MAX_COMMIT_FILES = 12

#: Symbol claims sampled per file. A 400-symbol file would otherwise dominate the
#: run time and give one file's shape a vote proportional to its length.
DEFAULT_MAX_SYMBOLS = 40

# ---- verdict thresholds ----

#: The flag must fire this many times more often on a genuinely co-changed pair
#: than on an unrelated one — measured at the LOWER end of the confidence
#: interval, so a lucky sample cannot buy a yes. 2.0 is deliberately well above
#: 1.0 (chance): co-change is a noisy proxy for "related", and a margin that
#: barely clears chance would not survive the noise in the proxy itself.
MIN_LIFT = 2.0

#: If the flag catches less than this share of really-co-changed pairs, the board
#: is empty in practice. It may still be precise; a precise warning that almost
#: never appears is not worth the install.
MIN_RECALL = 0.10

#: If more than this share of claims are too tangled to answer, the tool spends
#: most of its life saying "narrow your claim" or warning about everything.
MAX_TANGLED = 0.30

#: A claim that reaches more than this share of the files under test warns about
#: so much that its warning is close to unconditional. Chosen, not derived — it
#: is the point at which one flag in four is noise even if every one is "true".
TANGLE_SHARE = 0.20

#: Below this many usable co-changed pairs the sample cannot support any verdict,
#: and the honest answer is "unknown", which is reported as a refusal.
MIN_PAIRS = 50

#: Permutation trials for the chance baseline. 2000 gives a p-value resolution of
#: 0.0005, an order of magnitude finer than the 0.01 the verdict tests against.
PERMUTATIONS = 2000

#: The flag must beat this. A permutation test shuffles the labels and asks how
#: often pure chance reproduces the observed ratio.
MAX_P_VALUE = 0.01


# --------------------------------------------------------------------------
# Buckets. Every pair lands in exactly one.
# --------------------------------------------------------------------------

FLAGGED = "flagged"  # the warning would have fired
TANGLED = "tangled"  # a claim so connected the warning says nothing
QUIET = "quiet"  # nothing at all


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------


class CalibrationError(RuntimeError):
    """Raised when the measurement cannot be made at all. Not a verdict."""


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if proc.returncode != 0:
        raise CalibrationError(
            f"git {' '.join(args)} failed in {root}: {proc.stderr.strip() or 'no output'}"
        )
    return proc.stdout


def read_commits(root: Path, window: int, max_files: int) -> tuple[list[tuple[str, list[str]]], dict]:
    """Real commits and the files each touched, newest first.

    Merges are excluded: a merge commit's file list is the union of two branches
    and reflects no single decision. Commits touching one file are kept in the
    co-change record (they cost nothing) but produce no pairs.
    """
    out = _git(
        root,
        "log",
        "--no-merges",
        f"-n{window}",
        "--pretty=format:\x01%H",
        "--name-only",
    )
    commits: list[tuple[str, list[str]]] = []
    for block in out.split("\x01"):
        block = block.strip("\n")
        if not block:
            continue
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        sha, files = lines[0].strip(), [ln.strip() for ln in lines[1:]]
        commits.append((sha, files))

    stats = {
        "scanned": len(commits),
        "dropped_too_big": sum(1 for _, f in commits if len(f) > max_files),
        "dropped_single_file": sum(1 for _, f in commits if len(f) < 2),
    }
    return commits, stats


# --------------------------------------------------------------------------
# Joining the graph to the repository
# --------------------------------------------------------------------------


def _norm(p: str) -> str:
    s = str(p or "").replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s.rstrip("/")


def graph_files(graph) -> set[str]:
    return {
        _norm(d.get("source_file") or "")
        for _, d in graph.nodes(data=True)
        if d.get("source_file")
    }


def align_paths(graph, tracked: set[str]) -> tuple[dict[str, str], dict]:
    """Map git paths to the paths the graph stored, and say so when they disagree.

    The graph records whatever path the scan walked. If the scan was rooted at a
    subdirectory, every graph path carries a prefix the git path does not, and a
    naive string compare makes EVERY file miss — which would show up here as a
    perfectly quiet board rather than as the plumbing error it is. So the overlap
    is measured and reported, and a suffix match is tried before giving up.
    """
    gfiles = graph_files(graph)
    direct = {p: p for p in tracked if p in gfiles}
    info = {"graph_files": len(gfiles), "tracked": len(tracked), "matched_direct": len(direct)}

    if len(direct) >= 0.2 * min(len(gfiles), len(tracked)) or not gfiles:
        info["match_mode"] = "direct"
        return direct, info

    # Suffix fallback: unique endswith match only. An ambiguous suffix is dropped
    # rather than guessed — a wrong join is worse than a missing one here.
    by_suffix: dict[str, list[str]] = {}
    for g in gfiles:
        by_suffix.setdefault(g.split("/")[-1], []).append(g)
    mapped: dict[str, str] = {}
    for t in tracked:
        cands = [g for g in by_suffix.get(t.split("/")[-1], []) if g.endswith("/" + t) or g == t]
        if len(cands) == 1:
            mapped[t] = cands[0]
    info["match_mode"] = "suffix"
    info["matched_suffix"] = len(mapped)
    return (mapped if len(mapped) > len(direct) else direct), info


# --------------------------------------------------------------------------
# Building claims and running the real contact() over them
# --------------------------------------------------------------------------


@dataclass
class ClaimSet:
    """Everything the simulation needs about one file."""

    git_path: str
    graph_path: str
    whole: Resolution
    #: Scope strings for single-symbol claims — the small, specific shape
    #: docs/COMMS.md says the warning needs to be honest.
    symbol_scopes: list[str] = field(default_factory=list)
    #: True when contact() itself refuses a whole-file claim on this file as too
    #: broad. Determined by asking contact(), not by re-deriving its cap here.
    whole_file_refused: bool = False


def build_claims(graph, root: Path, files: dict[str, str], max_symbols: int, rng: random.Random) -> dict[str, ClaimSet]:
    """One ClaimSet per file, skipping files the map cannot place at all."""
    claims: dict[str, ClaimSet] = {}
    for git_path, graph_path in sorted(files.items()):
        try:
            whole = _resolve(graph, _scope.parse(graph_path), root)
        except _scope.ScopeError:
            continue
        if whole.miss_reason or not whole.places:
            continue

        basename = graph_path.split("/")[-1]
        labels: list[str] = []
        for _nid, data, line in file_nodes(graph, graph_path):
            if line is None:
                continue
            label = str(data.get("label") or "")
            if not label or label == basename:
                continue
            if data.get("file_type") == "rationale" or len(label) > 60:
                continue
            if "#" in label or "\\" in label:
                # Escapable, but a claim nobody would type by hand is not a
                # claim worth measuring.
                continue
            labels.append(label)
        labels = sorted(set(labels))
        if len(labels) > max_symbols:
            labels = rng.sample(labels, max_symbols)

        scopes = []
        for label in labels:
            s = f"{graph_path}#{label}"
            try:
                parsed = _scope.parse(s)
            except _scope.ScopeError:
                continue
            res = _resolve(graph, parsed, root)
            if res.miss_reason or not res.places:
                continue
            scopes.append(s)

        claims[git_path] = ClaimSet(
            git_path=git_path,
            graph_path=graph_path,
            whole=whole,
            symbol_scopes=scopes,
        )

    # Ask contact() whether it will even accept a whole-file claim. Cheap, and it
    # keeps this module from encoding a cap rule of its own that could disagree
    # with the one actually shipping.
    probe_others = [(c.git_path, c.graph_path, c.whole) for c in list(claims.values())[:1]]
    for cs in claims.values():
        others = [o for o in probe_others if o[0] != cs.git_path]
        rep = _contact(graph, cs.whole, others)
        cs.whole_file_refused = bool(rep.note and _CAP_PHRASE in rep.note)
    return claims


def contact_map(
    graph, root: Path, claims: dict[str, ClaimSet], grain: str, progress=None
) -> dict[str, set[str]]:
    """For each file, every other file its claims come into contact with.

    This runs the real ``contact()``: ``others`` is every other file held whole,
    which is what a board full of other agents looks like at its broadest.

    ``grain="symbol"`` is the OPTIMISTIC case and the headline: it credits the
    tool with a flag whenever the agent would have been warned had it claimed
    exactly the right symbol, by taking the union over every symbol in the file.
    Real agents do not always pick the right symbol, so the true rate is lower.
    ``grain="file"`` is what people actually type — one whole-file claim.

    The same treatment is applied to co-changed and unrelated pairs alike, so the
    ratio between them stays fair whichever grain is used.
    """
    others_all = [(c.git_path, c.graph_path, c.whole) for c in claims.values()]
    reach: dict[str, set[str]] = {p: set() for p in claims}
    for i, (path, cs) in enumerate(sorted(claims.items())):
        if progress is not None and i % 25 == 0:
            progress(i, len(claims))
        others = [o for o in others_all if o[0] != path]
        hit = reach[path]
        if grain == "file":
            report = _contact(graph, cs.whole, others)
            for touch in report.touches:
                hit.add(touch.other_actor)
            continue
        for s in cs.symbol_scopes:
            mine = _resolve(graph, _scope.parse(s), root)
            report = _contact(graph, mine, others)
            for touch in report.touches:
                hit.add(touch.other_actor)
    if progress is not None:
        progress(len(claims), len(claims))
    return reach


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------


def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% interval for a proportion. Handles 0 and n hits, which exact CIs do not."""
    if n == 0:
        return (0.0, 1.0)
    p = hits / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def permutation_p(pos_flags: list[int], neg_flags: list[int], trials: int, rng: random.Random) -> tuple[float, float]:
    """How often chance alone reproduces the observed ratio.

    THE BASELINE THE WHOLE REPORT RESTS ON. The labels (co-changed / not) are
    shuffled across the same flags, which destroys any real association while
    keeping the flag rate and both sample sizes exactly as observed. A ratio the
    shuffle reaches often is not evidence of anything.
    """
    observed = _ratio(pos_flags, neg_flags)
    pool = pos_flags + neg_flags
    n_pos = len(pos_flags)
    at_least = 0
    null_max = 0.0
    for _ in range(trials):
        rng.shuffle(pool)
        r = _ratio(pool[:n_pos], pool[n_pos:])
        null_max = max(null_max, r)
        if r >= observed:
            at_least += 1
    # +1/+1: a permutation test can never honestly report p = 0.
    return (at_least + 1) / (trials + 1), null_max


def _ratio(pos: list[int], neg: list[int]) -> float:
    if not pos or not neg:
        return 0.0
    tpr = sum(pos) / len(pos)
    fpr = sum(neg) / len(neg)
    if fpr == 0:
        # No false flags in the sample. Rule of three: the true rate could still
        # be as high as 3/n, and pretending it is zero would report an infinite
        # advantage from a finite sample.
        fpr = 3.0 / len(neg)
    return tpr / fpr if fpr else 0.0


# --------------------------------------------------------------------------
# The measurement
# --------------------------------------------------------------------------


@dataclass
class Result:
    repo: str
    graph: str
    ok: bool = False
    reasons: list[str] = field(default_factory=list)
    numbers: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def measure(
    graph,
    root: Path,
    *,
    sample: int = DEFAULT_SAMPLE,
    window: int = DEFAULT_WINDOW,
    max_commit_files: int = DEFAULT_MAX_COMMIT_FILES,
    max_symbols: int = DEFAULT_MAX_SYMBOLS,
    tangle_share: float = TANGLE_SHARE,
    seed: int = 20260820,
    scramble: bool = False,
    verbose: bool = True,
) -> dict:
    rng = random.Random(seed)
    say = (lambda m: print(m, file=sys.stderr)) if verbose else (lambda m: None)

    if graph is None:
        raise CalibrationError("no map for this project — run `graphify update .` first")

    tracked = {_norm(p) for p in _git(root, "ls-files").splitlines() if p.strip()}
    if not tracked:
        raise CalibrationError(f"{root} has no tracked files")

    files, align = align_paths(graph, tracked)
    if not files:
        raise CalibrationError(
            "the map and the repository share no file paths — the graph was probably "
            "built from a different directory than the one being calibrated"
        )

    commits, cstats = read_commits(root, window, max_commit_files)
    if not commits:
        raise CalibrationError(f"{root} has no non-merge commits to learn from")

    # Ground truth over the WHOLE window, not just the sampled commits: a pair
    # used as a negative must be one that never co-changed, and the sampled 100
    # is far too small a window to establish "never".
    co_changed: set[tuple[str, str]] = set()
    for _sha, fs in commits:
        known = sorted({f for f in fs if f in files})
        if len(known) < 2 or len(fs) > max_commit_files:
            continue
        for i in range(len(known)):
            for j in range(i + 1, len(known)):
                co_changed.add((known[i], known[j]))

    usable = [
        (sha, sorted({f for f in fs if f in files}))
        for sha, fs in commits
        if 2 <= len(fs) <= max_commit_files
    ]
    usable = [(sha, fs) for sha, fs in usable if len(fs) >= 2]
    if not usable:
        raise CalibrationError(
            f"none of the last {cstats['scanned']} commits touched two or more files that "
            f"are both in the map — nothing to calibrate on"
        )
    picked = usable if len(usable) <= sample else rng.sample(usable, sample)

    universe_paths = sorted({f for _sha, fs in picked for f in fs})
    say(f"  universe: {len(universe_paths)} files from {len(picked)} sampled commits")

    # NEGATIVE CONTROL. Hand each file somebody else's slice of the map. The
    # graph is still real, the commits are still real, the tangle is still real —
    # only the correspondence between a file's history and its place in the map
    # is destroyed. A harness that still reports an advantage after this is
    # measuring its own plumbing, and every number it prints about a real project
    # is worthless. This is the one run where DO NOT ENABLE is the correct
    # answer by construction, which makes it the test that the refusal path is
    # reachable from data and not only from a strict threshold.
    universe_map = {p: files[p] for p in universe_paths}
    if scramble:
        shuffled = list(universe_map.values())
        rng.shuffle(shuffled)
        universe_map = dict(zip(universe_map.keys(), shuffled))

    claims = build_claims(graph, root, universe_map, max_symbols, rng)
    if len(claims) < 2:
        raise CalibrationError("fewer than two files could be placed on the map")

    refused_whole = sum(1 for c in claims.values() if c.whole_file_refused)

    say(f"  building contact map over {len(claims)} files (symbol grain) ...")
    reach = contact_map(
        graph, root, claims, "symbol",
        progress=(lambda i, n: say(f"    {i}/{n}")) if verbose else None,
    )
    say("  building contact map (file grain) ...")
    reach_file = contact_map(graph, root, claims, "file")

    # ---- positives ----
    pos_pairs: set[tuple[str, str]] = set()
    for _sha, fs in picked:
        fs = [f for f in fs if f in claims]
        for i in range(len(fs)):
            for j in range(i + 1, len(fs)):
                pos_pairs.add(tuple(sorted((fs[i], fs[j]))))  # type: ignore[arg-type]

    # ---- negatives: the empirical chance baseline ----
    # Drawn from the SAME files, so a flag cannot win by preferring busy files.
    # Rejecting any pair that co-changed anywhere in the window keeps the label
    # honest rather than merely unobserved in the sample.
    pool = sorted(claims)
    neg_pairs: set[tuple[str, str]] = set()
    attempts = 0
    want = len(pos_pairs)
    max_attempts = max(20000, want * 200)
    while len(neg_pairs) < want and attempts < max_attempts and len(pool) > 1:
        attempts += 1
        a, b = rng.sample(pool, 2)
        pair = tuple(sorted((a, b)))
        if pair in co_changed or pair in neg_pairs:
            continue
        neg_pairs.add(pair)  # type: ignore[arg-type]

    # Precision as it would actually be experienced. Two priors, because the
    # difference between them is itself the point:
    #
    #   * tested — among the busy files real commits touch. Sampling makes the
    #     positives and negatives 50/50, which they are not, so neither of these
    #     is the sampled rate.
    #   * repo — across every file pair in the project. This is the population a
    #     claim is actually drawn from, and it is far more lopsided. Quoting
    #     precision against the tested prior would flatter the tool by roughly the
    #     ratio between the two.
    tested_total = len(claims) * (len(claims) - 1) / 2
    tested_real = len({p for p in co_changed if p[0] in claims and p[1] in claims})
    prior_tested = (tested_real / tested_total) if tested_total else 0.0
    repo_total = len(files) * (len(files) - 1) / 2
    prior_repo = (len(co_changed) / repo_total) if repo_total else 0.0

    scored = {
        name: _score(r, pos_pairs, neg_pairs, len(claims), tangle_share, prior_repo, rng)
        for name, r in (("symbol", reach), ("file", reach_file))
    }
    for v in scored.values():
        v["signal"]["co_change_prior_tested"] = prior_tested
    head = scored["symbol"]
    denom = max(1, len(claims) - 1)
    tangled_files = {p for p, r in reach.items() if len(r) > tangle_share * denom}

    return {
        "repo": str(root),
        "scrambled": scramble,
        "commits": {
            "scanned": cstats["scanned"],
            "usable": len(usable),
            "sampled": len(picked),
            "dropped_too_big": cstats["dropped_too_big"],
            "dropped_single_file": cstats["dropped_single_file"],
            "max_commit_files": max_commit_files,
        },
        "files": {
            "in_graph": align["graph_files"],
            "tracked": align["tracked"],
            "joined": len(files),
            "match_mode": align["match_mode"],
            "under_test": len(claims),
            "symbol_claims": sum(len(c.symbol_scopes) for c in claims.values()),
            "whole_file_claims_refused": refused_whole,
            "tangled": len(tangled_files),
            "tangle_share_threshold": tangle_share,
        },
        "buckets": head["buckets"],
        "signal": head["signal"],
        "by_grain": {k: v["signal"] for k, v in scored.items()},
        "buckets_by_grain": {k: v["buckets"] for k, v in scored.items()},
        "settings": {"seed": seed, "sample": sample, "window": window, "max_symbols": max_symbols},
    }


def _score(
    reach: dict[str, set[str]],
    pos_pairs,
    neg_pairs,
    n_files: int,
    tangle_share: float,
    prior: float,
    rng: random.Random,
) -> dict:
    """Bucket every pair and turn the two flag rates into one comparable number."""
    denom = max(1, n_files - 1)
    tangled_files = {p for p, r in reach.items() if len(r) > tangle_share * denom}

    def bucket(a: str, b: str) -> str:
        if a in tangled_files or b in tangled_files:
            return TANGLED
        # Undirected on purpose, matching contact(): either agent being warned is
        # a warning delivered.
        if b in reach.get(a, ()) or a in reach.get(b, ()):
            return FLAGGED
        return QUIET

    pos_b = [bucket(*p) for p in sorted(pos_pairs)]
    neg_b = [bucket(*p) for p in sorted(neg_pairs)]

    def counts(bs: list[str]) -> dict:
        return {k: bs.count(k) for k in (FLAGGED, TANGLED, QUIET)}

    pos_flags = [1 if b == FLAGGED else 0 for b in pos_b if b != TANGLED]
    neg_flags = [1 if b == FLAGGED else 0 for b in neg_b if b != TANGLED]

    tpr = (sum(pos_flags) / len(pos_flags)) if pos_flags else 0.0
    fpr = (sum(neg_flags) / len(neg_flags)) if neg_flags else 0.0
    tpr_lo, tpr_hi = wilson(sum(pos_flags), len(pos_flags))
    fpr_lo, fpr_hi = wilson(sum(neg_flags), len(neg_flags))
    lift = _ratio(pos_flags, neg_flags)
    lift_lo = tpr_lo / fpr_hi if fpr_hi > 0 else 0.0
    p_value, null_max = permutation_p(list(pos_flags), list(neg_flags), PERMUTATIONS, rng)
    def _prec(rate_pos: float, rate_neg: float) -> float:
        d = prior * rate_pos + (1 - prior) * rate_neg
        return (prior * rate_pos) / d if d > 0 else 0.0

    return {
        "buckets": {
            "co_changed": counts(pos_b),
            "unrelated": counts(neg_b),
            "all": counts(pos_b + neg_b),
        },
        "signal": {
            "pairs_co_changed": len(pos_pairs),
            "pairs_unrelated": len(neg_pairs),
            "decided_co_changed": len(pos_flags),
            "decided_unrelated": len(neg_flags),
            "tangled_files": len(tangled_files),
            "flag_rate_co_changed": tpr,
            "flag_rate_co_changed_ci": [tpr_lo, tpr_hi],
            "flag_rate_unrelated": fpr,
            "flag_rate_unrelated_ci": [fpr_lo, fpr_hi],
            "lift": lift,
            "lift_lower_bound": lift_lo,
            "chance_lift": 1.0,
            "permutation_p": p_value,
            "permutation_best_null_lift": null_max,
            "co_change_prior": prior,
            "precision_in_the_wild": _prec(tpr, fpr),
            # The pessimistic end of the same interval. A zero false-flag count
            # in a finite sample does NOT mean the rate is zero, and reporting
            # only the point estimate would let a lucky run print "100% precise".
            "precision_worst_case": _prec(tpr_lo, fpr_hi),
        },
    }


# --------------------------------------------------------------------------
# The verdict — five ways to say no
# --------------------------------------------------------------------------


def verdict(
    m: dict,
    *,
    min_lift: float = MIN_LIFT,
    min_recall: float = MIN_RECALL,
    max_tangled: float = MAX_TANGLED,
    min_pairs: int = MIN_PAIRS,
    max_p: float = MAX_P_VALUE,
) -> tuple[bool, list[str]]:
    """Yes or no, with every failing test named. Order is worst-first."""
    s, f, b = m["signal"], m["files"], m["buckets"]
    reasons: list[str] = []

    if s["decided_co_changed"] < min_pairs:
        reasons.append(
            f"not enough evidence: {s['decided_co_changed']} usable co-changed pairs, "
            f"under the {min_pairs} needed for any verdict. This is 'unknown', not 'no'."
        )

    tangled_share = b["all"][TANGLED] / max(1, sum(b["all"].values()))
    if tangled_share > max_tangled:
        reasons.append(
            f"too tangled: {tangled_share:.0%} of pairs involve a claim that reaches more than "
            f"{f['tangle_share_threshold']:.0%} of the codebase under test, over the "
            f"{max_tangled:.0%} limit. A warning that fires on everything says nothing."
        )

    if s["permutation_p"] > max_p:
        reasons.append(
            f"not distinguishable from chance: shuffling the labels reproduces this advantage "
            f"{s['permutation_p']:.1%} of the time (limit {max_p:.0%})."
        )

    if s["lift_lower_bound"] < min_lift:
        reasons.append(
            f"advantage too small: a flag is {s['lift']:.2f}x more likely on a really-co-changed "
            f"pair than on an unrelated one, but the sample only supports "
            f"{s['lift_lower_bound']:.2f}x with confidence, under the {min_lift:.1f}x required "
            f"(chance is 1.00x)."
        )

    if s["flag_rate_co_changed"] < min_recall:
        reasons.append(
            f"empty board: the warning fires on only {s['flag_rate_co_changed']:.1%} of pairs that "
            f"really were changed together, under the {min_recall:.0%} floor. It would almost "
            f"always be silent, and silence reads as 'you are clear'."
        )

    return (not reasons, reasons)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def render(m: dict, ok: bool, reasons: list[str]) -> str:
    s, f, c, b = m["signal"], m["files"], m["commits"], m["buckets"]
    L: list[str] = []
    L.append("")
    L.append(f"CONTACT-WARNING CALIBRATION — {m['repo']}")
    L.append("=" * 72)
    if m.get("scrambled"):
        L.append("  ** NEGATIVE CONTROL RUN: file-to-map correspondence deliberately shuffled.")
        L.append("  ** The only correct verdict below is DO NOT ENABLE.")
    L.append("")
    L.append("WHAT WAS SIMULATED")
    L.append(
        f"  {c['sampled']} real commits sampled from the last {c['scanned']} (non-merge), "
        f"{c['usable']} of which were usable."
    )
    L.append(
        f"  Dropped: {c['dropped_too_big']} touching more than {c['max_commit_files']} files "
        f"(sweeps, not single pieces of work), {c['dropped_single_file']} touching only one."
    )
    L.append(
        f"  {f['under_test']} files under test, {f['symbol_claims']} symbol claims, "
        f"joined to the map by {f['match_mode']} path match "
        f"({f['joined']}/{f['tracked']} tracked files are in the map)."
    )
    L.append("")
    L.append("WHAT THE WARNING DID")
    L.append(f"  {'':22}{'co-changed':>12}{'unrelated':>12}")
    for key, label in ((FLAGGED, "flagged"), (TANGLED, "tangled"), (QUIET, "nothing at all")):
        L.append(
            f"  {label:22}{b['co_changed'][key]:>12}{b['unrelated'][key]:>12}"
        )
    L.append(
        f"  {'total pairs':22}{s['pairs_co_changed']:>12}{s['pairs_unrelated']:>12}"
    )
    L.append("")
    L.append(
        f"  'tangled' = one side's claim reaches more than "
        f"{f['tangle_share_threshold']:.0%} of the {f['under_test']} files under test, so its "
        f"warning is close to unconditional. {f['tangled']} file(s) are like that."
    )
    L.append(
        f"  Separately, contact() refuses a whole-file claim outright on "
        f"{f['whole_file_claims_refused']}/{f['under_test']} of these files (its cap is {_CAP})."
    )
    L.append("")
    L.append("THE NUMBER THAT MATTERS")
    L.append(
        f"  fires on {s['flag_rate_co_changed']:.1%} of pairs really changed together "
        f"(95% CI {s['flag_rate_co_changed_ci'][0]:.1%}–{s['flag_rate_co_changed_ci'][1]:.1%}, "
        f"n={s['decided_co_changed']})"
    )
    L.append(
        f"  fires on {s['flag_rate_unrelated']:.1%} of pairs never changed together "
        f"(95% CI {s['flag_rate_unrelated_ci'][0]:.1%}–{s['flag_rate_unrelated_ci'][1]:.1%}, "
        f"n={s['decided_unrelated']})"
    )
    L.append("")
    L.append(
        f"  ADVANTAGE OVER CHANCE: {s['lift']:.2f}x  "
        f"(supported at {s['lift_lower_bound']:.2f}x; chance is 1.00x)"
    )
    L.append(
        f"  Random baseline: shuffling the labels reaches this advantage in "
        f"{s['permutation_p']:.2%} of {PERMUTATIONS} trials "
        f"(best a shuffle managed: {s['permutation_best_null_lift']:.2f}x)."
    )
    L.append("")
    L.append(
        f"  In real use a flag would be right about {s['precision_in_the_wild']:.0%} of the time "
        f"(worst case in this sample: {s['precision_worst_case']:.0%}), because only "
        f"{s['co_change_prior']:.2%} of ALL file pairs in this repo ever change together — "
        f"{s['co_change_prior_tested']:.1%} among the busy files tested here. That is the honest "
        f"precision: a prompt to look, never a verdict."
    )
    L.append("")
    L.append("  Everything above is the OPTIMISTIC case: the agent claimed exactly the symbol")
    L.append("  that mattered. Claimed as a whole file instead — what people usually type —")
    fg = m["by_grain"]["file"]
    L.append(
        f"  the same repository gives {fg['flag_rate_co_changed']:.1%} vs "
        f"{fg['flag_rate_unrelated']:.1%}, an advantage of {fg['lift']:.2f}x "
        f"(supported at {fg['lift_lower_bound']:.2f}x), with "
        f"{m['buckets_by_grain']['file']['all'][TANGLED]} pairs lost to tangle."
    )
    L.append("")
    L.append("VERDICT")
    if ok:
        L.append("  ENABLE the contact warning on this project.")
        L.append("  It beats chance by a clear margin, it is not drowning in tangle, and it")
        L.append("  catches enough to be worth reading. It is still advisory: see the")
        L.append("  precision line above before treating any flag as fact.")
    else:
        # "We could not tell" and "we measured it and it is not worth it" are
        # both refusals, but they call for different next steps: one wants more
        # history, the other wants the feature left off. Saying "no" to the first
        # would be as dishonest as saying "yes".
        unknown_only = len(reasons) == 1 and reasons[0].startswith("not enough evidence")
        L.append(
            "  CANNOT SAY YET — do not enable on this evidence."
            if unknown_only
            else "  DO NOT ENABLE the contact warning on this project."
        )
        for r in reasons:
            L.append(f"    - {r}")
        L.append("")
        L.append("  This is the calibration working, not failing. A contact warning on a")
        L.append("  project it does not suit costs attention on every claim and returns")
        L.append("  nothing; an empty board in particular reads as 'you are clear'.")
    L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def load_graph(path: Path):
    from networkx.readwrite import json_graph

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        return json_graph.node_link_graph(data, edges="links")
    except TypeError:  # networkx < 3.4
        return json_graph.node_link_graph(data)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="graphify-comms-calibrate",
        description="Measure whether the contact warning is worth enabling on this repository.",
    )
    ap.add_argument("repo", nargs="?", default=".", help="repository root (default: .)")
    ap.add_argument("--graph", default=None, help="graph.json (default: <repo>/graphify-out/graph.json)")
    ap.add_argument("--sample", type=int, default=DEFAULT_SAMPLE)
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--max-commit-files", type=int, default=DEFAULT_MAX_COMMIT_FILES)
    ap.add_argument("--max-symbols", type=int, default=DEFAULT_MAX_SYMBOLS)
    ap.add_argument("--tangle-share", type=float, default=TANGLE_SHARE)
    ap.add_argument("--min-lift", type=float, default=MIN_LIFT)
    ap.add_argument("--min-recall", type=float, default=MIN_RECALL)
    ap.add_argument("--max-tangled", type=float, default=MAX_TANGLED)
    ap.add_argument("--min-pairs", type=int, default=MIN_PAIRS)
    ap.add_argument("--seed", type=int, default=20260820)
    ap.add_argument(
        "--scramble",
        action="store_true",
        help="negative control: shuffle which file owns which slice of the map. "
        "The verdict MUST come back DO NOT ENABLE; if it does not, this harness "
        "is measuring itself and none of its other numbers mean anything.",
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--quiet", action="store_true", help="no progress on stderr")
    a = ap.parse_args(argv)

    root = Path(a.repo).resolve()
    gpath = Path(a.graph) if a.graph else root / "graphify-out" / "graph.json"
    if not gpath.exists():
        print(
            f"error: no map at {gpath}. Run `graphify update {root}` first — calibration "
            f"needs the map it is calibrating.",
            file=sys.stderr,
        )
        return 2
    try:
        graph = load_graph(gpath)
        m = measure(
            graph, root,
            sample=a.sample, window=a.window, max_commit_files=a.max_commit_files,
            max_symbols=a.max_symbols, tangle_share=a.tangle_share, seed=a.seed,
            scramble=a.scramble, verbose=not a.quiet,
        )
    except CalibrationError as exc:
        # Cannot measure is NOT the same as measured-and-refused, and must never
        # be reported as either a yes or a no.
        print(f"cannot calibrate: {exc}", file=sys.stderr)
        return 3
    m["graph"] = str(gpath)

    ok, reasons = verdict(
        m, min_lift=a.min_lift, min_recall=a.min_recall,
        max_tangled=a.max_tangled, min_pairs=a.min_pairs,
    )
    if a.json:
        print(json.dumps({**m, "enable": ok, "reasons": reasons}, indent=2))
    else:
        print(render(m, ok, reasons))
    # Exit code carries the verdict so this can gate an install script.
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
