"""Two agents, one real repository, one real map: does the product actually work?

Everything else in tests/comms/ tests a module against its own contract. This
file tests the PROMISE: two agents working the same checkout at the same time
find out about each other before they collide, and find out about each other's
neighbouring work before they start.

So nothing here is faked. A throwaway git repo is written to disk with source
files that genuinely import and call each other, the real ``graphify extract
--code-only`` builds the real map from them (offline, no API key), and the flow
runs through the real modules writing to the real per-repo log store under the
real lock. If any of those is wrong -- the extractor labels symbols in a shape
the resolver cannot match, the log store lands somewhere the next command does
not look, the reducer forgets a release -- this file is where it shows up, and
nowhere else does.

The scenario is the one an agent lives through:

    A claims a symbol
    B checks that file            -> B is told A is there, and what A is doing
    B claims a symbol elsewhere   -> allowed, but WARNED: B's code calls A's
    B tries A's exact symbol      -> refused while A holds it
    A releases
    B tries A's exact symbol      -> now allowed

Plus one control: a claim on an UNCONNECTED file must produce no warning. A
warning that fires for everything is the same as no warning at all, so the
control is not optional -- it is the half of the test that gives the other half
any meaning.

HOW THE FLOW IS DRIVEN. graphify has no ``comms`` subcommand yet; graphify/comms
is a library. ``_claim`` / ``_check`` / ``_release`` below are the three commands
a CLI would expose, written the only way the modules allow -- take the lock, read
the log, fold it, decide, append -- and deliberately kept to about ten lines each
so they stay obviously a thin shell over the real thing rather than a second
implementation the test could pass against on its own.

WHY ONE FIXTURE, MANY TESTS. The story is sequential: "B can NOW claim it" only
means something after "B could not a moment ago". So the whole run happens once
in a module-scoped fixture that records what each step returned, and each test
below asserts one beat of that transcript with its own reason. A failure then
names the beat that broke instead of a line number in a 60-line function.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from comms_graph import contact, log, resolve, scope as scope_mod, state
from comms_graph.lock import file_lock

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Anything that would make extract try to reach an LLM. Stripped so the run is
# provably offline: if --code-only ever stopped being enough, this test would
# fail rather than quietly spend somebody's key.
_KEY_VARS = (
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY", "MOONSHOT_API_KEY", "DEEPSEEK_API_KEY",
)

AGENT_A = "claude-a11c"
AGENT_B = "codex-b02f"
AGENT_C = "gemini-c33d"

A_SCOPE = "core/store.py#save_record"
B_SCOPE = "service/orders.py#place_order"
CONTROL_SCOPE = "service/billing.py#charge"


# ---------------------------------------------------------------------------
# The repository under test
# ---------------------------------------------------------------------------

# Three files, two of which are genuinely coupled: service/orders.py imports
# core.store and calls save_record. service/billing.py is the control -- same
# repo, same language, same shape, no connection to either claim. The coupling
# has to be real code, not a hand-built graph, because the thing being tested is
# whether the extractor and the resolver agree about what a symbol is.
_SOURCES = {
    "core/__init__.py": "",
    "core/store.py": '''"""Persistence for order records."""


def save_record(record):
    """Write a record to the store."""
    return {"ok": True, "record": record}


def load_record(record_id):
    """Read a record back out of the store."""
    return {"id": record_id}
''',
    "service/__init__.py": "",
    "service/orders.py": '''"""Order handling, built on top of the store."""

from core.store import save_record


def place_order(order):
    """Place an order by persisting it."""
    return save_record(order)
''',
    "service/billing.py": '''"""Billing. Deliberately connected to nothing else in this repo."""


def charge(amount):
    """Charge a card, in cents."""
    return amount * 100
''',
}


def _write_repo(root: Path) -> None:
    for rel, body in _SOURCES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed; this test builds a real git repo")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    subprocess.run([git, "init", "-q", "."], cwd=root, check=True, env=env)
    subprocess.run([git, "add", "-A"], cwd=root, check=True, env=env)
    subprocess.run(
        [git, "-c", "user.email=e2e@test", "-c", "user.name=e2e", "commit", "-qm", "init"],
        cwd=root, check=True, env=env,
    )


def _extract(root: Path, home: Path):
    """Run the real CLI, offline. Returns the CompletedProcess for diagnosis."""
    env = {k: v for k, v in os.environ.items() if k not in _KEY_VARS}
    env["HOME"] = str(home)
    env["GRAPHIFY_OUT"] = str(root / "graphify-out")
    # The project is on sys.path for the subprocess explicitly rather than by
    # relying on an editable install: a half-installed venv would otherwise make
    # this report "the product is broken" when the truth is "the venv is".
    env["PYTHONPATH"] = os.pathsep.join(
        [str(PROJECT_ROOT), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    return subprocess.run(
        [sys.executable, "-m", "graphify", "extract", ".", "--code-only"],
        cwd=root, capture_output=True, text=True, env=env,
    )


# ---------------------------------------------------------------------------
# The three commands, as a CLI would implement them
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    """What one command answered, in the terms a user reads off the screen."""

    allowed: bool
    #: Active claims held by somebody else that cover the scope asked about.
    conflicts: list = field(default_factory=list)
    #: The rendered contact advice, exactly as it would be printed.
    advice: str = ""
    contact_report: contact.ContactReport | None = None
    claim_id: str = ""


def _clock():
    """Distinct, ascending, timezone-aware timestamps. The reducer sorts by ts."""
    t = dt.datetime(2026, 5, 22, 14, 0, 0, tzinfo=dt.timezone.utc)
    n = 0
    while True:
        n += 1
        yield t + dt.timedelta(seconds=n)


def _load_state(root: Path) -> state.State:
    return state.fold(log.read(log.log_path(root)))


def _others(st: state.State, graph, root: Path, actor: str):
    """Everybody else's live claims, placed on the map."""
    return [
        (c.actor, str(c.scope), resolve.resolve(graph, c.scope, root))
        for c in st.claims.values()
        if c.actor != actor
    ]


def _check(root: Path, graph, actor: str, raw_scope: str) -> Outcome:
    """`comms check <scope>`: who is on this, and what is next to it?"""
    sc = scope_mod.parse(raw_scope)
    st = _load_state(root)
    conflicts = st.conflicts_for(sc, actor)
    report = contact.contact(graph, resolve.resolve(graph, sc, root), _others(st, graph, root, actor))
    return Outcome(
        allowed=not conflicts,
        conflicts=conflicts,
        advice=contact.render(report),
        contact_report=report,
    )


def _claim(root: Path, graph, actor: str, raw_scope: str, intent: str, clock) -> Outcome:
    """`comms claim <scope>`: take the ground, or be refused and say so in the log."""
    sc = scope_mod.parse(raw_scope)
    path = log.log_path(root)
    with file_lock(log.store_dir(root) / "lock"):
        st = state.fold(log.read(path))
        conflicts = st.conflicts_for(sc, actor)
        ts = next(clock)
        if conflicts:
            holder = conflicts[0]
            # A refusal is itself an event. Without it a prevented collision
            # leaves no trace, which is how the tool ends up unable to show it
            # ever did anything.
            log.append(path, log.Event(
                ts=ts, id=log.new_id(ts), actor=actor, type="blocked",
                data={"scope": str(sc), "intent": intent,
                      "holder": holder.actor, "holder_scope": str(holder.scope)},
            ))
            return Outcome(allowed=False, conflicts=conflicts)
        ev = log.Event(ts=ts, id=log.new_id(ts), actor=actor, type="claim",
                       scope=[str(sc)], data={"intent": intent})
        log.append(path, ev)
        report = contact.contact(
            graph, resolve.resolve(graph, sc, root), _others(st, graph, root, actor)
        )
        return Outcome(allowed=True, advice=contact.render(report),
                       contact_report=report, claim_id=ev.id)


def _release(root: Path, actor: str, claim_id: str, result: str, clock) -> None:
    """`comms release`: hand the ground back."""
    path = log.log_path(root)
    with file_lock(log.store_dir(root) / "lock"):
        ts = next(clock)
        log.append(path, log.Event(
            ts=ts, id=log.new_id(ts), actor=actor, type="release",
            data={"refs": [claim_id], "result": result},
        ))


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class Transcript:
    root: Path
    log_path: Path
    extract_stdout: str
    a_claim: Outcome
    b_check_same_file: Outcome
    b_claim_neighbour: Outcome
    control_claim: Outcome
    b_blocked_while_held: Outcome
    b_claim_after_release: Outcome
    final_state: state.State


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> Transcript:
    base = tmp_path_factory.mktemp("comms-e2e")
    root = base / "repo"
    root.mkdir()
    home = base / "home"
    home.mkdir()
    _write_repo(root)

    # HOME decides where the comms store lives, and the store is the whole point
    # of the flow. Pin it for the duration of the run and put it back after, so
    # this never writes into the developer's real ~/Library.
    saved_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        proc = _extract(root, home)
        if proc.returncode != 0:
            pytest.fail(
                "`graphify extract --code-only` failed on a plain three-file Python "
                f"repo, so the map the whole flow rests on does not exist:\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
        graph_json = root / "graphify-out" / "graph.json"
        assert graph_json.exists(), f"extract reported success but wrote no graph.json: {proc.stdout}"

        from graphify.paths import load_node_link_graph
        graph = load_node_link_graph(str(graph_json))

        clock = _clock()
        a_claim = _claim(root, graph, AGENT_A, A_SCOPE, "make save_record idempotent", clock)
        b_check = _check(root, graph, AGENT_B, "core/store.py")
        b_neighbour = _claim(root, graph, AGENT_B, B_SCOPE, "add a discount hook", clock)
        control = _claim(root, graph, AGENT_C, CONTROL_SCOPE, "switch to minor units", clock)
        b_blocked = _claim(root, graph, AGENT_B, A_SCOPE, "also wanted this", clock)
        _release(root, AGENT_A, a_claim.claim_id, "done", clock)
        b_after = _claim(root, graph, AGENT_B, A_SCOPE, "picking up where A left off", clock)
        final = _load_state(root)

        return Transcript(
            root=root,
            log_path=log.log_path(root),
            extract_stdout=proc.stdout + proc.stderr,
            a_claim=a_claim,
            b_check_same_file=b_check,
            b_claim_neighbour=b_neighbour,
            control_claim=control,
            b_blocked_while_held=b_blocked,
            b_claim_after_release=b_after,
            final_state=final,
        )
    finally:
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home


# ---------------------------------------------------------------------------
# The beats
# ---------------------------------------------------------------------------


def test_the_map_was_built_from_real_code_offline(run: Transcript):
    """If this fails: `graphify extract --code-only` no longer indexes a plain
    Python package without an API key, so every agent working offline gets a
    comms install whose map half is permanently empty -- and an empty map makes
    contact() answer "no map" to every question, which reads like "all clear"."""
    assert "AST extraction" in run.extract_stdout
    assert (run.root / "graphify-out" / "graph.json").exists()


def test_the_log_lives_outside_the_repo_and_survives_the_map(run: Transcript):
    """If this fails: the log is inside the working tree or inside graphify-out.
    Both are fatal in the same way -- graphify-out is deleted and rewritten whole
    on every extract, and a repo-local log gets committed and then shared between
    checkouts. Either one loses live claims without anybody noticing."""
    assert run.log_path.exists(), "the flow wrote no log at all"
    assert run.root not in run.log_path.parents, (
        f"the event log landed inside the repo ({run.log_path}); a rebuild or a "
        "commit will take coordination state with it"
    )


def test_first_agent_gets_the_ground_it_asked_for(run: Transcript):
    """If this fails: an agent cannot claim anything on an empty log, so nobody
    ever gets past the first command and the tool is inert."""
    assert run.a_claim.allowed
    assert run.a_claim.claim_id, "a granted claim must come back with an id to release later"


def test_checking_a_file_names_who_is_in_it_and_what_they_are_doing(run: Transcript):
    """If this fails: agent B asks about core/store.py while A is editing a symbol
    inside it and is told nothing. This is the collision the tool exists to
    prevent, and a whole-file question missing a symbol-level claim is the exact
    shape of the miss -- it looks like a clean answer, not like an error."""
    conflicts = run.b_check_same_file.conflicts
    assert conflicts, "checking a file with a live claim inside it reported nothing"
    holder = conflicts[0]
    assert holder.actor == AGENT_A
    assert str(holder.scope) == A_SCOPE, "the answer must echo the scope actually held"
    assert "idempotent" in holder.intent, (
        "the holder's intent is the part a human acts on -- without it the answer "
        "is 'somebody is there', which is not enough to decide anything"
    )


def test_claiming_next_door_is_allowed_but_warns_about_the_coupling(run: Transcript):
    """The headline feature. B claims place_order, which CALLS the save_record
    that A is rewriting. Nothing may block B -- ordering was measured and does not
    predict anything -- but B must be told, by name, before starting.

    If this fails: two agents rewrite both halves of one call site in parallel,
    each believing the other is somewhere unrelated. That is the failure the
    contact warning was built for, and its absence is silent."""
    out = run.b_claim_neighbour
    assert out.allowed, "a neighbouring claim must never be blocked -- advice, not a verdict"
    advice = out.advice
    assert "could not place your claim" not in advice, (
        "B's claim did not land on the map at all, so the warning could not even be "
        f"attempted. The map, not the claim, is what needs fixing here.\n{advice}"
    )
    assert AGENT_A in advice, f"the warning must name who to talk to:\n{advice}"
    assert "save_record" in advice, f"the warning must name their code:\n{advice}"
    assert "place_order" in advice, f"the warning must name my code:\n{advice}"
    assert "calls" in advice.lower(), (
        f"the warning must say HOW the two are connected; 'related' with no reason "
        f"is not actionable:\n{advice}"
    )


def test_a_symbol_claim_uses_the_name_a_person_types(run: Transcript):
    """The joint between the two halves of the product, isolated.

    A claim is typed by a human or an agent as a source symbol: ``save_record``.
    The map labels callables with a call-shaped suffix: ``save_record()``. Every
    other consumer in graphify closes that gap before matching -- affected.py:63,
    serve.py:524 and symbol_resolution.py:33 all strip the parentheses.

    If this fails: no symbol-level claim in any language whose callables get that
    label ever lands on the map, so the contact warning is dead for the case it
    was built for. It does not fail loudly at the CLI either -- the user sees
    "could not place your claim on the map", reads it as a scope typo, retypes
    the same name, and concludes the map half does not work."""
    from graphify.paths import load_node_link_graph

    graph = load_node_link_graph(str(run.root / "graphify-out" / "graph.json"))
    res = resolve.resolve(graph, scope_mod.parse(A_SCOPE), run.root)
    assert res.places, (
        f"claiming {A_SCOPE!r} -- the name as it is spelled in the source -- resolved "
        f"to nothing. miss_reason: {res.miss_reason}"
    )
    assert res.places[0].source_file == "core/store.py"
    assert res.places[0].start_line == 4, "the claim must land on the definition, not the file"


def test_an_unconnected_claim_produces_no_warning(run: Transcript):
    """The control, and the reason the previous test means anything. billing.charge
    is in the same repo and the same language and touches neither claim.

    If this fails: the warning fires regardless of the map, so it carries no
    information. Agents learn to ignore it within a day and the feature is worse
    than absent, because it is trusted for a while first."""
    out = run.control_claim
    assert out.allowed
    assert "could not place your claim" not in out.advice, (
        "the control claim never reached the map, so its silence proves nothing "
        f"about the map:\n{out.advice}"
    )
    assert not out.contact_report.touches, (
        f"an unconnected file was reported as near somebody's work: {out.contact_report.touches}"
    )
    assert "NEARBY" not in out.advice and "SAME GROUND" not in out.advice
    assert out.contact_report.note, (
        "'nothing found' must still say so out loud -- an empty report and a report "
        "that could not be produced must never look the same to the reader"
    )


def test_the_exact_same_symbol_is_refused_while_it_is_held(run: Transcript):
    """If this fails: exclusivity does not exist and every other guarantee here is
    decoration -- two agents edit one function at once and the second write wins."""
    out = run.b_blocked_while_held
    assert not out.allowed, "B took a symbol A was already holding"
    assert out.conflicts[0].actor == AGENT_A


def test_a_refusal_is_recorded_so_the_tool_can_show_it_worked(run: Transcript):
    """If this fails: prevented collisions leave no trace, and a log of thousands
    of claims can honestly report that it never prevented anything -- which is how
    a team decides the tool is not worth running."""
    blocked = [b for b in run.final_state.blocked if b.actor == AGENT_B]
    assert blocked, "the refusal was never written to the log"
    assert blocked[0].holder == AGENT_A
    assert blocked[0].holder_scope == A_SCOPE


def test_release_hands_the_ground_back(run: Transcript):
    """If this fails: released claims stay live forever. Agents finish, the scopes
    they touched stay locked, and within a session every useful file is claimed by
    somebody who left -- so everyone starts ignoring the tool to get work done."""
    assert run.b_claim_after_release.allowed, (
        "A released save_record and B still could not take it"
    )
    assert not run.final_state.active_claims_by_actor(AGENT_A), (
        "A's claim is still active after A released it"
    )
    held_by_b = {str(c.scope) for c in run.final_state.active_claims_by_actor(AGENT_B)}
    assert A_SCOPE in held_by_b, "B's new claim on the freed scope is not in the state"


def test_the_whole_run_is_replayable_from_the_log_alone(run: Transcript):
    """The log is the only durable copy of coordination state; State is a view.

    If this fails: two agents in two processes fold the same file into different
    worlds, so one of them is acting on a state the other cannot see. Everything
    above is then true only inside one process, which is the one case that does
    not matter."""
    replayed = state.fold(log.read(run.log_path))
    assert {k: str(v.scope) for k, v in replayed.claims.items()} == {
        k: str(v.scope) for k, v in run.final_state.claims.items()
    }
    assert len(replayed.blocked) == len(run.final_state.blocked)
    assert len(replayed.releases) == len(run.final_state.releases)


# ---------------------------------------------------------------------------
# Packaging: what is in the repo is not what reaches a user
# ---------------------------------------------------------------------------


def test_every_comms_data_file_is_declared_for_the_wheel():
    """IF THIS FAILS: a non-Python file in graphify/comms/ is dropped from the
    built wheel, and the drop is INVISIBLE. Tests pass — they run against the
    source tree, where the file is always there. Only a user installing from a
    wheel meets the failure.

    This bit once already: instructions.md (the fork's always-on coordination
    text) was never declared, so setuptools left it out, and the loader treats a
    missing file as "coordination guidance is optional" and returns "". The
    install SUCCEEDED, reported nothing wrong, and set up no coordination at
    all — agents were simply never told to claim files. There is no way to
    notice that from the outside, which is what makes it worth a test.

    include-package-data is false and the package-data table is per-package, so
    a new data file needs a new declaration. This asserts the declaration covers
    what is actually on disk rather than trusting anyone to remember.
    """
    import fnmatch
    import tomllib

    root = Path(__file__).resolve().parents[1]
    pkg = root / "comms_graph"
    cfg = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    setuptools_cfg = cfg["tool"]["setuptools"]
    declared = setuptools_cfg.get("packages")
    covered = (any(fnmatch.fnmatch("comms_graph", g)
                   for g in declared.get("find", {}).get("include", []))
               if isinstance(declared, dict) else "comms_graph" in (declared or []))
    assert covered, f"comms_graph is not covered by packages (declared: {declared})"
    globs = setuptools_cfg.get("package-data", {}).get("comms_graph", [])

    on_disk = sorted(
        f.name
        for f in pkg.iterdir()
        if f.is_file() and f.suffix != ".py" and not f.name.startswith(".")
    )
    undeclared = [
        name for name in on_disk if not any(fnmatch.fnmatch(name, g) for g in globs)
    ]
    assert not undeclared, (
        f"comms_graph/ holds {undeclared} which no [tool.setuptools.package-data] "
        f'glob for "comms_graph" matches (declared: {globs}). These files exist in '
        "the repo but will NOT be in the wheel, and nothing at runtime will say so."
    )


def test_every_command_the_briefing_teaches_actually_exists():
    """IF THIS FAILS: the always-on instructions teach an agent a command the
    CLI does not dispatch.

    That file is the ENTIRE instruction set an agent gets, and it is loaded on
    every turn — so a verb renamed here and not there sends every agent in the
    fleet at something that exits 2. The briefing is documentation that runs.
    """
    import re

    root = Path(__file__).resolve().parents[1]
    briefing = (root / "comms_graph" / "instructions.md").read_text(encoding="utf-8")
    cli_src = (root / "comms_graph" / "cli.py").read_text(encoding="utf-8")

    # Every "comms-graph <verb>" the briefing shows, plus any "task <sub>".
    verbs = set(re.findall(r"comms-graph ([a-z-]+)", briefing))
    subs = set(re.findall(r"comms-graph task ([a-z-]+)", briefing))
    assert verbs, "the briefing should show at least one command"

    dispatched = set(re.findall(r'sub == "([a-z-]+)"', cli_src))
    dispatched |= set(re.findall(r'verb == "([a-z-]+)"', cli_src))

    missing = {v for v in verbs if v not in dispatched}
    assert not missing, f"briefing teaches {sorted(missing)}, which the CLI does not dispatch"
    missing_subs = {v for v in subs if v not in dispatched}
    assert not missing_subs, f"briefing teaches 'task {sorted(missing_subs)}', not dispatched"


def test_the_briefing_names_the_flags_it_uses():
    """A flag shown in the briefing that the parser ignores is worse than one
    that errors: --task was accepted and silently dropped for exactly this
    reason, and nothing said so."""
    import re

    root = Path(__file__).resolve().parents[1]
    briefing = (root / "comms_graph" / "instructions.md").read_text(encoding="utf-8")
    cli_src = (root / "comms_graph" / "cli.py").read_text(encoding="utf-8")

    flags = set(re.findall(r"--([a-z-]{2,})", briefing))
    flags -= {"as", "code-only"}  # --as is universal; --code-only belongs to extract
    for flag in sorted(flags):
        assert f'"{flag}"' in cli_src, f"briefing shows --{flag}, which the CLI never reads"


def test_the_hook_path_is_the_one_the_cli_dispatches():
    """IF THIS FAILS: there are two implementations of check again and the
    dangerous one is reachable.

    graphify/comms/commands.py holds a complete, plausible, well-commented
    implementation of claim/check/release that NOTHING imports and that has not
    tracked any of the correctness work — the self-review gate, filesystem-aware
    paths, the fail-closed wrapper. It is marked as superseded. This pins the
    fact that the CLI does not quietly start using it again.
    """
    import re

    root = Path(__file__).resolve().parents[1]
    cli_src = (root / "comms_graph" / "cli.py").read_text(encoding="utf-8")

    assert 'sub == "check"' in cli_src
    assert "_cmd_check_inner" in cli_src, "the fail-closed wrapper must still be there"
    assert not re.search(r"^\s*from \. import commands", cli_src, re.M), \
        "cli.py must not import the superseded command layer"

    for name in ("commands", "tools"):
        assert not (root / "comms_graph" / f"{name}.py").exists(), (
            f"{name}.py is back. It is a second implementation that nothing dispatches.")
