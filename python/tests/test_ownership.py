"""A claim belongs to the agent session that made it, not to a name that looks like it."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from comms_graph import log as clog
from comms_graph import state as cstate

from .test_session_identity import (
    SESSION_A, SESSION_B, _claims, _edit, _events, _hook, _repo, _run, _start_store)

TWIN_1 = "4f9a1c00-0000-4000-8000-000000000001"
TWIN_2 = "4f9a1c99-0000-4000-8000-000000000002"


def _edit_env(repo, name, session, env):
    payload = {"session_id": session, "cwd": str(repo), "tool_name": "Edit",
               "tool_input": {"file_path": str(repo / name)}}
    return _run(repo, ["hook", "pre-edit"], stdin=json.dumps(payload), env=env)


def _dirty(repo, name):
    (repo / name).write_text("changed\n")


# ---------------------------------------------------------------------------
# finding 4: two sessions whose ids share a prefix
# ---------------------------------------------------------------------------


def test_sessions_sharing_an_id_prefix_block_each_other(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    assert _edit(repo, "src/a.py", TWIN_1)[0] == 0
    _dirty(repo, "src/a.py")

    code, _, err = _edit(repo, "src/a.py", TWIN_2)
    assert code == 2, "a session with the same first six characters is somebody else"
    assert "BLOCKED" in err

    st = cstate.fold(_events(repo))
    assert st.family_of(TWIN_1).isdisjoint(st.family_of(TWIN_2)), "each gets its own name"


def test_session_end_of_a_prefix_twin_keeps_the_other_twins_claim(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    (repo / "src" / "c.py").write_text("c = 1\n")
    assert _edit(repo, "src/a.py", TWIN_1)[0] == 0
    assert _edit(repo, "src/c.py", TWIN_2)[0] == 0

    assert _hook(repo, "session-end", TWIN_2, reason="exit")[0] == 0
    held = _claims(repo)
    assert "src/c.py" not in held
    assert "src/a.py" in held, "the other session's auto-claim survives"


def test_every_new_claim_records_the_session_that_made_it(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    assert _edit(repo, "src/a.py", SESSION_A)[0] == 0
    code, out, err = _run(repo, ["claim", "src/b2.py", "--as", "named-one"],
                          env={"CLAUDE_CODE_SESSION_ID": SESSION_B})
    assert code == 0, out + err
    sessions = {e.scope[0]: e.data.get("agent_session") for e in _events(repo)
                if e.type == "claim"}
    assert sessions["src/a.py"] == SESSION_A
    assert sessions["src/b2.py"] == SESSION_B


# ---------------------------------------------------------------------------
# finding 8: a shared generic COMMS_ACTOR
# ---------------------------------------------------------------------------


def test_two_sessions_with_one_generic_comms_actor_block_each_other(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    env = {"COMMS_ACTOR": "eli"}
    assert _edit_env(repo, "src/a.py", SESSION_A, env)[0] == 0
    _dirty(repo, "src/a.py")

    code, _, err = _edit_env(repo, "src/a.py", SESSION_B, env)
    assert code == 2, err
    holder = _claims(repo)["src/a.py"]
    assert holder.actor != "eli", "the hook never writes a name the CLI refuses"
    assert holder.actor in cstate.fold(_events(repo)).family_of(SESSION_A)


def test_two_sessions_with_one_specific_comms_actor_still_block_each_other(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    env = {"COMMS_ACTOR": "claude-dev"}
    assert _edit_env(repo, "src/a.py", SESSION_A, env)[0] == 0
    _dirty(repo, "src/a.py")
    assert _edit_env(repo, "src/a.py", SESSION_B, env)[0] == 2


# ---------------------------------------------------------------------------
# finding 5: a subagent's name never takes over its lead
# ---------------------------------------------------------------------------


def _lead_holding_two(repo):
    env = {"CLAUDE_CODE_SESSION_ID": SESSION_A}
    _start_store(repo)
    (repo / "src" / "c.py").write_text("c = 1\n")
    code, out, err = _run(repo, ["claim", "src/a.py", "--intent", "real work"], env=env)
    assert code == 0, out + err
    lead = out.split(" by @", 1)[1].split()[0]
    assert _edit(repo, "src/c.py", SESSION_A)[0] == 0
    return env, lead


def _actorless_name(repo, env):
    code, out, err = _run(repo, ["note", "probe"], env=env)
    assert code == 0, out + err
    return out.split(" by @", 1)[1].split()[0]


def test_a_subagents_hello_does_not_take_over_its_lead(tmp_path):
    repo = _repo(tmp_path)
    env, lead = _lead_holding_two(repo)
    assert _run(repo, ["hello", "--as", "sub-reviewer"], env=env)[0] == 0
    assert _actorless_name(repo, env) == lead
    code, out, _ = _run(repo, ["whoami"], env=env)
    assert out.startswith(f"@{lead} "), out
    assert "sub-reviewer" in cstate.fold(_events(repo)).family_of(SESSION_A)


@pytest.mark.parametrize("how", ["bare", "--all-mine", "--latest", "path", "id"])
def test_release_under_a_subagents_name_frees_nothing_of_its_leads(tmp_path, how):
    repo = _repo(tmp_path)
    env, lead = _lead_holding_two(repo)
    assert _run(repo, ["hello", "--as", "sub-reviewer"], env=env)[0] == 0
    target = {"bare": [], "--all-mine": ["--all-mine"], "--latest": ["--latest"],
              "path": ["src/a.py"], "id": [_claims(repo)["src/a.py"].id]}[how]

    _run(repo, ["release", "--as", "sub-reviewer", *target], env=env)

    held = _claims(repo)
    assert held["src/a.py"].actor == lead, "the lead's claim is still held"
    assert held["src/c.py"].actor == lead, "the lead's auto-claim is still held"
    assert not [e for e in _events(repo) if e.type == "release"]


# ---------------------------------------------------------------------------
# finding 6: status, claims, whoami and release agree on who "you" is
# ---------------------------------------------------------------------------


def _views(repo, env):
    return {verb: _run(repo, [verb], env=env)[1] for verb in ("status", "claims", "whoami")}


def _assert_views_agree(repo, env, me, mine, others):
    views = _views(repo, env)
    assert f"you are @{me}: {mine} claim" in views["status"], views["status"]
    assert (f"@{me} holds {mine}" in views["claims"]) or (
        mine == 0 and f"@{me} holds nothing" in views["claims"]), views["claims"]
    assert views["whoami"].startswith(f"@{me} ") and f"holds {mine}" in views["whoami"]
    for text in views.values():
        for name, count in others.items():
            assert f"@{name} {count}" in text, text
        assert "release --as" in text, text


def test_status_claims_whoami_and_release_count_only_the_callers_name(tmp_path):
    repo = _repo(tmp_path)
    env = {"CLAUDE_CODE_SESSION_ID": SESSION_A}
    for name, path in (("lead-x", "src/a.py"), ("other-name", "src/b.py"),
                       ("other-name", "src/c.py")):
        code, out, err = _run(repo, ["claim", path, "--as", name], env=env)
        assert code == 0, out + err

    _assert_views_agree(repo, env, "lead-x", 1, {"other-name": 2})

    code, out, _ = _run(repo, ["release"], env=env)
    assert code == 0 and out.count("RELEASED") == 1 and "src/a.py" in out
    code, out, _ = _run(repo, ["release"], env=env)
    assert "nothing to release" in out
    assert "@other-name 2" in out and "release --as other-name" in out


def test_a_session_renamed_twice_by_an_older_build_still_agrees_with_release(tmp_path):
    repo = _repo(tmp_path)
    env = {"CLAUDE_CODE_SESSION_ID": SESSION_A}
    log_file = clog.log_path(repo)
    log_file.parent.mkdir(parents=True)
    t0 = datetime.now(timezone.utc) - timedelta(minutes=30)
    events = []
    for i, name in enumerate(("first-one", "second-one", "third-one")):
        data = {"agent_session": SESSION_A, **({"primary": True} if i else {})}
        events.append(clog.Event(ts=t0 + timedelta(minutes=i), id=clog.new_id(),
                                 actor=name, type="hello", scope=None, data=data))
    for i, (name, path) in enumerate((("second-one", "src/a.py"), ("third-one", "src/b.py"))):
        events.append(clog.Event(ts=t0 + timedelta(minutes=5 + i), id=clog.new_id(),
                                 actor=name, type="claim", scope=[path], data={"intent": "x"}))
    clog.append_batch(log_file, events)

    _assert_views_agree(repo, env, "first-one", 0, {"second-one": 1, "third-one": 1})
    code, out, _ = _run(repo, ["release"], env=env)
    assert "nothing to release" in out and "release --as" in out
    assert len(_claims(repo)) == 2


# ---------------------------------------------------------------------------
# finding 7: a batch claim follows the single-claim rule and writes nothing on failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scopes", [["src/a.py"], ["src/a.py", "src/b.py"]])
def test_a_claim_is_not_blocked_by_its_sessions_claim_under_another_name(tmp_path, scopes):
    repo = _repo(tmp_path)
    env = {"CLAUDE_CODE_SESSION_ID": SESSION_A}
    code, out, err = _run(repo, ["claim", "src/a.py", "--as", "lead-x"], env=env)
    assert code == 0, out + err
    code, out, err = _run(repo, ["claim", *scopes, "--as", "sub-y", "--intent", "x"], env=env)
    assert code == 0, out + err
    assert _run(repo, ["claim", "src/a.py", "--as", "sub-y"], env={
        "CLAUDE_CODE_SESSION_ID": SESSION_B})[0] == 1, "another session is still blocked"


def test_a_failed_batch_claim_writes_no_release(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    env = {"CLAUDE_CODE_SESSION_ID": SESSION_A}
    assert _edit(repo, "src/a.py", SESSION_A)[0] == 0
    before = [e.id for e in _events(repo) if e.type in ("claim", "release")]

    code, out, err = _run(repo, ["claim", "src/a.py", "src/b.py", "--as", "lead-x",
                                 "--intent", "both"], env=env)
    assert code == 1, out + err
    after = [e.id for e in _events(repo) if e.type in ("claim", "release")]
    assert after == before, "a refused batch changes no claim"
    assert _claims(repo)["src/a.py"].auto


def test_a_batch_claim_takes_over_its_sessions_auto_claim(tmp_path):
    repo = _repo(tmp_path)
    env = {"CLAUDE_CODE_SESSION_ID": SESSION_A}
    _run(repo, ["note", "hi", "--as", "other-agent"], env={"CLAUDE_CODE_SESSION_ID": "x"})
    assert _edit(repo, "src/a.py", SESSION_A)[0] == 0
    code, out, err = _run(repo, ["claim", "src/a.py", "src/b.py", "--as", "lead-x",
                                 "--intent", "both"], env=env)
    assert code == 0, out + err
    held = _claims(repo)
    assert held["src/a.py"].actor == "lead-x" and not held["src/a.py"].auto
