from datetime import datetime, timedelta, timezone

import pytest

from comms_graph.log import Event


def event(id, event_type, actor="sol", scope=None, **data):
    return Event(id=id, ts=datetime(2026, 9, 16, tzinfo=timezone.utc) + timedelta(minutes=int(id)),
                 type=event_type, actor=actor, scope=scope, data=data)


def test_history_pages_old_events_without_duplicates_when_new_work_arrives():
    from comms_graph.board_history import history_page
    events = [event(str(i), "note", body=f"step {i}") for i in range(6)]
    first = history_page(events, limit=2)
    assert [e["body"] for e in first["events"]] == ["step 5", "step 4"]
    events.append(event("6", "note", body="new step"))
    second = history_page(events, before=first["next_before"], limit=2)
    assert [e["body"] for e in second["events"]] == ["step 3", "step 2"]


@pytest.mark.parametrize("refs", [["0"], "0"])
def test_released_holds_stay_in_both_owner_and_task_history(refs):
    from comms_graph.board_history import history_page
    events = [event("0", "claim", scope=["src/a.py"], task="contacts", intent="Contact form"),
              event("1", "release", actor="human", refs=refs, result="Session ended")]
    for filters in ({"actor": "sol"}, {"task": "contacts"}):
        rows = history_page(events, **filters)["events"]
        assert [r["type"] for r in rows] == ["release", "claim"]
        assert rows[0]["actor"] == "human"
        assert rows[0]["original_actor"] == "sol"
        assert rows[0]["scopes"] == ["src/a.py"]


def test_same_agent_claiming_two_different_tasks_does_not_merge_their_history():
    from comms_graph.board_history import history_page
    a = event("0", "claim", scope=["a.py"], task="one", intent="Fix layout")
    b = event("1", "claim", scope=["b.py"], task="two", intent="Fix layout")
    b.ts = a.ts
    assert len(history_page([a, b])["events"]) == 2


def test_task_connections_appear_in_both_tasks_histories():
    from comms_graph.board_history import history_page
    edge = event("0", "task_edge", **{"from": "contacts", "to": "map", "kind": "consumes"})
    for task in ("contacts", "map"):
        rows = history_page([edge], task=task)["events"]
        assert len(rows) == 1
        assert rows[0]["from"] == "contacts"
        assert rows[0]["to"] == "map"
        assert rows[0]["kind"] == "consumes"


def test_multi_owner_release_keeps_every_original_owner():
    from comms_graph.board_history import history_page
    events = [event("0", "claim", scope=["a.py"]),
              event("1", "claim", actor="astra", scope=["b.py"]),
              event("2", "release", actor="human", refs=["0", "1"])]
    row = history_page(events, actor="astra")["events"][0]
    assert row["original_actors"] == ["sol", "astra"]
