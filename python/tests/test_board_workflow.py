"""Exercise the JavaScript actually served by the board, with controlled IO."""

import json
import shutil
import subprocess

import pytest

from comms_graph import server


def run_page(tmp_path, exercise):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to exercise the served UI")
    # Only browser IO is simulated. Renderers, event handlers and state
    # transitions are the production script, not a second implementation.
    harness = r'''
const elements = {};
function element(id) {
  const handlers = {};
  return {id, innerHTML: '', textContent: '', value: '', hidden: false,
    disabled: false, scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    classList: {add() {}, remove() {}, toggle() {}, contains() {return false;}},
    addEventListener(k, fn) {handlers[k] = fn;},
    click() {if (handlers.click) handlers.click({target: this}); if(this.onclick) this.onclick();},
    focus() {}, setAttribute() {}, getAttribute() {return '';},
    querySelector() {return null;}, querySelectorAll() {return [];}
  };
}
const document = {documentElement: element('root'),
  getElementById(id) {return elements[id] || (elements[id] = element(id));},
  addEventListener() {}};
const location = {search: '?store=one', reload() {}};
const window = {location, prompt() {return null;}, addEventListener() {}};
const localStorage = {getItem() {return null;}, setItem() {}};
let source;
function EventSource() {source = this; this.close = function() {};}
function setInterval() {}
function setTimeout() {}
function clearTimeout() {}
function alert() {}
const snapshot = {tasks: [], claims: [], feed: [], projects: [], counts: {},
  roster: [], alerts: [], dirty: {}, guard: {}, generated: '2026-09-16 12:00:00 UTC'};
'''
    script = server._PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
    path = tmp_path / "board-workflow.js"
    path.write_text(harness + script + "\n" + exercise)
    result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_connection_does_not_claim_data_is_live_before_a_snapshot(tmp_path):
    result = run_page(tmp_path, r'''
source.onopen();
const opening = elements.liveTxt.textContent;
source.onmessage({data: JSON.stringify(snapshot)});
const loaded = elements.liveTxt.textContent;
source.onerror();
const disconnected = elements.liveTxt.textContent;
process.stdout.write(JSON.stringify({opening, loaded, disconnected, retained: D === null ? null : D.tasks}));
''')
    assert result["opening"] == "Loading work"
    assert result["loaded"] == "Live"
    assert result["disconnected"] == "Reconnecting"
    assert result["retained"] == []


def test_bad_snapshot_preserves_previous_work_and_recovers(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.tasks = [{id: 'one', title: 'Keep my work', phase: 'ready'}];
source.onmessage({data: JSON.stringify(snapshot)});
source.onmessage({data: '{broken'});
const afterBad = {state: elements.liveTxt.textContent, title: D.tasks[0].title};
source.onmessage({data: JSON.stringify(snapshot)});
process.stdout.write(JSON.stringify({afterBad, recovered: elements.liveTxt.textContent}));
''')
    assert result["afterBad"] == {"state": "Update failed", "title": "Keep my work"}
    assert result["recovered"] == "Live"


def test_silent_connection_becomes_stale_without_hiding_work(tmp_path):
    result = run_page(tmp_path, r'''
source.onmessage({data: JSON.stringify(snapshot)});
const original = Date.now;
Date.now = () => original() + 40000;
tick();
process.stdout.write(JSON.stringify({state: elements.liveTxt.textContent, hasWork: D !== null}));
''')
    assert result == {"state": "Updates delayed", "hasWork": True}
