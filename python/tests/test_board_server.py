"""The live board: the surface a person leaves open while agents work.

Two things matter here and nothing else does. It must never go blank because one
piece of data could not be read — a board that disappears when something is wrong
is worse than one that says what is wrong. And it must be read-only: the log is
written under a lock, through a fold that enforces the rules, and a dashboard
that could mutate it would be a second writer with none of those guarantees.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from comms_graph import log as clog
from comms_graph import server as cserver


@pytest.fixture()
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("x = 1\n")
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    subprocess.run([git, "init", "-q", "."], cwd=repo, check=True)

    log_file = clog.log_path(repo)
    httpd = cserver.serve(repo, log_file, port=0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield repo, log_file, f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8")


def test_the_board_serves_before_anything_has_ever_happened(board):
    """IF THIS FAILS: the board is broken exactly when somebody first opens it.
    A brand-new repo with no log at all is the normal first experience."""
    repo, log_file, base = board
    status, body = _get(base + "/")
    assert status == 200
    assert "comms" in body

    status, body = _get(base + "/api/status")
    data = json.loads(body)
    assert data["counts"]["claims"] == 0
    assert "error" not in data


def test_the_page_ships_usable_css_and_script(board):
    """IF THIS FAILS: the page renders as unstyled text with dead JavaScript.

    This happened: the template was written with doubled braces for a .format()
    call that never happened, so `{{` and `}}` shipped literally and every CSS
    rule and script block was invalid. Nothing errored — it just looked wrong.
    """
    repo, log_file, base = board
    _, body = _get(base + "/")
    assert "{{" not in body and "}}" not in body
    assert "grid-template-columns" in body
    assert "EventSource" in body


def test_an_unreadable_log_is_reported_rather_than_blanking_the_board(board):
    """A board that vanishes when something is wrong tells you nothing. One
    that says what is wrong tells you where to look."""
    repo, log_file, base = board
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_bytes(b'{"this": "is not a valid event line"}\n')
    status, body = _get(base + "/api/status")
    assert status == 200
    data = json.loads(body)
    assert "error" in data or data["counts"]["claims"] == 0


def test_a_missing_code_map_says_how_to_build_one(board):
    """The map is optional — claims record and block without it — so its absence
    is a normal state that must read as a next step, not as a failure."""
    repo, log_file, base = board
    status, body = _get(base + "/map.html")
    assert status == 200
    assert "graphify extract" in body


def test_the_task_frame_renders_with_no_tasks(board):
    repo, log_file, base = board
    status, body = _get(base + "/tasks.html")
    assert status == 200
    assert "Nothing declared yet" in body or "No tasks declared yet" in body


def test_the_board_has_no_way_to_change_anything(board):
    """IF THIS FAILS: the board becomes a second writer. Everything that changes
    the log goes through the CLI, where the per-repo lock and the fold enforce
    the rules; an endpoint here would have neither."""
    repo, log_file, base = board
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        req = urllib.request.Request(base + "/api/status", method=method, data=b"{}")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                assert r.status >= 400, f"{method} was accepted"
        except urllib.error.HTTPError as exc:
            assert exc.code >= 400
        except urllib.error.URLError:
            pass  # refusing to speak to it at all is also fine


def test_the_stamp_changes_only_when_the_log_does(board):
    """The graphs are expensive to redraw and hold the reader's pan and zoom, so
    the page reloads them on this signal and nothing else. If it changed on a
    timer, the view would be yanked out from under whoever was reading it."""
    repo, log_file, base = board
    b = cserver.Board(repo, log_file)
    first = b.stamp()
    assert b.stamp() == first, "the stamp must be stable while nothing happens"

    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "ab") as fh:
        fh.write(b"\n")
    assert b.stamp() != first, "an append must move the stamp"


def test_concurrent_requests_never_serve_an_empty_page(board):
    """IF THIS FAILS: a frame goes blank with HTTP 200 and nothing in any log.

    Building a page is not side-effect free — it renders to a file and reads it
    back. Two requests missing the cache together (a browser fetching both
    frames, or a reload landing on top of a push) had both builders writing the
    same path at once, and one read it half-written.
    """
    import threading
    import urllib.request

    repo, log_file, base = board
    results = []

    def hit(path):
        try:
            with urllib.request.urlopen(base + path, timeout=30) as r:
                results.append((path, r.status, len(r.read())))
        except Exception as exc:  # noqa: BLE001 - recorded, asserted below
            results.append((path, type(exc).__name__, 0))

    paths = ["/map.html", "/tasks.html"] * 4
    threads = [threading.Thread(target=hit, args=(p,)) for p in paths]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == len(paths)
    for path, status, size in results:
        assert status == 200, f"{path} returned {status}"
        assert size > 0, f"{path} served an empty body"


def _at(seq, typ, actor, minutes_ago, data=None, scope=None):
    """An event stamped `minutes_ago` in the past."""
    from datetime import datetime, timedelta, timezone
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return clog.Event(ts=ts, id=clog.new_id(ts), actor=actor, type=typ,
                      scope=scope, data=data or {})


def test_a_claim_is_quiet_because_its_HOLDER_went_silent(board, tmp_path):
    """IF THIS FAILS: a legitimate long task reads as abandoned.

    A claim's timestamp is when the ground was TAKEN, never when it was last
    touched, so ageing claims out by their own age calls a three-hour task
    dead. What says nobody is coming back is the HOLDER going silent.
    """
    from comms_graph import server as cserver

    repo, log_file, base = board
    log_file.parent.mkdir(parents=True, exist_ok=True)
    events = [
        # alice took ground three hours ago and is still talking
        _at(0, "hello", "alice", 180, {"agent_session": "sA"}),
        _at(0, "claim", "alice", 180, {"intent": "a long one"}, scope=["src/a.py"]),
        _at(0, "hello", "alice", 2, {"agent_session": "sA"}),
        # bob took ground three hours ago and has not been heard since
        _at(0, "hello", "bob", 180, {"agent_session": "sB"}),
        _at(0, "claim", "bob", 180, {"intent": "then crashed"}, scope=["src/b.py"]),
    ]
    events.sort(key=lambda e: e.ts)
    with open(log_file, "wb") as fh:
        for e in events:
            fh.write(e.encode())

    snap = cserver._snapshot(repo, log_file)
    by_actor = {c["actor"]: c for c in snap["claims"]}
    assert by_actor["alice"]["quiet"] is False, (
        "a three-hour task by an agent that is still talking read as abandoned: "
        + repr(by_actor["alice"])
    )
    assert by_actor["bob"]["quiet"] is True, repr(by_actor["bob"])

    kinds = {a["kind"] for a in snap["alerts"]}
    assert "quiet" in kinds, snap["alerts"]
    quiet = next(a for a in snap["alerts"] if a["kind"] == "quiet")
    assert "@bob" in quiet["text"] and "@alice" not in quiet["text"], quiet


def test_the_board_names_a_dependency_loop(board):
    """A cycle is the one state a plan cannot recover from on its own, and it
    was computed inside the task-graph page's side panel — which the board
    hides to give the drawing room. So it was invisible where people watch."""
    from comms_graph import server as cserver

    repo, log_file, base = board
    log_file.parent.mkdir(parents=True, exist_ok=True)
    events = [
        _at(0, "task", "p", 5, {"task": "x", "title": "X"}),
        _at(0, "task", "p", 5, {"task": "y", "title": "Y"}),
        _at(0, "task_edge", "p", 4, {"from": "x", "to": "y", "kind": "interface"}),
        _at(0, "task_edge", "p", 3, {"from": "y", "to": "x", "kind": "interface"}),
        _at(0, "task_edge", "p", 2, {"from": "x", "to": "ghost", "kind": "sequence"}),
    ]
    events.sort(key=lambda e: e.ts)
    with open(log_file, "wb") as fh:
        for e in events:
            fh.write(e.encode())

    snap = cserver._snapshot(repo, log_file)
    kinds = {a["kind"] for a in snap["alerts"]}
    assert "cycle" in kinds, snap["alerts"]
    assert "dangling" in kinds, snap["alerts"]
    cyc = next(a for a in snap["alerts"] if a["kind"] == "cycle")
    assert "x" in cyc["text"] and "y" in cyc["text"], cyc


def test_a_quiet_board_says_so_rather_than_showing_nothing(board):
    """No alerts must be a statement, not an absence — the reader has to be able
    to tell "nothing is wrong" from "the board did not load"."""
    from comms_graph import server as cserver

    repo, log_file, base = board
    snap = cserver._snapshot(repo, log_file)
    assert snap["alerts"] == [], snap["alerts"]
    assert "error" not in snap


def test_who_is_here_lists_everyone_who_acted_not_only_who_said_hello(board):
    """IF THIS FAILS: the board reports an empty room to somebody watching
    agents work in it.

    A hello is only written when the host reports a session id, which in
    practice means Claude Code. An agent on any other harness could claim,
    release and get refused all day while the panel said "Nobody has said hello
    in this repo".
    """
    from comms_graph import server as cserver

    repo, log_file, base = board
    log_file.parent.mkdir(parents=True, exist_ok=True)
    events = [
        _at(0, "hello", "claude-dev", 10, {"agent_session": "sA", "vendor": "anthropic"}),
        _at(0, "claim", "claude-dev", 9, {"intent": "a"}, scope=["src/a.py"]),
        # no hello at all — a harness that reports no session id
        _at(0, "claim", "codex-dev", 8, {"intent": "b"}, scope=["src/b.py"]),
    ]
    events.sort(key=lambda e: e.ts)
    with open(log_file, "wb") as fh:
        for e in events:
            fh.write(e.encode())

    snap = cserver._snapshot(repo, log_file)
    by_actor = {r["actor"]: r for r in snap["roster"]}
    assert set(by_actor) == {"claude-dev", "codex-dev"}, snap["roster"]
    assert by_actor["codex-dev"]["holding"] == 1
    assert by_actor["codex-dev"]["last_seen"], by_actor["codex-dev"]

    # a hello is still what ties an actor to a process, so the board says which
    assert by_actor["claude-dev"]["identified"] is True
    assert by_actor["codex-dev"]["identified"] is False


def test_the_page_script_actually_parses(board):
    """IF THIS FAILS: the board renders as an empty shell with two graphs in it.

    This has now happened twice, both times silently. First a template written
    with doubled braces for a .format() that never ran, so every CSS rule was
    invalid. Then an escaped quote inside a triple-quoted Python string —
    `\\"..\\"` became `".."`, which closed the JS string early and killed the
    whole <script>. Nothing errors: the iframes still load, the header still
    draws, and the rail is simply never filled in.

    Checking for markers cannot catch this; only parsing can.
    """
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; cannot parse the page script")

    repo, log_file, base = board
    _, body = _get(base + "/")
    assert "<script>" in body, "the page lost its script block entirely"
    js = body.split("<script>", 1)[1].split("</script>", 1)[0]

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(js)
        path = fh.name
    out = subprocess.run([node, "--check", path], capture_output=True, text=True)
    assert out.returncode == 0, "the served page script does not parse:\n" + out.stderr


def test_the_page_draws_every_array_the_snapshot_carries(board):
    """IF THIS FAILS: data is collected on every push and rendered nowhere.

    Every one of these was in the payload and undrawn at some point — findings
    for the whole life of the board, and the feed, the claim id and the
    verification evidence until the page was rebuilt around them.
    """
    repo, log_file, base = board
    _, body = _get(base + "/")
    for name in ("alerts", "claims", "roster", "tasks", "feed", "findings", "notes", "projects"):
        assert "D." + name in body or "d." + name in body, f"the page never reads {name}"
    # the reported checks reach the page on the EVENT that reported them
    for token in ("quiet", "identified", "e.checks", "c.id", "band"):
        assert token in body, f"the page never reads {token}"


def test_the_graphs_cost_nothing_until_somebody_asks_for_them(board):
    """IF THIS FAILS: every page load pays for two vis-network layouts.

    The board used to mount both graphs permanently, side by side, so each got
    half a window and the activity — the one thing with content all day — got
    whatever was left. Now the stream is the page and both graphs live behind
    one overlay, loaded on first open.
    """
    repo, log_file, base = board
    _, body = _get(base + "/")
    assert 'id="dagWrap"' in body
    assert body.count('id="dagFrame"') == 1
    frame = body[body.index('id="dagFrame"'):][:140]
    assert "src=" not in frame, ("a graph loads on every page view: " + frame)
    assert "/tasks.html" in body and "/map.html" in body
    assert 'id="gTasks"' in body and 'id="gMap"' in body



def test_an_agent_with_no_hello_is_not_declared_abandoned_while_it_works(board):
    """IF THIS FAILS: the board tells you to take ground off a working agent.

    A hello only exists when the host reports a session id, which in practice
    means Claude Code. `quiet` was computed from hellos, so an agent on any
    other harness went quiet an hour after taking ground no matter how hard it
    was working — and the board contradicted itself on one screen: "last seen
    10 seconds ago" in the roster beside "idle 3 hours" on the claim, with an
    alert recommending the reader free it.
    """
    from comms_graph import server as cserver

    repo, log_file, base = board
    log_file.parent.mkdir(parents=True, exist_ok=True)
    events = [
        # amy runs under a harness that reports a session; zoe does not
        _at(0, "hello", "amy", 200, {"agent_session": "sess-amy"}),
        _at(0, "claim", "amy", 190, {"intent": "b"}, scope=["src/b.py"]),
        _at(0, "claim", "zoe", 190, {"intent": "a"}, scope=["src/a.py"]),
        # both are working RIGHT NOW
        _at(0, "note", "amy", 0, {"body": "still going"}),
        _at(0, "note", "zoe", 0, {"body": "still going, 40 files to go"}),
    ]
    events.sort(key=lambda e: e.ts)
    with open(log_file, "wb") as fh:
        for e in events:
            fh.write(e.encode())

    snap = cserver._snapshot(repo, log_file)
    by_actor = {c["actor"]: c for c in snap["claims"]}
    assert by_actor["amy"]["quiet"] is False
    assert by_actor["zoe"]["quiet"] is False, (
        "an agent that acted seconds ago was declared abandoned: " + repr(by_actor["zoe"])
    )
    assert "quiet" not in {a["kind"] for a in snap["alerts"]}, snap["alerts"]

    # and the two surfaces cannot disagree: roster recency matches claim idle
    roster = {r["actor"]: r for r in snap["roster"]}
    assert roster["zoe"]["last_seen"], roster["zoe"]


def test_the_working_now_band_is_actually_visible(board):
    """IF THIS FAILS: the claims strip renders and measures zero pixels tall.

    The card is a flex column whose body takes the remaining height, so a
    sibling between the header and the body collapses unless it opts out. The
    band had five rows of content in the DOM and no height on screen — the one
    failure mode a markup assertion cannot catch, so this pins the CSS rule.
    """
    repo, log_file, base = board
    _, body = _get(base + "/")
    assert 'id="nowBand"' in body
    assert "#nowBand { flex: none; }" in body, (
        "nowBand has no flex-basis rule; it will collapse inside the card"
    )
    # and it must not reuse a class the inherited sheet already owns. `.held`
    # there is a fade-in badge — absolute, opacity 0 — so the band rendered
    # with height and no visible content until it was given its own name.
    assert 'class="nowband"' in body, "the band lost its own class"
    assert 'class="held"' not in body, (
        "the band is using .held again, which the inherited sheet hides"
    )


def test_a_findings_text_reaches_the_feed(board):
    """IF THIS FAILS: every finding on the board renders as an empty quote line.

    `find` writes a finding's text as data["summary"] — that is what the CLI
    writes, what the Go build wrote before it, and what the findings panel
    already reads. The feed builder read data["body"] alone, which only notes
    carry. So the board showed 1532 findings in the real store with the category
    badge, the actor and the time, and nothing where the sentence goes.

    It is worth a test rather than a fix alone because nothing else notices: the
    page still renders, the counts are still right, and the panel beside it is
    still correct. Only a person reading a blank line would ever know.
    """
    repo, log_file, base = board
    clog.append(log_file, clog.Event(
        ts=_now(), id=clog.new_id(), actor="alice", type=clog.TYPE_FINDING,
        data={"category": "ship", "summary": "v9 released and installed"},
    ))
    clog.append(log_file, clog.Event(
        ts=_now(), id=clog.new_id(), actor="alice", type=clog.TYPE_NOTE,
        data={"body": "a note carries body, and must keep working"},
    ))

    _, raw = _get(base + "/api/status")
    feed = json.loads(raw).get("feed", [])
    by_type = {e["type"]: e for e in feed}

    assert by_type["finding"]["body"] == "v9 released and installed", (
        "the finding reached the feed without its text"
    )
    assert by_type["note"]["body"] == "a note carries body, and must keep working"


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)
