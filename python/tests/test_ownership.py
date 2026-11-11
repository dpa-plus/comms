"""A claim belongs to the agent session that made it, not to a name that looks like it."""

from __future__ import annotations

import json

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
