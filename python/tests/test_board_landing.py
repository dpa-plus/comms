"""Where the board opens when it is run as a login service.

The service runs from ``/``, which is not a project, so its own view is always
empty. A board whose one view is who holds which files must not open on a page
that says nobody holds a file while a project on the rail has holders.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from comms_graph import log as clog
from comms_graph import server as cserver

HARNESS = r'''
const elements = {};
function fakeElement() {
  return {
    innerHTML: "", textContent: "", value: "", hidden: false,
    disabled: false, src: "", scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
    addEventListener() {}, getAttribute() { return ""; }, setAttribute() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; }
  };
}
const document = {
  documentElement: fakeElement(),
  getElementById(id) { return elements[id] || (elements[id] = fakeElement()); },
  addEventListener() {}
};
let replaced = [];
let closes = 0;
let source = null;
const location = {search: SEARCH, reload() {}, replace(url) { replaced.push(url); }};
const window = {
  location: location, prompt() { return null; },
  addEventListener() {}, dispatchEvent() {}, ResizeObserver: null
};
const localStorage = {getItem() { return null; }, setItem() {}};
const history = {replaceState() {}};
function EventSource() {
  source = this;
  this.close = function () { closes += 1; };
}
function alert() {}
function setInterval() {}
function setTimeout() {}
'''


def _project(key, idle, current=False):
    return {"key": key, "name": key, "root": "/work/" + key, "exists": True,
            "bytes": 10, "idle_seconds": idle, "current": current,
            "band": "active" if idle < 3600 else "low"}


def _land(tmp_path, monkeypatch, *, search="", projects, with_event=False):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; cannot exercise the served page script")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    root = tmp_path / "service-root"
    root.mkdir()
    log_file = clog.log_path(root)
    board = cserver.Board(root, log_file)
    snap = board.snapshot()
    assert snap["claims"] == [] and snap["feed"] == []
    snap["projects"] = projects
    if with_event:
        snap["feed"] = [{"id": "01A", "type": "note", "actor": "dev", "ts": snap["generated"],
                         "scopes": [], "scope": "", "body": "hi"}]
    script = board.page().split("<script>", 1)[1].split("</script>", 1)[0]
    exercise = (
        "\nsource.onmessage({data: JSON.stringify(" + json.dumps(snap) + ")});\n"
        "process.stdout.write(JSON.stringify({replaced: replaced, closes: closes}));\n"
    )
    path = tmp_path / "page.js"
    path.write_text("const SEARCH = " + json.dumps(search) + ";\n" + HARNESS + script + exercise)
    out = subprocess.run([node, str(path)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_board_with_no_project_of_its_own_opens_the_most_recently_active_one(
        tmp_path, monkeypatch):
    got = _land(tmp_path, monkeypatch,
                projects=[_project("old1", 90000), _project("busy", 40), _project("old2", 7200)])
    assert got == {"replaced": ["?store=busy"], "closes": 1}


def test_a_board_run_inside_a_project_stays_on_it(tmp_path, monkeypatch):
    got = _land(tmp_path, monkeypatch,
                projects=[_project("mine", 5000, current=True), _project("busy", 40)])
    assert got["replaced"] == []


def test_a_chosen_project_is_never_replaced(tmp_path, monkeypatch):
    got = _land(tmp_path, monkeypatch, search="?store=old1",
                projects=[_project("old1", 90000), _project("busy", 40)])
    assert got["replaced"] == []


def test_a_board_whose_own_log_has_events_stays_on_it(tmp_path, monkeypatch):
    got = _land(tmp_path, monkeypatch, with_event=True,
                projects=[_project("busy", 40)])
    assert got["replaced"] == []


def test_a_board_with_no_projects_at_all_stays_put(tmp_path, monkeypatch):
    got = _land(tmp_path, monkeypatch, projects=[])
    assert got["replaced"] == []
