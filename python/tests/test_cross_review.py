"""Defects a second reviewer proved after the first review round."""

from __future__ import annotations

import json
from datetime import timedelta

from comms_graph import hooks as chooks
from comms_graph import log as clog
from comms_graph import state as cstate

from .test_session_identity import (
    SESSION_A, SESSION_B, _append, _claims, _edit, _events, _hook, _repo, _run, _start_store)


def test_a_claim_that_lands_while_the_lock_is_out_of_reach_still_blocks(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _start_store(repo)

    def lock_times_out_after_a_peer_claims(*args, **kwargs):
        _append(repo, "peer-agent", "claim", {"intent": "peer got there first",
                                               "agent_session": SESSION_B},
                ago=timedelta(seconds=0), scope=["src/a.py"])
        raise TimeoutError("lock held")

    monkeypatch.setattr(chooks, "_settle_locked", lock_times_out_after_a_peer_claims)
    code, _out, err = _edit(repo, "src/a.py", SESSION_A)
    assert code == 2, err
    assert "peer-agent" in err


def test_a_lock_out_of_reach_alone_does_not_block(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _start_store(repo)

    def lock_times_out(*args, **kwargs):
        raise TimeoutError("lock held")

    monkeypatch.setattr(chooks, "_settle_locked", lock_times_out)
    assert _edit(repo, "src/a.py", SESSION_A)[0] == 0


def test_a_claim_with_no_recorded_session_belongs_to_the_session_that_made_it(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    _append(repo, "dev", "hello", {"agent_session": SESSION_A}, ago=timedelta(minutes=30))
    _append(repo, "dev", "claim", {"intent": "from an older build"},
            ago=timedelta(minutes=29), scope=["src/a.py"])
    _append(repo, "dev", "hello", {"agent_session": SESSION_B}, ago=timedelta(minutes=10))

    held = cstate.fold(_events(repo)).claims
    assert [c.agent_session for c in held.values() if str(c.scope) == "src/a.py"] == [SESSION_A]
    assert _edit(repo, "src/a.py", SESSION_A)[0] == 0
    code, _out, err = _edit(repo, "src/a.py", SESSION_B)
    assert code == 2, "a later session that reused the name does not own the claim"
    assert "@dev" in err


def test_session_end_above_the_repository_releases_what_the_session_claimed_in_it(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    above = repo.parent
    payload = {"session_id": SESSION_A, "cwd": str(above), "tool_name": "Edit",
               "tool_input": {"file_path": str(repo / "src" / "a.py")}}
    assert _run(above, ["hook", "pre-edit"], stdin=json.dumps(payload))[0] == 0
    assert _claims(repo)["src/a.py"].auto

    end = {"session_id": SESSION_A, "cwd": str(above), "hook_event_name": "SessionEnd"}
    assert _run(above, ["hook", "session-end"], stdin=json.dumps(end))[0] == 0
    assert "src/a.py" not in _claims(repo)
    assert "src/b.py" in _claims(repo), "another session's claim stays"
