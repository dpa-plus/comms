"""Named coordination sessions: the tag every other event carries.

A session is the only thing in comms that spans agents AND spans verbs. If it
breaks, it breaks quietly: the commands still exit 0, the claims are still
recorded, and the damage shows up later — ground that `end` cannot free because
it was never tagged, two halves of one group working under two ids that look
like different projects, or an agent that starts a session and is then blocked
from editing the file it just claimed because its identity record was
overwritten.

Each test says what breaks in the real world if it fails.
"""

from __future__ import annotations

import io
import os
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

from comms_graph import log as clog
from comms_graph import session as csession
from comms_graph import state as cstate

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _cli(repo, *args, session=None):
    """Run the real command in `repo`. Returns (exit_code, stdout+stderr).

    `session` is the host agent's session id, per actor, exactly as in
    test_task_graph: leaving it unset makes every actor in the test look like
    one agent, which for the identity tests below would be the bug passing
    itself off as the fix.

    `session <verb>` is dispatched here rather than through cli.main because the
    wiring line in cli.py is the caller's to add; everything else goes through
    the real entry point.
    """
    from comms_graph import cli as ccli

    out = io.StringIO()
    was = os.getcwd()
    prior = os.environ.get("CLAUDE_CODE_SESSION_ID")
    os.chdir(repo)
    try:
        if session is not None:
            os.environ["CLAUDE_CODE_SESSION_ID"] = session
        else:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        with redirect_stdout(out), redirect_stderr(out):
            try:
                if args and args[0] == "session":
                    code = csession.main(list(args[1:]))
                else:
                    code = ccli.main(list(args))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
    finally:
        os.chdir(was)
        if prior is None:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        else:
            os.environ["CLAUDE_CODE_SESSION_ID"] = prior
    return code, out.getvalue()


def _repo(tmp_path, monkeypatch):
    import shutil
    import subprocess

    import pytest

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("COMMS_LABEL", raising=False)
    monkeypatch.delenv("COMMS_VENDOR", raising=False)
    monkeypatch.delenv("COMMS_MODEL", raising=False)
    (tmp_path / "home").mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (repo / name).write_text("x = 1\n")
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    subprocess.run([git, "init", "-q", "."], cwd=repo, check=True)
    return repo


def _events(repo):
    try:
        return clog.read(clog.log_path(repo))
    except FileNotFoundError:
        return []


def _state(repo):
    return cstate.fold(_events(repo))


def _claim_as_the_wiring_will(repo, actor, scope, *, ts=None):
    """A claim written the way cli.py writes one once `stamp` is wired in.

    The stamping side of claim/release/find/note/task is the caller's line to
    add; this reproduces it so the session behaviour that depends on tagged
    events can be tested for real rather than described.
    """
    st = _state(repo)
    data = {"intent": "work"}
    csession.stamp(st, actor, data)
    when = ts or datetime.now(UTC)
    ev = clog.Event(ts=when, id=clog.new_id(when), actor=actor, type="claim",
                    scope=[scope], data=data)
    clog.append(clog.log_path(repo), ev)
    return ev.id


def _hello_at(repo, actor, when, **data):
    """A hello dated `when` — for the actors a test needs to be silent or old."""
    ev = clog.Event(ts=when, id=clog.new_id(when), actor=actor, type="hello",
                    scope=None, data=data)
    clog.append(clog.log_path(repo), ev)
    return ev.id


# ---------------------------------------------------------------------------
# Starting and joining — one name, one id
# ---------------------------------------------------------------------------


def test_starting_a_session_records_the_id_every_later_event_repeats(tmp_path, monkeypatch):
    """IF THIS FAILS: the session exists in the output and nowhere else.

    There is no `session` event type. The session IS the pair of keys on the
    actor's hello; a start that does not write them leaves a command that prints
    a session id and a log that has never heard of one.
    """
    repo = _repo(tmp_path, monkeypatch)
    code, out = _cli(repo, "session", "start", "auth-refactor", "--as", "claude-dev",
                     "--label", "Claude Dev", session="agent-A")
    assert code == 0, out

    st = _state(repo)
    sess = st.sessions["claude-dev"]
    assert sess.session_name == "auth-refactor"
    assert sess.session_id, "the hello must carry the id, not just print it"
    assert sess.label == "Claude Dev"
    assert sess.session_id in out, "the id has to be printable — peers join by it"

    hello = [e for e in _events(repo) if e.type == "hello"][-1]
    assert hello.data["comms_session_start"] is True
    assert hello.data["comms_session_id"] == sess.session_id
    assert hello.data["comms_session_name"] == "auth-refactor"


def test_two_agents_in_one_named_session_share_one_id(tmp_path, monkeypatch):
    """IF THIS FAILS: joining silently forks the group.

    Everything the session is for — ending it frees exactly its ground, the
    archive counts exactly its work — is keyed on the id. Two agents who believe
    they are in one session but carry two ids coordinate nothing and are told
    nothing.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "auth-refactor", "--as", "claude-dev", session="agent-A")
    code, out = _cli(repo, "session", "join", "auth-refactor", "--as", "codex-dev",
                     session="agent-B")
    assert code == 0, out

    st = _state(repo)
    assert st.sessions["codex-dev"].session_id == st.sessions["claude-dev"].session_id
    joined = [e for e in _events(repo) if e.type == "hello"][-1]
    assert joined.data["comms_session_join"] is True
    assert "comms_session_start" not in joined.data


def test_a_name_is_matched_the_way_a_person_would_say_it(tmp_path, monkeypatch):
    """IF THIS FAILS: `join "Auth-Refactor"` starts nothing and finds nothing.

    Agents retype the name from a sentence, not from the log. A case-sensitive
    lookup turns one session into two, and the canonical spelling has to win so
    the second speller does not record a second name for one piece of work.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "auth-refactor", "--as", "claude-dev", session="agent-A")
    code, out = _cli(repo, "session", "join", "Auth-Refactor", "--as", "codex-dev",
                     session="agent-B")
    assert code == 0, out
    st = _state(repo)
    assert st.sessions["codex-dev"].session_name == "auth-refactor", \
        "the name the session was started with is the one that gets recorded"


def test_starting_a_name_that_is_already_live_is_refused_and_writes_nothing(
        tmp_path, monkeypatch):
    """IF THIS FAILS: two groups work under one name and neither is told.

    A second start mints a SECOND id under the same name. Both halves then see a
    session called auth-refactor on the board, `end` closes one of them, and the
    other half's ground stays held by a session the operator believes they just
    closed.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "auth-refactor", "--as", "claude-dev", session="agent-A")
    before = len(_events(repo))

    code, out = _cli(repo, "session", "start", "auth-refactor", "--as", "codex-dev",
                     session="agent-B")
    assert code == 1, out
    assert "already active" in out
    assert "session join" in out, "a refusal has to say what to do instead"
    assert len(_events(repo)) == before, "a refusal must not append"


def test_joining_a_session_nobody_started_is_refused(tmp_path, monkeypatch):
    """IF THIS FAILS: a typo becomes a private session.

    Creating the name on join would let `join "auth-refctor"` succeed, report a
    session id, and leave the agent coordinating with nobody while believing it
    is in the room.
    """
    repo = _repo(tmp_path, monkeypatch)
    code, out = _cli(repo, "session", "join", "auth-refactor", "--as", "claude-dev",
                     session="agent-A")
    assert code == 1, out
    assert "no active comms session" in out
    assert "session start" in out
    assert _state(repo).sessions.get("claude-dev") is None


# ---------------------------------------------------------------------------
# Switching — the claims do not follow you
# ---------------------------------------------------------------------------


def test_moving_to_another_session_frees_the_ground_you_took_in_the_old_one(
        tmp_path, monkeypatch):
    """IF THIS FAILS: ground taken for one piece of work is held hostage by the next.

    The claim stays tagged with the session the agent has LEFT, so ending that
    session cannot find it (its holder moved on) and the new session does not
    know it exists. The file is then held by somebody who has stopped thinking
    about it until it goes stale — an hour of every other agent being blocked
    for no reason anybody can see.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "first-piece", "--as", "claude-dev", session="agent-A")
    held = _claim_as_the_wiring_will(repo, "claude-dev", "a.py")
    assert held in _state(repo).claims

    _cli(repo, "session", "start", "second-piece", "--as", "claude-dev", session="agent-A")
    st = _state(repo)
    assert held not in st.claims, "the old session's ground must be released"
    assert st.sessions["claude-dev"].session_name == "second-piece"

    audit = [e for e in _events(repo)
             if e.type == "release" and e.data.get("session_retire")][-1]
    assert audit.data["retired_actor"] == "claude-dev"
    assert audit.data["refs"] == [held]
    assert audit.data["comms_session_name"] == "first-piece", \
        "the audit event belongs to the session being left, not the one being joined"
    assert "second-piece" in audit.data["reason"]


def test_the_release_that_ends_the_old_session_folds_before_the_new_hello(
        tmp_path, monkeypatch):
    """IF THIS FAILS: the actor joins nothing.

    The fold sorts by TIMESTAMP, not by append order. If the retire and the
    hello share an instant they can fold in either order, and in the wrong one
    the retire drops the session the hello has just created — leaving an agent
    that ran `session start`, was told it worked, and is on no roster at all.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "first-piece", "--as", "claude-dev", session="agent-A")
    _claim_as_the_wiring_will(repo, "claude-dev", "a.py")
    _cli(repo, "session", "start", "second-piece", "--as", "claude-dev", session="agent-A")

    events = _events(repo)
    retire = [e for e in events if e.data.get("session_retire")][-1]
    hello = [e for e in events if e.type == "hello"][-1]
    assert retire.ts < hello.ts, "earlier in time, not merely earlier in the file"
    assert _state(repo).sessions["claude-dev"].session_name == "second-piece"


def test_a_first_session_does_not_claim_the_actor_came_from_somewhere(tmp_path, monkeypatch):
    """IF THIS FAILS: the audit trail invents a move that never happened.

    The departure event says "actor moved to comms session X" and takes the
    actor off the roster. Writing one for an agent that has never been here puts
    a phantom handover in the history of every session that was ever started,
    and the log is the thing people read months later to work out what happened.
    """
    repo = _repo(tmp_path, monkeypatch)
    code, out = _cli(repo, "session", "start", "auth-refactor", "--as", "claude-dev",
                     session="agent-A")
    assert code == 0, out
    assert not [e for e in _events(repo) if e.data.get("session_retire")]


def test_leaving_a_session_is_recorded_even_when_you_were_holding_nothing(
        tmp_path, monkeypatch):
    """IF THIS FAILS: an agent is quietly in two sessions at once.

    The departure is what says the actor left the first one. Skipping it when
    there are no claims to free leaves the old session's story ending with the
    agent still in the room, and a reader reconstructing who was on what gets
    an actor that appears in two windows with no event between them.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "first-piece", "--as", "claude-dev", session="agent-A")
    _cli(repo, "session", "start", "second-piece", "--as", "claude-dev", session="agent-A")

    audit = [e for e in _events(repo) if e.data.get("session_retire")]
    assert len(audit) == 1, "one departure, from the one session actually left"
    assert audit[0].data["comms_session_name"] == "first-piece"
    assert audit[0].data["refs"] == []


def test_rejoining_the_session_you_are_already_in_keeps_your_claims(tmp_path, monkeypatch):
    """IF THIS FAILS: re-running `join` throws away your own work.

    Agents re-announce themselves — a wrapper script, a resumed session, a
    relabel. Treating that as a move would release every claim the agent is
    actively working behind, and it would look like a no-op command.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "one-piece", "--as", "claude-dev", session="agent-A")
    _cli(repo, "session", "join", "one-piece", "--as", "codex-dev", session="agent-B")
    held = _claim_as_the_wiring_will(repo, "codex-dev", "a.py")

    code, out = _cli(repo, "session", "join", "one-piece", "--as", "codex-dev",
                     "--label", "Codex Dev", session="agent-B")
    assert code == 0, out
    st = _state(repo)
    assert held in st.claims, "same session, same ground"
    assert st.sessions["codex-dev"].label == "Codex Dev"


# ---------------------------------------------------------------------------
# Ending
# ---------------------------------------------------------------------------


def test_ending_a_session_frees_its_claims_and_leaves_the_other_one_alone(
        tmp_path, monkeypatch):
    """IF THIS FAILS: closing one piece of work wipes the repo.

    A release with no comms_session_id is the GLOBAL end, and the fold reads it
    as "archive everything and start over" — every claim and every actor in the
    repo, including the group working in the session next door, who are given no
    warning and whose files are handed to whoever asks next.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "mine", "--as", "claude-dev", session="agent-A")
    mine = _claim_as_the_wiring_will(repo, "claude-dev", "a.py")
    _cli(repo, "session", "start", "theirs", "--as", "codex-dev", session="agent-B")
    theirs = _claim_as_the_wiring_will(repo, "codex-dev", "b.py")

    code, out = _cli(repo, "session", "end", "mine", "--as", "claude-dev",
                     "--reason", "shipped", session="agent-A")
    assert code == 0, out
    assert "released 1 claim." in out, out

    st = _state(repo)
    assert mine not in st.claims
    assert theirs in st.claims, "the session next door keeps its ground"
    assert "codex-dev" in st.sessions
    assert "claude-dev" not in st.sessions, "the ended session's actors leave the roster"

    archived = st.ended_comms_sessions[-1]
    assert archived.name == "mine"
    assert archived.reason == "shipped"
    assert archived.released_refs == [mine]


def test_ending_a_session_nobody_started_is_refused(tmp_path, monkeypatch):
    """IF THIS FAILS: a mistyped name reports a clean shutdown.

    The operator reads "Ended comms session" and stops looking, while the real
    session is still open and still holding every file it took.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "auth-refactor", "--as", "claude-dev", session="agent-A")
    before = len(_events(repo))
    code, out = _cli(repo, "session", "end", "auth-refctor", "--as", "claude-dev",
                     session="agent-A")
    assert code == 1, out
    assert "no active comms session" in out
    assert len(_events(repo)) == before
    assert _state(repo).sessions["claude-dev"].session_name == "auth-refactor"


def test_a_session_whose_agents_all_died_can_still_be_ended_by_name(tmp_path, monkeypatch):
    """IF THIS FAILS: the ground a crashed session took is unreachable by name.

    Liveness is judged on the actors, so a session whose agents have all gone
    quiet is on no roster — but its CLAIMS are still tagged with it and still
    blocking everybody. If `end` only looked at the roster, the one command that
    clears them would answer "no such session" and the files could be freed only
    one claim id at a time.
    """
    repo = _repo(tmp_path, monkeypatch)
    long_ago = datetime.now(UTC) - timedelta(hours=6)
    dead_id = clog.new_id(long_ago)
    _hello_at(repo, "ghost-dev", long_ago, base_name="ghost",
              comms_session_id=dead_id, comms_session_name="ghosted",
              comms_session_start=True)
    held = _claim_as_the_wiring_will(repo, "ghost-dev", "a.py",
                                     ts=long_ago + timedelta(seconds=1))
    assert held in _state(repo).claims

    code, out = _cli(repo, "session", "end", "ghosted", "--as", "human-eli",
                     "--reason", "agents are gone", session="agent-B")
    assert code == 0, out
    st = _state(repo)
    assert held not in st.claims
    assert st.ended_comms_sessions[-1].session_id == dead_id


def test_session_housekeeping_stays_out_of_the_finished_work_feed(tmp_path, monkeypatch):
    """IF THIS FAILS: the "recently completed" feed fills with admin.

    A retire, a leader transfer and a session end are all release events that
    carry refs, because they sweep up the claims they close. Reported as
    finished work they drown the real releases, and the one question the feed
    answers — what actually got done — stops being answerable.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "mine", "--as", "claude-dev", session="agent-A")
    _claim_as_the_wiring_will(repo, "claude-dev", "a.py")
    _cli(repo, "session", "end", "mine", "--as", "claude-dev", session="agent-A")
    assert _state(repo).releases == []


# ---------------------------------------------------------------------------
# Retire
# ---------------------------------------------------------------------------


def test_retiring_an_actor_takes_it_off_the_roster_and_frees_what_it_held(
        tmp_path, monkeypatch):
    """IF THIS FAILS: a crashed agent's locks can only be cleared one at a time.

    Retire is the whole-actor version of `release --force`. Without it the way
    to clean up after an agent that is not coming back is to pass `--as` its
    name, which writes a lie into an append-only log.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "auth-refactor", "--as", "claude-dev", session="agent-A")
    a = _claim_as_the_wiring_will(repo, "claude-dev", "a.py")
    b = _claim_as_the_wiring_will(repo, "claude-dev", "b.py")

    code, out = _cli(repo, "session", "retire", "claude-dev", "--as", "human-eli",
                     "--reason", "process is gone", session="agent-B")
    assert code == 0, out
    assert "released 2 claims" in out
    assert "History remains" in out, "an append-only log must not be described as edited"

    st = _state(repo)
    assert a not in st.claims and b not in st.claims
    assert "claude-dev" not in st.sessions
    audit = [e for e in _events(repo) if e.data.get("session_retire")][-1]
    assert audit.actor == "human-eli", "recorded under the name of whoever ran it"
    assert audit.data["retired_actor"] == "claude-dev"
    assert audit.data["comms_session_name"] == "auth-refactor"
    assert audit.data["reason"] == "process is gone"


def test_retiring_somebody_who_is_not_here_is_refused_rather_than_reported_done(
        tmp_path, monkeypatch):
    """IF THIS FAILS: a typo'd actor name reads as a completed cleanup.

    "Retired @claude-dv; released 0 claims" is a sentence that stops the
    operator looking, while @claude-dev is still on the roster still holding
    everything it took.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "auth-refactor", "--as", "claude-dev", session="agent-A")
    before = len(_events(repo))
    code, out = _cli(repo, "session", "retire", "claude-dv", "--as", "human-eli",
                     session="agent-B")
    assert code == 1, out
    assert "no active session or claims" in out
    assert len(_events(repo)) == before
    assert "claude-dev" in _state(repo).sessions


# ---------------------------------------------------------------------------
# Lead
# ---------------------------------------------------------------------------


def test_leadership_moves_to_the_named_actor(tmp_path, monkeypatch):
    """IF THIS FAILS: the priority channel is stuck with whoever greeted first.

    The leader's one privilege is posting priority notes and findings. Election
    by "earliest hello" cannot be corrected when the earliest is the one that
    has gone quiet, which is exactly when somebody needs to raise something.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "auth-refactor", "--as", "claude-dev", session="agent-A")
    _cli(repo, "session", "join", "auth-refactor", "--as", "codex-dev", session="agent-B")
    assert _state(repo).sessions["claude-dev"].leader is True

    code, out = _cli(repo, "session", "lead", "codex-dev", "--as", "claude-dev",
                     "--reason", "handing over", session="agent-A")
    assert code == 0, out
    st = _state(repo)
    assert st.sessions["codex-dev"].leader is True
    assert st.sessions["claude-dev"].leader is False, "exactly one leader, never two"

    transfer = [e for e in _events(repo) if e.data.get("leader_transfer")][-1]
    assert transfer.data["leader_actor"] == "codex-dev"
    assert transfer.data["comms_session_name"] == "auth-refactor"


def test_lead_with_no_actor_named_means_take_it(tmp_path, monkeypatch):
    """IF THIS FAILS: an agent cannot pick leadership up off a silent peer.

    `session lead` with no argument is the whole recovery path when the elected
    leader has stopped answering: the one who is still working takes the
    channel. Defaulting to anything other than the caller makes it a no-op that
    reports success.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "auth-refactor", "--as", "claude-dev", session="agent-A")
    _cli(repo, "session", "join", "auth-refactor", "--as", "codex-dev", session="agent-B")

    code, out = _cli(repo, "session", "lead", "--as", "codex-dev", session="agent-B")
    assert code == 0, out
    assert "@codex-dev is now the comms leader." in out
    st = _state(repo)
    assert st.sessions["codex-dev"].leader is True
    assert st.sessions["claude-dev"].leader is False


def test_re_announcing_yourself_does_not_take_leadership_back(tmp_path, monkeypatch):
    """IF THIS FAILS: the board shows two leaders, and the priority rule breaks.

    A hello decides its own `leader` flag. If it ignores the transfer that has
    already happened, the founder — who is still the earliest arrival — flags
    itself leader again on its next join, while the actor leadership was handed
    to keeps its flag too. Two leaders means the leader-only channel is either
    open to both or refused to both, depending on which one a reader finds first.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "auth-refactor", "--as", "claude-dev", session="agent-A")
    _cli(repo, "session", "join", "auth-refactor", "--as", "codex-dev", session="agent-B")
    _cli(repo, "session", "lead", "codex-dev", "--as", "claude-dev", session="agent-A")

    _cli(repo, "session", "join", "auth-refactor", "--as", "claude-dev",
         "--label", "Claude Dev", session="agent-A")
    st = _state(repo)
    leaders = sorted(a for a, s in st.sessions.items() if s.leader)
    assert leaders == ["codex-dev"], leaders


def test_leadership_cannot_be_handed_to_an_actor_nobody_has_heard_from(
        tmp_path, monkeypatch):
    """IF THIS FAILS: the priority channel points at nobody.

    An actor silent past the activity window is presumed gone. Making it leader
    means priority notes are addressed to a dead process and nobody else can
    post them.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "auth-refactor", "--as", "claude-dev", session="agent-A")
    _hello_at(repo, "ghost-dev", datetime.now(UTC) - timedelta(hours=6), base_name="ghost")

    code, out = _cli(repo, "session", "lead", "ghost-dev", "--as", "claude-dev",
                     session="agent-A")
    assert code == 1, out
    assert "not active" in out
    assert not any(e.data.get("leader_transfer") for e in _events(repo))
    assert _state(repo).sessions["claude-dev"].leader is True


def test_lead_is_judged_on_activity_not_on_when_the_greeting_was(tmp_path, monkeypatch):
    """IF THIS FAILS: leadership is refused to the busiest agent in the repo.

    An agent that greeted this morning and has been claiming all afternoon is
    shown active by every other reader in this build. Gating on the one-shot
    hello would refuse it here while the board calls it the most active actor
    on the roster — one screen contradicting the next.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "auth-refactor", "--as", "claude-dev", session="agent-A")
    old = datetime.now(UTC) - timedelta(hours=6)
    _hello_at(repo, "busy-dev", old, base_name="busy",
              comms_session_id=_state(repo).sessions["claude-dev"].session_id,
              comms_session_name="auth-refactor")
    _claim_as_the_wiring_will(repo, "busy-dev", "c.py")  # a minute ago, not six hours

    code, out = _cli(repo, "session", "lead", "busy-dev", "--as", "claude-dev",
                     session="agent-A")
    assert code == 0, out
    assert _state(repo).sessions["busy-dev"].leader is True


# ---------------------------------------------------------------------------
# The stamp — the writing side of the whole mechanic
# ---------------------------------------------------------------------------


def test_stamp_tags_an_event_for_an_actor_that_is_in_a_named_session(tmp_path, monkeypatch):
    """IF THIS FAILS: nothing an agent does belongs to the session it joined.

    `end` releases claims by session id and the archive counts events by session
    id. An untagged claim is one `end` cannot free and one the summary of that
    piece of work never mentions.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "auth-refactor", "--as", "claude-dev", session="agent-A")
    st = _state(repo)

    data: dict = {"intent": "fix the N+1"}
    csession.stamp(st, "claude-dev", data)
    assert data["comms_session_id"] == st.sessions["claude-dev"].session_id
    assert data["comms_session_name"] == "auth-refactor"


def test_stamp_leaves_an_unnamed_actor_and_a_deliberate_tag_alone(tmp_path, monkeypatch):
    """IF THIS FAILS: session-lifecycle events are re-tagged to the wrong window.

    `retire` names the session it is ACTING ON, which is not always the one the
    writer is in — an operator in session B clearing up after an agent in
    session A is the case. A stamp that overwrites would file that event under
    the operator's own work. And an actor in no named session must stay
    untagged: inventing an id would put its events in a window that does not
    exist.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "auth-refactor", "--as", "claude-dev", session="agent-A")
    st = _state(repo)

    deliberate = {"comms_session_id": "01ELSEWHERE", "comms_session_name": "theirs"}
    csession.stamp(st, "claude-dev", deliberate)
    assert deliberate["comms_session_id"] == "01ELSEWHERE"

    stranger: dict = {"intent": "x"}
    csession.stamp(st, "nobody-here", stranger)
    assert stranger == {"intent": "x"}


def test_the_session_hello_keeps_the_identity_the_pre_edit_hook_matches_on(
        tmp_path, monkeypatch):
    """IF THIS FAILS: starting a session blocks you from your own files.

    The pre-edit hook has no COMMS_ACTOR; it identifies the caller by matching
    the host session id against `agent_session` on the hello. This hello
    REPLACES the actor's previous one in the fold, so dropping that key erases
    the pairing and every claim in the repo — the caller's own included — comes
    back as somebody else's.

    It is also what keeps the session alive: claim writes a fresh hello whenever
    the recorded pairing does not match, and that hello carries no session, so
    without this key the next claim would silently drop the agent out of the
    session it just started.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "auth-refactor", "--as", "claude-dev", session="agent-A")
    hellos_before = sum(1 for e in _events(repo) if e.type == "hello")

    code, out = _cli(repo, "claim", "a.py", "--as", "claude-dev", "--intent", "work",
                     session="agent-A")
    assert code == 0, out

    st = _state(repo)
    assert st.sessions["claude-dev"].agent_session == "agent-A"
    assert sum(1 for e in _events(repo) if e.type == "hello") == hellos_before, \
        "claim must not need to re-announce an actor the session hello already paired"
    assert st.sessions["claude-dev"].session_name == "auth-refactor", \
        "and the session must survive the next command"


def test_a_session_hello_carries_the_vendor_so_reviews_stay_independent(
        tmp_path, monkeypatch):
    """IF THIS FAILS: joining a session downgrades every later verification.

    Independence is read off the verifier's hello. This one supersedes the one
    that had the vendor, so dropping it turns "verified by a different family of
    model" into "unknown" — a weaker claim than the truth, made silently.
    """
    repo = _repo(tmp_path, monkeypatch)
    monkeypatch.setenv("COMMS_VENDOR", "anthropic")
    _cli(repo, "session", "start", "auth-refactor", "--as", "claude-dev", session="agent-A")
    monkeypatch.setenv("COMMS_VENDOR", "openai")
    _cli(repo, "session", "join", "auth-refactor", "--as", "codex-dev", session="agent-B")

    st = _state(repo)
    assert st.sessions["claude-dev"].vendor == "anthropic"
    assert st.independence_of("codex-dev", "claude-dev") == "independent"


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


def test_asking_how_the_verb_works_never_writes_anything(tmp_path, monkeypatch):
    """IF THIS FAILS: discovery has side effects.

    These commands are read by agents, and the first thing an agent does with an
    unfamiliar verb is ask it for help. `session end --help` must not end a
    session.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "auth-refactor", "--as", "claude-dev", session="agent-A")
    before = len(_events(repo))
    for args in (("session", "--help"), ("session", "end", "--help"),
                 ("session", "retire", "-h")):
        code, out = _cli(repo, *args, session="agent-A")
        assert code == 0, out
        assert "Usage: comms-graph session" in out
    assert len(_events(repo)) == before


def test_a_name_that_could_forge_a_line_of_output_is_refused(tmp_path, monkeypatch):
    """IF THIS FAILS: a session name can repaint the operator's screen.

    The name is stored raw and printed raw by this command, the board and
    `status`. A newline in it invents a second row on the roster; an ESC can
    rewrite the rows above it. The Go build gets this for free from %q; here it
    has to be explicit.
    """
    repo = _repo(tmp_path, monkeypatch)
    code, out = _cli(repo, "session", "start", "auth\n@root holds everything",
                     "--as", "claude-dev", session="agent-A")
    assert code == 2, out
    assert "control character" in out
    assert _events(repo) == []


def test_an_unknown_subcommand_or_flag_is_refused_not_ignored(tmp_path, monkeypatch):
    """IF THIS FAILS: a mistyped flag reports success having done something else.

    `session end --resaon "..."` would end the session with the default reason
    and exit 0, so the log records why nothing.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "session", "start", "auth-refactor", "--as", "claude-dev", session="agent-A")
    before = len(_events(repo))

    code, out = _cli(repo, "session", "stop", "auth-refactor", "--as", "claude-dev",
                     session="agent-A")
    assert code == 2, out
    assert "unknown session command" in out

    code, out = _cli(repo, "session", "end", "auth-refactor", "--resaon", "x",
                     "--as", "claude-dev", session="agent-A")
    assert code == 2, out
    assert "unknown flag --resaon" in out
    assert len(_events(repo)) == before
