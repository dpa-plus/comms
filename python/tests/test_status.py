"""The two read surfaces: `comms status` and `comms log`.

These are the commands nobody double-checks. A session starts, the hook runs
`comms status --since 24h`, and whatever it prints is what every agent in the
repo believes about who is working where. So the failures worth testing are not
crashes: they are the confident wrong answers:

  * a board that looks calm because the log could not be read
  * a claim held since yesterday that reads exactly like one taken two minutes
    ago, so nobody ever goes around a crashed agent
  * a filter that quietly returns nothing, which is indistinguishable from
    "there is no history here"
  * a read surface that writes, in a store several agents are appending to

Each test says what breaks in the real world if it fails.
"""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from comms_graph import log as clog
from comms_graph import status as cstatus

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _repo(tmp_path, monkeypatch):
    """A git repo with an isolated HOME, so the store is this test's alone."""
    import shutil
    import subprocess

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    subprocess.run([git, "init", "-q", "."], cwd=repo, check=True)
    return repo


def _streams(repo, verb, *args, session=None):
    """Run one read surface in `repo`. Returns (exit_code, stdout, stderr).

    The two streams are kept APART rather than merged, because that separation
    is load-bearing for these commands: warnings and errors go to stderr
    precisely so `status --json | jq` keeps working, and a helper that merged
    them would let a regression that prints a warning onto stdout pass.

    `session` is the host agent's session id, kept out of the way for the same
    reason the task-graph tests do it: an inherited pytest session would make
    every actor in the test look like one agent.
    """
    out, err = io.StringIO(), io.StringIO()
    was = os.getcwd()
    prior = os.environ.get("CLAUDE_CODE_SESSION_ID")
    os.chdir(repo)
    try:
        if session is not None:
            os.environ["CLAUDE_CODE_SESSION_ID"] = session
        else:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cstatus.main([verb, *args])
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
    finally:
        os.chdir(was)
        if prior is None:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        else:
            os.environ["CLAUDE_CODE_SESSION_ID"] = prior
    return code, out.getvalue(), err.getvalue()


def _run(repo, verb, *args, session=None):
    """(exit_code, everything the caller would see on a terminal)."""
    code, out, err = _streams(repo, verb, *args, session=session)
    return code, out + err


def _log_file(repo) -> Path:
    path = clog.log_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write(repo, events):
    """Append events straight to the store, exactly as the Go build would."""
    path = _log_file(repo)
    for ev in events:
        clog.append(path, ev)
    return path


def _ev(typ, actor, ago=timedelta(0), data=None, scope=None, now=None):
    ts = (now or datetime.now(UTC)) - ago
    return clog.Event(ts=ts, id=clog.new_id(ts), actor=actor, type=typ,
                      scope=scope, data=data or {})


# ---------------------------------------------------------------------------
# status: the roster
# ---------------------------------------------------------------------------


def test_status_since_24h_is_the_invocation_the_session_hook_makes(tmp_path, monkeypatch):
    """IF THIS FAILS: every session in the repo starts blind. The SessionStart
    hook runs exactly `comms status --since 24h`; if that flag is unsupported or
    the command exits non-zero, the first thing every agent sees is an error
    instead of who is holding what."""
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, [
        _ev("hello", "claude-dev"),
        _ev("claim", "claude-dev", scope=["a.py"], data={"intent": "fix the parser"}),
    ])

    code, out = _run(repo, "status", "--since", "24h")

    assert code == 0, out
    assert "ACTIVE SESSIONS" in out and "ACTIVE CLAIMS" in out
    assert "@claude-dev" in out
    assert "a.py" in out and "fix the parser" in out


def test_an_empty_store_reports_an_empty_board_not_an_error(tmp_path, monkeypatch):
    """IF THIS FAILS: the first agent into a fresh repo is met with a failure for
    the crime of being first. A repo nobody has claimed in yet genuinely has
    nothing to report, and that is a 0 with `(none)`, not an error."""
    repo = _repo(tmp_path, monkeypatch)

    code, out = _run(repo, "status")

    assert code == 0, out
    assert out.count("(none)") >= 2


def test_a_claim_held_past_the_stale_window_is_flagged_and_its_holder_named_dead(
    tmp_path, monkeypatch
):
    """IF THIS FAILS: a crashed agent's locks are indistinguishable from live
    work forever. Nobody steals a claim they believe somebody is using, so
    without the STALE tag and the LIKELY DEAD row the whole repo waits on a
    process that exited hours ago."""
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, [
        _ev("hello", "ghost", ago=timedelta(hours=6)),
        _ev("claim", "ghost", ago=timedelta(hours=6), scope=["a.py"],
            data={"intent": "half a refactor"}),
    ])

    code, out = _run(repo, "status")

    assert code == 0, out
    assert "STALE" in out, "a six-hour-old claim must not read like a fresh one"
    assert "LIKELY DEAD" in out
    assert "@ghost" in out, "a silent holder stays on the roster; its locks need releasing"


def test_a_silent_holder_stays_on_the_roster_even_though_the_window_dropped_it(
    tmp_path, monkeypatch
):
    """IF THIS FAILS: the row offering the fix vanishes at exactly the moment it
    is needed. An actor silent past the 4h activity window but still holding
    ground is the crash case; drop it from the roster and the operator sees
    claims with nobody attached and no one to chase."""
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, [
        _ev("hello", "crashed", ago=timedelta(hours=9)),
        _ev("claim", "crashed", ago=timedelta(hours=9), scope=["a.py"], data={}),
        _ev("hello", "alive"),
    ])

    code, out = _run(repo, "status")

    roster = out.split("ACTIVE CLAIMS")[0]
    assert "@crashed" in roster
    assert "@alive" in roster


def test_an_idle_actor_holding_nothing_is_not_accused_of_being_dead(tmp_path, monkeypatch):
    """IF THIS FAILS: the crash warning cries wolf. Silence on its own is an
    agent thinking; only silence WHILE holding locks is the signal, and a flag
    that fires on the benign case gets ignored on the real one."""
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, [_ev("hello", "quiet", ago=timedelta(hours=3))])

    code, out = _run(repo, "status")

    assert code == 0, out
    assert "LIKELY DEAD" not in out


def test_the_stale_threshold_is_movable_and_actually_moves(tmp_path, monkeypatch):
    """IF THIS FAILS: --stale-after is decorative, and the CLI cannot be pointed
    at the same threshold as the dashboard. Two surfaces disagreeing about when
    a claim is abandoned is worse than neither having a flag."""
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, [
        _ev("hello", "dev", ago=timedelta(minutes=20)),
        _ev("claim", "dev", ago=timedelta(minutes=20), scope=["a.py"], data={}),
    ])

    fresh_code, fresh = _run(repo, "status", "--stale-after", "1h")
    tight_code, tight = _run(repo, "status", "--stale-after", "5m")

    assert fresh_code == 0 and tight_code == 0
    assert "STALE" not in fresh, "20 minutes is not stale at the default threshold"
    assert "STALE" in tight, "20 minutes IS stale at a five-minute threshold"


def test_collisions_prevented_is_reported_all_time(tmp_path, monkeypatch):
    """IF THIS FAILS: the only number that justifies the ceremony disappears.
    A prevented collision is not news that goes stale: a store of thousands of
    claims reporting zero collisions ever prevented is what this line exists to
    stop."""
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, [
        _ev("hello", "alice"),
        _ev("claim", "alice", scope=["a.py"], data={"intent": "mine"}),
        _ev("blocked", "bob", ago=timedelta(days=9),
            data={"scope": "a.py", "holder": "alice", "intent": "also mine"}),
    ])

    code, out = _run(repo, "status", "--since", "24h")

    assert code == 0, out
    assert "COLLISIONS PREVENTED: 1 all time" in out, (
        "an old refusal still counts; the total is the point"
    )
    assert "@bob was stopped from editing a.py (held by @alice)" in out


def test_a_task_refusal_is_not_rendered_as_an_editing_collision(tmp_path, monkeypatch):
    """IF THIS FAILS: a refused self-review prints "stopped from editing  (held
    by @)": an empty scope and an empty holder, which reads as a bug in comms
    rather than as the review gate doing its job."""
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, [
        _ev("blocked", "solo", data={"task": "auth", "attempted": "verified",
                                     "reason": "self-review"}),
    ])

    code, out = _run(repo, "status")

    assert code == 0, out
    assert "stopped from editing" not in out
    assert "@solo was refused on task auth (self-review)" in out


def test_findings_and_notes_outside_the_window_are_not_shown(tmp_path, monkeypatch):
    """IF THIS FAILS: --since means nothing on the feeds it exists for, and a
    week-old note reads as something somebody just said."""
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, [
        _ev("note", "old", ago=timedelta(days=5), data={"body": "ancient history"}),
        _ev("note", "new", data={"body": "just now"}),
    ])

    code, out = _run(repo, "status", "--since", "24h")

    assert "just now" in out
    assert "ancient history" not in out


def test_status_json_is_the_machine_shape_and_is_uncapped(tmp_path, monkeypatch):
    """IF THIS FAILS: anything built on `status --json`: a dashboard, a
    supervisor deciding whether to start an agent: reads a different world than
    the human output, or worse, a truncated one. The human view caps claims for
    readability; the machine view must not, or a consumer can edit into a
    conflict the cap hid."""
    repo = _repo(tmp_path, monkeypatch)
    events = [_ev("hello", "dev")]
    for i in range(60):
        events.append(_ev("claim", "dev", scope=[f"f{i}.py"], data={"intent": f"w{i}"}))
    _write(repo, events)

    code, out, err = _streams(repo, "status", "--json")

    assert code == 0, err
    doc = json.loads(out)
    assert {"sessions", "claims", "findings", "notes", "tasks"} <= set(doc)
    assert len(doc["claims"]) == 60, "the machine view must not hide live claims"
    assert doc["sessions"][0]["actor"] == "dev"
    assert "last_seen" in doc["sessions"][0]


def test_status_json_marks_a_stale_claim_as_stale(tmp_path, monkeypatch):
    """IF THIS FAILS: a supervisor reading JSON cannot tell an abandoned lock
    from a live one, so automated cleanup either never runs or runs on
    everything."""
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, [
        _ev("hello", "ghost", ago=timedelta(hours=5)),
        _ev("claim", "ghost", ago=timedelta(hours=5), scope=["a.py"], data={}),
    ])

    code, out, err = _streams(repo, "status", "--json")

    assert code == 0, err
    doc = json.loads(out)
    assert doc["claims"][0]["stale"] is True
    assert doc["claims"][0]["age"] == "5h"


# ---------------------------------------------------------------------------
# log: the filters
# ---------------------------------------------------------------------------


def test_log_prints_readable_history_within_the_default_window(tmp_path, monkeypatch):
    """IF THIS FAILS: the history is only readable as JSON, and the default
    window silently shows everything ever, which on a real store is thousands
    of lines where the caller asked for today."""
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, [
        _ev("hello", "dev", ago=timedelta(days=30)),
        _ev("finding", "dev", data={"category": "bug", "summary": "N+1 in the loader"}),
        _ev("note", "dev", data={"body": "migration lands next session"}),
    ])

    code, out = _run(repo, "log")

    assert code == 0, out
    assert "finding  @dev  [bug] N+1 in the loader" in out
    assert "note     @dev  migration lands next session" in out
    assert "hello" not in out, "the default window is 24h, and the hello is 30 days old"


def test_log_type_filter_rejects_a_type_this_build_cannot_read(tmp_path, monkeypatch):
    """IF THIS FAILS: a typo like `--type findings` silently matches nothing and
    reads as "there are no findings": the exact confident wrong answer this
    surface must never give. The whitelist has to refuse, loudly, with the real
    names in the message."""
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, [_ev("finding", "dev", data={"category": "bug", "summary": "x"})])

    code, out = _run(repo, "log", "--type", "findings")

    assert code == 2, out
    assert "unknown event type" in out
    assert "finding" in out, "the error must name the types that DO work"


def test_log_type_filter_keeps_only_the_named_types(tmp_path, monkeypatch):
    """IF THIS FAILS: --type is decorative and every query returns the whole
    log, which is how a filter stops being read at all."""
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, [
        _ev("hello", "dev"),
        _ev("claim", "dev", scope=["a.py"], data={"intent": "work"}),
        _ev("finding", "dev", data={"category": "fix", "summary": "patched it"}),
    ])

    code, out = _run(repo, "log", "--type", "finding,claim")

    assert code == 0, out
    assert "patched it" in out and "work" in out
    assert "hello" not in out


def test_log_scope_filter_matches_a_finding_that_names_the_file_only_by_ref(
    tmp_path, monkeypatch
):
    """IF THIS FAILS: `log --scope <file> --type finding`: the query an agent
    runs to learn a file's history before touching it: returns nothing while
    the log is full of findings about that file. Findings carry their target in
    data.refs and have an EMPTY top-level scope, so matching only the scope
    array answers "no history" for every one of them."""
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, [
        _ev("finding", "dev", data={
            "category": "gotcha",
            "summary": "the loader caches across requests",
            "refs": [{"kind": "path", "value": "src/loader.py"}],
        }),
        _ev("finding", "dev", data={
            "category": "bug", "summary": "unrelated",
            "refs": [{"kind": "path", "value": "src/other.py"}],
        }),
    ])

    code, out = _run(repo, "log", "--scope", "src/loader.py")

    assert code == 0, out
    assert "caches across requests" in out
    assert "unrelated" not in out


def test_log_category_filter_excludes_everything_that_is_not_a_finding(
    tmp_path, monkeypatch
):
    """IF THIS FAILS: asking for the decisions returns claims and notes too. A
    category is a question about findings; letting other types through a filter
    that cannot apply to them buries the answer."""
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, [
        _ev("note", "dev", data={"body": "chatter"}),
        _ev("finding", "dev", data={"category": "decision", "summary": "uuid keys"}),
        _ev("finding", "dev", data={"category": "bug", "summary": "leaky handle"}),
    ])

    code, out = _run(repo, "log", "--category", "decision")

    assert code == 0, out
    assert "uuid keys" in out
    assert "leaky handle" not in out and "chatter" not in out


def test_log_rejects_a_category_that_is_not_one_of_the_five(tmp_path, monkeypatch):
    """IF THIS FAILS: `--category bugs` matches nothing and reads as "no bugs
    recorded", which is the most dangerous possible wrong answer from a tool
    whose job is remembering problems."""
    repo = _repo(tmp_path, monkeypatch)

    code, out = _run(repo, "log", "--category", "bugs")

    assert code == 2, out
    assert "unknown category" in out


def test_log_actor_filter_narrows_to_one_agent(tmp_path, monkeypatch):
    """IF THIS FAILS: there is no way to ask what one agent did, which is the
    first question anybody asks after a bad session."""
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, [
        _ev("note", "alice", data={"body": "alice was here"}),
        _ev("note", "bob", data={"body": "bob was here"}),
    ])

    code, out = _run(repo, "log", "--actor", "alice")

    assert "alice was here" in out
    assert "bob was here" not in out


def test_log_json_emits_one_replayable_line_per_event(tmp_path, monkeypatch):
    """IF THIS FAILS: `log --json` is not the log. Its output is what somebody
    pipes into a merge, an archive, or another store, and a line that does not
    decode back to the same event silently rewrites history at the far end."""
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, [
        _ev("claim", "dev", scope=["a.py#parse"], data={"intent": "narrow it"}),
        _ev("finding", "dev", data={"category": "fix", "summary": "done"}),
    ])

    code, out, err = _streams(repo, "log", "--json")

    assert code == 0, err
    lines = out.splitlines()
    assert len(lines) == 2
    decoded = [clog.Event.decode(ln) for ln in lines]
    assert decoded[0].type == "claim" and decoded[0].scope == ["a.py#parse"]
    assert decoded[1].data["summary"] == "done"


def test_a_release_that_somebody_else_forced_does_not_read_as_a_normal_handback(
    tmp_path, monkeypatch
):
    """IF THIS FAILS: "she finished" and "somebody took it off her" print the
    same line. Those are opposite facts about a claim, and the second one is the
    one that needs looking into."""
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, [
        _ev("claim", "alice", scope=["a.py"], data={"intent": "work"}),
        _ev("release", "bob", data={"refs": ["01ABC"], "result": "abandoned",
                                    "original_actor": "alice"}),
    ])

    code, out = _run(repo, "log")

    assert code == 0, out
    assert "arbitrated @alice's claim" in out


# ---------------------------------------------------------------------------
# Both: honesty about what could not be read, and never writing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["status", "log"])
def test_a_corrupt_log_is_reported_not_rendered_as_an_empty_board(
    verb, tmp_path, monkeypatch
):
    """IF THIS FAILS: a broken log looks exactly like a quiet repo. An agent
    reads "(none)", concludes nothing is claimed, and edits straight into
    somebody's ground, while the file on disk plainly says otherwise. "I could
    not find out" must never render as "nothing is held"."""
    repo = _repo(tmp_path, monkeypatch)
    path = _write(repo, [_ev("claim", "dev", scope=["a.py"], data={"intent": "x"})])
    with open(path, "a") as fh:
        fh.write("{ this is not json }\n")

    code, out = _run(repo, verb)

    assert code == 2, out
    assert "unreadable" in out
    assert "(none)" not in out, "a board must not be drawn from a log we could not read"


@pytest.mark.parametrize("verb", ["status", "log"])
def test_a_truncated_final_line_still_reports_everything_before_it(
    verb, tmp_path, monkeypatch
):
    """IF THIS FAILS: a writer killed mid-append takes the whole board down with
    it. The half-written last line lost nothing that was already committed, and
    refusing to show any of it turns one interrupted process into a repo-wide
    outage."""
    repo = _repo(tmp_path, monkeypatch)
    path = _write(repo, [
        _ev("hello", "dev"),
        _ev("claim", "dev", scope=["a.py"], data={"intent": "real work"}),
    ])
    with open(path, "a") as fh:
        fh.write('{"ts":"2026-05-22T14:30:00Z","id":"01H","actor":"dev","ty')

    code, out = _run(repo, verb)

    assert code == 0, out
    assert "real work" in out


@pytest.mark.parametrize("verb", ["status", "log"])
def test_a_read_surface_writes_nothing_at_all(verb, tmp_path, monkeypatch):
    """IF THIS FAILS: reading the board mutates it. Several agents run these on
    every session start; a surface that appends, rewrites, or even takes the
    lock turns "check the board" into contention with the agents doing real
    work, and an accidental append corrupts a log that is truth."""
    repo = _repo(tmp_path, monkeypatch)
    path = _write(repo, [
        _ev("hello", "dev"),
        _ev("claim", "dev", scope=["a.py"], data={"intent": "work"}),
        _ev("blocked", "other", data={"scope": "a.py", "holder": "dev"}),
    ])
    before = path.read_bytes()
    lock = path.parent / ".lock"
    lock_before = lock.exists()

    code, _out = _run(repo, verb)

    assert code == 0
    assert path.read_bytes() == before, "the log must be byte-identical after a read"
    assert lock.exists() == lock_before, "a read surface must not take the lock"


@pytest.mark.parametrize("verb", ["status", "log"])
def test_a_read_surface_does_not_create_the_store(verb, tmp_path, monkeypatch):
    """IF THIS FAILS: asking a question builds a store. That is how one
    unwritable HOME denied every edit in every repository on the machine: the
    read path mkdir'd, the mkdir failed, and the fail-closed hook turned the
    exception into a block."""
    repo = _repo(tmp_path, monkeypatch)
    store = clog.store_dir(repo)
    assert not store.exists()

    code, _out = _run(repo, verb)

    assert code == 0
    assert not store.exists(), "a pure read must not bring the store into existence"


@pytest.mark.parametrize("verb", ["status", "log"])
def test_a_window_the_go_build_would_reject_is_rejected_here_too(
    verb, tmp_path, monkeypatch
):
    """IF THIS FAILS: the two builds accept different windows over one shared
    log. `--since 7d` works here and dies on the Go build, so a script copied
    between them breaks for whoever inherited it, and quietly accepting it as
    something else would show the wrong window under the right label."""
    repo = _repo(tmp_path, monkeypatch)

    code, out = _run(repo, verb, "--since", "7d")

    assert code == 2, out
    assert "168h" in out, "the error has to name the spelling that works"


@pytest.mark.parametrize("verb", ["status", "log"])
def test_a_negative_window_is_refused_rather_than_quietly_clamped(
    verb, tmp_path, monkeypatch
):
    """IF THIS FAILS: `--since -24h` prints a normal-looking board for the wrong
    period. A caller whose date arithmetic went negative gets a plausible answer
    instead of the bug."""
    repo = _repo(tmp_path, monkeypatch)

    code, out = _run(repo, verb, "--since=-24h")

    assert code == 2, out
    assert "negative" in out


@pytest.mark.parametrize("verb", ["status", "log"])
def test_asking_how_a_read_surface_works_has_no_side_effect(verb, tmp_path, monkeypatch):
    """IF THIS FAILS: discovery costs something. For a tool whose users are
    agents, `--help` is the whole discovery path, and it must never do anything
    but print."""
    repo = _repo(tmp_path, monkeypatch)

    code, out = _run(repo, verb, "--help")

    assert code == 0
    assert "Usage" in out
    assert not clog.store_dir(repo).exists()
