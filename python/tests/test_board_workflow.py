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


def test_task_cards_show_current_owner_and_do_not_call_review_a_human_blocker(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.tasks = [
 {id: 'build', title: 'Make contacts easier', phase: 'doing', doers: ['sol'], files_held: 2},
 {id: 'check', title: 'Check the result', phase: 'review', did: 'astra'},
 {id: 'next', title: 'Another step', phase: 'ready'}];
snapshot.roster = [{actor: 'sol', last_seen: new Date().toISOString()}];
snapshot.alerts = [{kind: 'review', text: 'Check the result'}];
source.onmessage({data: JSON.stringify(snapshot)});
process.stdout.write(JSON.stringify({tasks: elements.tasks.innerHTML, attention: elements.alarms.innerHTML}));
''')
    assert "@sol" in result["tasks"]
    assert "2 holds" in result["tasks"]
    assert "Checking" in result["tasks"]
    assert "@astra" in result["tasks"]
    assert "Up next" in result["tasks"]
    assert "Check the result" not in result["attention"]


def test_open_history_filters_by_agent_and_task_not_just_event_type(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.feed = [
 {type: 'note', actor: 'sol', body: 'Visible agent note', ts: new Date().toISOString()},
 {type: 'note', actor: 'astra', body: 'Other agent note', ts: new Date().toISOString()},
 {type: 'claim', actor: 'sol', task: 'contacts', intent: 'Contact form', ts: new Date().toISOString()},
 {type: 'claim', actor: 'sol', task: 'map', intent: 'Map controls', ts: new Date().toISOString()}];
source.onmessage({data: JSON.stringify(snapshot)});
openHistory('actor', 'astra');
const actorHistory = elements.streamList.innerHTML;
openHistory('task', 'contacts');
process.stdout.write(JSON.stringify({actorHistory, taskHistory: elements.streamList.innerHTML}));
''')
    assert "Other agent note" in result["actorHistory"]
    assert "Visible agent note" not in result["actorHistory"]
    assert "Contact form" in result["taskHistory"]
    assert "Map controls" not in result["taskHistory"]


def test_task_detail_stays_current_without_closing_on_push(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.tasks = [{id: 'one', title: 'The work', phase: 'doing', doers: ['sol']}];
source.onmessage({data: JSON.stringify(snapshot)});
openTask('one');
snapshot.tasks[0] = {id: 'one', title: 'The work', phase: 'review', did: 'sol'};
source.onmessage({data: JSON.stringify(snapshot)});
process.stdout.write(JSON.stringify({detail: elements.tdet.innerHTML, hidden: elements.tdetWrap.hidden}));
''')
    assert "waiting for somebody else" in result["detail"]
    assert result["hidden"] is False


def test_team_does_not_say_nobody_is_active_when_all_active_agents_hold_work(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.roster = [{actor: 'sol', holding: 2, last_seen: new Date().toISOString()}];
source.onmessage({data: JSON.stringify(snapshot)});
process.stdout.write(JSON.stringify(elements.roster.innerHTML));
''')
    assert "Nobody has been active" not in result


def test_unrenderable_snapshot_keeps_previous_tasks_and_dom(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.tasks = [{id: 'one', title: 'Keep this task', phase: 'ready'}];
source.onmessage({data: JSON.stringify(snapshot)});
const previousHTML = elements.tasks.innerHTML;
snapshot.tasks = [{id: 'bad', phase: 'doing', doers: 'not an array'}];
source.onmessage({data: JSON.stringify(snapshot)});
process.stdout.write(JSON.stringify({title: D.tasks[0].title, same: previousHTML === elements.tasks.innerHTML, state: elements.liveTxt.textContent}));
''')
    assert result == {"title": "Keep this task", "same": True, "state": "Update failed"}


def test_earlier_results_stay_open_after_a_live_update(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.tasks = [1, 2, 3, 4].map(n => ({id: String(n), title: 'Result ' + n, phase: 'closed'}));
OPEN_RESULTS = true;
source.onmessage({data: JSON.stringify(snapshot)});
process.stdout.write(JSON.stringify(elements.tasks.innerHTML));
''')
    assert 'id="tdone" hidden' not in result
    assert 'class="tfoldc">hide' in result


def test_map_freshness_is_available_under_project_details(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.alerts = [{kind: 'stale-map', text: 'Code connections may be out of date'}];
source.onmessage({data: JSON.stringify(snapshot)});
process.stdout.write(JSON.stringify({details: elements.session.innerHTML, attention: elements.alarms.innerHTML}));
''')
    assert "Code connections may be out of date" in result["details"]
    assert "Code connections may be out of date" not in result["attention"]


def test_actor_history_keeps_multi_owner_releases(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.feed = [{type: 'release', actor: 'human', original_actor: 'sol', original_actors: ['sol', 'astra'], result: 'Session ended', ts: new Date().toISOString()}];
source.onmessage({data: JSON.stringify(snapshot)});
openHistory('actor', 'astra');
process.stdout.write(JSON.stringify(elements.streamList.innerHTML));
''')
    assert "Session ended" in result


def test_previous_verification_is_not_presented_as_a_check_of_new_work(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.tasks = [{id: 'one', title: 'Resubmitted work', phase: 'closed', did: 'sol', ever_verified: true, verified_by: ''}];
source.onmessage({data: JSON.stringify(snapshot)});
openTask('one');
process.stdout.write(JSON.stringify({card: elements.tasks.innerHTML, detail: elements.tdet.innerHTML}));
''')
    assert "Checked by" not in result["card"]
    assert "checked by" not in result["detail"]
    assert "verification not recorded" in result["card"]


def test_release_confirmation_is_in_page_scoped_and_safe_to_cancel(tmp_path):
    result = run_page(tmp_path, r'''
source.onmessage({data: JSON.stringify(snapshot)});
window.prompt = () => {throw new Error('No browser popups');};
const button = {getAttribute(key) {return {'data-id':'claim-1','data-actor':'sol','data-scope':'src/contact.py'}[key];}, focus() {}};
releaseClaim(button);
const opened = {hidden: elements.releaseWrap.hidden, scope: elements.releaseScope.textContent, actor: elements.releaseOwner.textContent};
elements.releaseCancel.click();
process.stdout.write(JSON.stringify({opened, closed: elements.releaseWrap.hidden, pending: RELEASE_PENDING}));
''')
    assert result == {"opened": {"hidden": False, "scope": "src/contact.py", "actor": "Held by @sol"}, "closed": True, "pending": None}


def test_release_requires_identity_and_reason_without_sending_a_request(tmp_path):
    result = run_page(tmp_path, r'''
source.onmessage({data: JSON.stringify(snapshot)});
let calls = 0; fetch = () => {calls++; throw new Error('Must not send');};
releaseClaim({getAttribute(k) {return {'data-id':'old','data-actor':'sol','data-scope':'a.py'}[k];}});
submitRelease({preventDefault() {}});
process.stdout.write(JSON.stringify({calls, error: elements.releaseError.textContent}));
''')
    assert result == {"calls": 0, "error": "Enter your name and a reason."}


def test_confirmed_release_uses_the_exact_original_hold_and_refreshes_the_board(tmp_path):
    result = run_page(tmp_path, r'''
(async () => {
source.onmessage({data: JSON.stringify(snapshot)});
let id = 'old'; const sent = [];
fetch = async (url, options) => {sent.push({url, body: options && JSON.parse(options.body)}); return {ok:true, json:async()=> options ? {ok:true} : snapshot};};
releaseClaim({getAttribute(k) {return {'data-id':id,'data-actor':'sol','data-scope':'a.py'}[k];}});
id = 'new';
elements.releaseActor.value = 'human'; elements.releaseReason.value = 'Session ended';
submitRelease({preventDefault() {}});
await new Promise(setImmediate);
process.stdout.write(JSON.stringify({sent, hidden: elements.releaseWrap.hidden, pending: RELEASE_PENDING}));
})();
''')
    assert result["sent"][0]["body"] == {"id": "old", "actor": "human", "reason": "Session ended", "store": "one"}
    assert result["sent"][1]["url"] == "/api/status?store=one"
    assert result["hidden"] is True
    assert result["pending"] is None


def test_release_failure_stays_visible_without_claiming_success(tmp_path):
    result = run_page(tmp_path, r'''
(async () => {
source.onmessage({data: JSON.stringify(snapshot)});
fetch = async () => ({ok:false, json:async()=>({error:'This hold was already released.'})});
releaseClaim({getAttribute(k) {return {'data-id':'old','data-actor':'sol','data-scope':'a.py'}[k];}});
elements.releaseActor.value = 'human'; elements.releaseReason.value = 'Session ended';
submitRelease({preventDefault() {}});
await new Promise(setImmediate);
process.stdout.write(JSON.stringify({error:elements.releaseError.textContent, hidden:elements.releaseWrap.hidden, reason:elements.releaseReason.value, disabled:elements.releaseConfirm.disabled}));
})();
''')
    assert result == {"error": "This hold was already released.", "hidden": False, "reason": "Session ended", "disabled": False}
