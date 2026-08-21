"""The event log is the only durable record of who is doing what.

Everything else in comms — the board, the state fold, the map projection — is a
view that can be rebuilt. The log cannot. So the properties tested here are all
of one kind: a line that reaches disk must mean tomorrow what it meant when it
was written, and a bad line must never take the good ones down with it.

Each test says what breaks in the real world if it fails.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from comms_graph import log as clog
from comms_graph.log import CorruptLogError, Event

UTC = timezone.utc
T0 = datetime(2026, 5, 22, 14, 30, tzinfo=UTC)


def ev(**kw) -> Event:
    base = dict(ts=T0, id=clog.new_id(T0), actor="claude-3a1f", type="note", data={"body": "hi"})
    base.update(kw)
    return Event(**base)


# ---------------------------------------------------------------------------
# Timestamps: the log stores instants, not wall clocks
# ---------------------------------------------------------------------------


def test_a_naive_timestamp_is_refused_rather_than_read_as_utc():
    """IF THIS FAILS: every agent that reaches for `datetime.now()` (the naive,
    local one — the obvious spelling) writes events shifted by its machine's UTC
    offset. Nothing errors and no line is malformed; claims just expire hours
    early or late and events from two machines interleave in the wrong causal
    order. A silent hours-wide skew in coordination state is far worse than a
    loud error at the call site that got it wrong."""
    naive = datetime(2026, 5, 22, 14, 30)  # what datetime.now() hands you
    with pytest.raises(ValueError):
        ev(ts=naive).encode()
    with pytest.raises(ValueError):
        clog.new_id(naive)


def test_the_zero_instant_is_never_written():
    """IF THIS FAILS: an accidental zero/default timestamp appends a line that
    this very module's reader classifies as corrupt — so one bad event bricks
    the whole log from that point on, including every good claim appended after
    it. A writer must never author bytes its own reader would refuse."""
    for spelling in (
        datetime(1, 1, 1, tzinfo=UTC),
        datetime(1, 1, 1, 1, 0, tzinfo=timezone(timedelta(hours=1))),  # same instant
    ):
        with pytest.raises(ValueError):
            ev(ts=spelling).encode()


def test_a_timestamp_without_a_zone_is_not_guessed_at_on_the_way_in(tmp_path):
    """IF THIS FAILS: a line from some other writer that omitted its offset gets
    read as if it were UTC, inventing an instant the file never stated — and the
    invented instant decides which of two claims came first."""
    p = tmp_path / "log.jsonl"
    p.write_text('{"ts":"2026-05-22T14:30:00","id":"1","actor":"a","type":"note"}\n')
    with pytest.raises(CorruptLogError):
        clog.read(p)


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_what_we_write_is_what_we_read_back(tmp_path):
    """IF THIS FAILS: the intent, the refs and the scope an agent recorded are
    not what the next agent sees. The log is the hand-off medium between agents
    that never talk directly, so a lossy round trip corrupts the hand-off."""
    p = tmp_path / "log.jsonl"
    original = Event(
        ts=T0,
        id=clog.new_id(T0),
        actor="claude-3a1f",
        type="claim",
        scope=["src/api/server.ts#charge"],
        data={
            "intent": "fix N+1 in login->dashboard & billing",  # < > & are Go-escaped
            "refs": ["01ABC", "01DEF"],
            "priority": True,
            "tier": 2,
            "unicode": "Café / файл / 日本語",
        },
    )
    clog.append(p, original)
    (back,) = clog.read(p)
    assert back.ts == original.ts
    assert back.id == original.id
    assert back.actor == original.actor
    assert back.type == original.type
    assert back.scope == original.scope
    assert back.data == original.data


def test_a_line_stays_one_line_whatever_the_body_says(tmp_path):
    """IF THIS FAILS: a note or intent containing U+2028/U+2029 tears one event
    into two for any consumer that splits on Unicode line boundaries (Python's
    own str.splitlines() does). Half an event is not a smaller event, it is a
    parse error that stops the log."""
    p = tmp_path / "log.jsonl"
    body = "before\u2028after\u2029end"  # LINE and PARAGRAPH SEPARATOR
    clog.append(p, ev(data={"body": body}))
    text = p.read_text(encoding="utf-8")
    assert len(text.splitlines()) == 1
    (back,) = clog.read(p)
    assert back.data["body"] == body


def test_the_same_event_encodes_to_the_same_bytes_every_time():
    """IF THIS FAILS: two writers (this port and the Go `comms` binary append to
    the SAME file) produce byte-different spellings of one event, so any dedupe,
    diff or checksum over raw lines sees two events where there is one."""
    a = Event(ts=T0, id="X", actor="a", type="note", data={"b": 1, "a": 2, "c": {"z": 1, "y": 2}})
    b = Event(ts=T0, id="X", actor="a", type="note", data={"c": {"y": 2, "z": 1}, "a": 2, "b": 1})
    assert a.encode() == b.encode()


# ---------------------------------------------------------------------------
# Surviving bad input
# ---------------------------------------------------------------------------


def test_an_event_type_a_newer_build_invented_does_not_brick_this_one(tmp_path, capsys):
    """IF THIS FAILS: the day a newer comms adds a tenth event type, every older
    install on the machine stops being able to read that repo's log at all —
    status, claim, note and the pre-edit hook fail together, on a log that is
    perfectly well formed. Readers must be tolerant of what they do not know."""
    p = tmp_path / "log.jsonl"
    clog.append(p, ev(id="A"))
    with open(p, "ab") as fh:
        fh.write(b'{"ts":"2026-05-22T14:31:00Z","id":"B","actor":"x","type":"prophecy"}\n')
    clog.append(p, ev(id="C", ts=T0 + timedelta(minutes=2)))

    got = clog.read(p)
    assert [e.id for e in got] == ["A", "C"]
    # ...and it says so once, so an operator learns their build is behind.
    assert "skipped" in capsys.readouterr().err


def test_a_writer_still_refuses_the_type_a_reader_tolerates():
    """IF THIS FAILS: a build authors events it cannot itself fold, so its own
    board silently omits work it recorded. Tolerance is for reading other
    people's future, never for writing your own."""
    with pytest.raises(ValueError):
        ev(type="prophecy").encode()


@pytest.mark.parametrize(
    "bad,why",
    [
        ('{"ts":"2026-05-22T14:31:00Z","id":"B","actor":"x",', "truncated json"),
        ('{"ts":"","id":"B","actor":"x","type":"note"}', "no ts"),
        ('{"ts":"0001-01-01T00:00:00Z","id":"B","actor":"x","type":"note"}', "zero ts"),
        ('{"ts":"2026-05-22T14:31:00Z","actor":"x","type":"note"}', "no id"),
        ('{"ts":"2026-05-22T14:31:00Z","id":"B","type":"note"}', "no actor"),
        ('{"ts":"2026-05-22T14:31:00Z","id":"B","actor":"x","type":"claim","scope":"a.ts"}',
         "scope is not an array"),
        ('{"ts":"2026-05-22T14:31:00Z","id":"B","actor":"x","type":"note","data":[1]}',
         "data is not an object"),
    ],
)
def test_a_line_we_cannot_parse_is_an_error_naming_its_line_number(tmp_path, bad, why):
    """IF THIS FAILS: a log that is genuinely damaged is read as a shorter, valid
    log — and an agent then acts on a picture of the repo that is missing claims
    it must respect. Refusing loudly, at a line number a human can go and look
    at, is the only safe direction to fail in. (Contrast the unknown-type case
    above: a newer writer is not a broken file.)"""
    p = tmp_path / "log.jsonl"
    with open(p, "wb") as fh:
        fh.write(ev(id="A").encode())
        fh.write(bad.encode("utf-8") + b"\n")
        fh.write(ev(id="C", ts=T0 + timedelta(minutes=2)).encode())
    with pytest.raises(CorruptLogError) as exc:
        clog.read(p)
    assert exc.value.line == 2, why


def test_a_torn_final_line_does_not_cost_the_events_before_it(tmp_path):
    """IF THIS FAILS: an agent killed mid-append (SIGKILL, a laptop lid, a full
    disk) destroys every claim in the repo's history, not just its own last
    write. The events before the tear are intact and must still be readable."""
    p = tmp_path / "log.jsonl"
    clog.append(p, ev(id="A"))
    clog.append(p, ev(id="B", ts=T0 + timedelta(minutes=1)))
    with open(p, "ab") as fh:
        fh.write(b'{"ts":"2026-05-22T14:33:00Z","id":"C","act')  # no newline: torn
    assert [e.id for e in clog.read(p)] == ["A", "B"]


def test_a_line_appended_twice_is_still_one_event(tmp_path):
    """IF THIS FAILS: a retried write, a log concatenated onto itself, or a
    copy-paste during recovery replays a claim AFTER its own release — so a
    scope nobody holds blocks everybody — and double-counts every finding."""
    p = tmp_path / "log.jsonl"
    line = ev(id="DUPLICATE").encode()
    with open(p, "wb") as fh:
        fh.write(line)
        fh.write(line)
    assert [e.id for e in clog.read(p)] == ["DUPLICATE"]


def test_a_runaway_line_is_refused_before_it_is_materialised(tmp_path):
    """IF THIS FAILS: one absurd line (a stuck writer, a binary file dropped in
    place) is read into memory in full during every replay — and replay happens
    on the pre-edit hot path, in front of every agent write."""
    p = tmp_path / "log.jsonl"
    with open(p, "wb") as fh:
        fh.write(b'{"ts":"2026-05-22T14:31:00Z","id":"B","actor":"x","type":"note","data":{"b":"')
        fh.write(b"x" * (clog.MAX_LINE_BYTES + 16))
        fh.write(b'"}}\n')
    with pytest.raises(CorruptLogError):
        clog.read(p)


def test_the_writer_refuses_a_line_its_own_reader_would_call_corrupt(tmp_path):
    """IF THIS FAILS: an oversized event is accepted by the writer and then
    rejected by the reader, so the log is bricked the instant it lands and the
    line that did it is already on disk. Every later read of that repo aborts —
    board, log, claim, and the pre-edit hook — and the store cannot be opened to
    find out why. The two limits must be one limit."""
    p = tmp_path / "log.jsonl"
    with pytest.raises(ValueError, match="line limit"):
        clog.append(p, ev(id="HUGE", data={"body": "x" * (clog.MAX_LINE_BYTES + 64)}))
    assert not p.exists(), "a refused event must not leave a partial log behind"
    clog.append(p, ev(id="FINE"))
    assert [e.id for e in clog.read(p)] == ["FINE"]


def test_appending_after_a_torn_line_does_not_fuse_with_it(tmp_path):
    """IF THIS FAILS: a crash mid-append is survivable until the NEXT command
    runs, and then it is not. Appending after an unterminated line concatenates
    onto it, so the fragment and the new event become one unparseable line in
    the MIDDLE of the log — no longer the tolerated trailing-garbage case, but
    permanent corruption that costs the entire history. One crash plus one
    ordinary later claim was enough to lose the repo's whole log."""
    p = tmp_path / "log.jsonl"
    clog.append(p, ev(id="A"))
    with open(p, "ab") as fh:
        fh.write(b'{"ts":"2026-05-22T14:33:00Z","id":"TORN","act')  # crash here
    clog.append(p, ev(id="B", ts=T0 + timedelta(minutes=2)))
    assert [e.id for e in clog.read(p)] == ["A", "B"]


def test_a_log_that_is_nothing_but_a_torn_line_still_accepts_writes(tmp_path):
    """IF THIS FAILS: a crash during the very FIRST append leaves a file with no
    newline anywhere in it, and the repair has no boundary to scan back to. That
    repo can never be written to again — the failure lands on a brand-new store,
    where it looks like comms is simply broken."""
    p = tmp_path / "log.jsonl"
    p.write_bytes(b'{"ts":"2026-05-22T14:30:00Z","id":"TORN","ty')
    clog.append(p, ev(id="FIRST"))
    assert [e.id for e in clog.read(p)] == ["FIRST"]


def test_a_torn_line_longer_than_one_read_chunk_is_still_found(tmp_path):
    """IF THIS FAILS: the backward scan for the last newline gives up after one
    chunk, so a large interrupted write (a big batch, a long note) is treated as
    having no boundary and takes the good events before it with it."""
    p = tmp_path / "log.jsonl"
    clog.append(p, ev(id="KEEP"))
    with open(p, "ab") as fh:
        fh.write(b'{"junk":"' + b"y" * (clog._READ_CHUNK * 3))
    clog.append(p, ev(id="AFTER", ts=T0 + timedelta(minutes=3)))
    assert [e.id for e in clog.read(p)] == ["KEEP", "AFTER"]


def test_a_repaired_torn_line_says_so_on_stderr(tmp_path, capsys):
    """IF THIS FAILS: bytes disappear from the append-only log silently. The
    fragment is safe to drop — nothing was ever told it succeeded — but dropping
    it quietly means a crash leaves no trace anyone can find afterwards."""
    p = tmp_path / "log.jsonl"
    clog.append(p, ev(id="A"))
    with open(p, "ab") as fh:
        fh.write(b'{"ts":"2026-05-22T14:33:00Z","id":"TORN","act')
    capsys.readouterr()
    clog.append(p, ev(id="B", ts=T0 + timedelta(minutes=2)))
    assert "unterminated final line" in capsys.readouterr().err


def test_a_missing_log_is_not_an_error(tmp_path):
    """IF THIS FAILS: the very first command in a fresh repo fails instead of
    reporting an empty board. Nobody having claimed anything yet is the normal
    starting state, not a fault."""
    assert clog.read(tmp_path / "never-written.jsonl") == []


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def test_one_bad_event_in_a_batch_writes_nothing_at_all(tmp_path):
    """IF THIS FAILS: a command that emits several events (a claim plus its note,
    a release plus its finding) can land half-written and stay that way forever —
    the log is append-only, so there is no repair. All-or-nothing is what lets a
    caller retry after an error instead of reconciling a partial record."""
    p = tmp_path / "log.jsonl"
    clog.append(p, ev(id="GOOD"))
    before = p.read_bytes()

    with pytest.raises(ValueError):
        clog.append_batch(p, [ev(id="A"), ev(id="B", type="prophecy"), ev(id="C")])

    assert p.read_bytes() == before
    assert [e.id for e in clog.read(p)] == ["GOOD"]


def test_a_batch_lands_as_whole_consecutive_lines(tmp_path):
    """IF THIS FAILS: a batch bigger than a stream buffer flushes mid-object and
    tears a JSON line in half. A torn line is not a lost event — it is a log that
    stops parsing there, taking every later event with it."""
    p = tmp_path / "log.jsonl"
    big = "x" * 20_000
    batch = [ev(id=f"E{i:04d}", ts=T0 + timedelta(seconds=i), data={"body": big}) for i in range(40)]
    clog.append_batch(p, batch)
    got = clog.read(p)
    assert [e.id for e in got] == [e.id for e in batch]


def test_the_log_is_not_readable_by_other_users(tmp_path):
    """IF THIS FAILS: on a shared host, every other account can read which files
    each agent is editing and why. The store is per-user machine state, not
    shared data."""
    p = tmp_path / "log.jsonl"
    clog.append(p, ev())
    assert os.stat(p).st_mode & 0o077 == 0


# ---------------------------------------------------------------------------
# Ids
# ---------------------------------------------------------------------------


def test_ids_minted_in_one_millisecond_still_sort_in_the_order_they_were_minted():
    """IF THIS FAILS: two events written in the same millisecond — a claim and
    the steal that displaces it is exactly that fast — sort in random order, so
    the reducer can replay the steal before the claim it steals from and leave
    two holders (or none) on one scope."""
    ids = [clog.new_id(T0) for _ in range(500)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    assert all(len(i) == 26 for i in ids)


# ---------------------------------------------------------------------------
# Where the log lives
# ---------------------------------------------------------------------------


def test_the_log_does_not_live_inside_the_repo(tmp_path):
    """IF THIS FAILS: coordination truth sits next to a derived artifact that is
    rewritten whole on every update and deleted outright by `graphify uninstall
    --purge` — so removing the map silently destroys the record of who claimed
    what. The map is derived; the log is truth; they must not share a fate."""
    repo = tmp_path / "repo"
    repo.mkdir()
    p = clog.log_path(repo)
    assert repo not in p.parents
    assert clog.store_dir(repo) in p.parents


def test_two_checkouts_do_not_share_a_log(tmp_path):
    """IF THIS FAILS: unrelated projects coordinate against each other — agent A
    in project one is blocked by agent B's claim on a same-named file in project
    two."""
    a, b = tmp_path / "one", tmp_path / "two"
    a.mkdir()
    b.mkdir()
    assert clog.log_path(a) != clog.log_path(b)


def test_every_worktree_of_one_repo_shares_one_log(tmp_path):
    """IF THIS FAILS: the single most likely deployment silently coordinates
    nothing. Worktree-per-agent is the standard way to run several coding agents
    on one repository (it is this harness's own default), and keying the store by
    the checkout path gives each worktree its own empty log: no blocks, no
    warnings, two calm boards and two agents editing the same function."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not available")
    main = tmp_path / "repo"
    main.mkdir()
    run = lambda *a: subprocess.run(
        [git, *a], cwd=main, capture_output=True, text=True, timeout=60, check=True
    )
    run("init", "-b", "main")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", "root")
    wt = tmp_path / "agent-two"
    run("worktree", "add", "-b", "agent-two", str(wt))
    assert wt.is_dir()

    assert clog.log_path(wt) == clog.log_path(main)


def test_a_store_under_a_temp_root_is_flagged_as_private(tmp_path):
    """IF THIS FAILS: a run with an overridden $HOME writes to a throwaway store
    that a normally-launched agent never opens — two agents coordinating through
    logs that cannot see each other, with no error anywhere. The caller can only
    warn about that if this reports it."""
    assert clog.is_ephemeral_store("/tmp/whatever/comms/abc") is True
    assert clog.is_ephemeral_store("/private/var/folders/9x/T/comms/abc") is True
    assert clog.is_ephemeral_store("/Users/someone/Library/Application Support/comms/abc") is False


def test_the_year_is_four_digits_on_every_platform():
    """strftime("%Y") is not portable: glibc renders year 1 as "1", BSD as "0001".

    This is the invariant behind test_the_zero_instant_is_never_written, pinned
    directly so it fails on the machine that has the bug rather than only on the
    one platform where the symptom shows. RFC3339 requires four digits, and the
    zero-instant guard compares the rendered string, so an unpadded year both
    breaks the format and slips past the guard.
    """
    from comms_graph.log import _format_ts

    assert _format_ts(datetime(1, 1, 1, tzinfo=UTC)) == "0001-01-01T00:00:00Z"
    assert _format_ts(datetime(999, 12, 31, 23, 59, 59, tzinfo=UTC)) == "0999-12-31T23:59:59Z"
