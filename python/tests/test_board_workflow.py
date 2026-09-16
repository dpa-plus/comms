"""Exercise the JavaScript actually served by the board, with controlled IO."""

import json
import re
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
  const attributes = {};
  const node = {id, textContent: '', value: '', hidden: false,
    disabled: false, scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    selectionStart: 0, selectionEnd: 0,
    classList: {add() {}, remove() {}, toggle() {}, contains() {return false;}},
    addEventListener(k, fn) {handlers[k] = fn;},
    click() {if (handlers.click) handlers.click({target: this}); if(this.onclick) this.onclick();},
    focus() {document.activeElement = this;},
    setSelectionRange(start, end) {this.selectionStart = start; this.selectionEnd = end;},
    setAttribute(key, value) {attributes[key] = String(value);},
    getAttribute(key) {return attributes[key] || '';},
    querySelector() {return null;},
    querySelectorAll(selector) {
      if (id === 'tasks' && selector === '[data-graph-task]') return elements._graphNodes || [];
      return [];
    }
  };
  let html = '';
  Object.defineProperty(node, 'innerHTML', {
    get() {return html;},
    set(value) {
      html = value;
      if (id === 'tasks') {
        ['graphSearch','graphCompleted','graphView','listView','graphZoomOut','graphFit',
         'graphZoomIn','graphDetails','graphShowCompleted','graphMoreRelated','graphViewport']
          .forEach(key => {delete elements[key];});
        elements._graphNodes = [];
        const pattern = /data-graph-task="([^"]+)"/g;
        let match;
        while ((match = pattern.exec(String(value)))) {
          const graphNode = element('generated-node');
          graphNode.setAttribute('data-graph-task', match[1]);
          elements._graphNodes.push(graphNode);
        }
      }
    }
  });
  return node;
}
const document = {documentElement: element('root'),
  activeElement: null,
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
const snapshot = {tasks: [], task_edges: [], code_map_available: false,
  claims: [], feed: [], projects: [], counts: {}, store_key: 'one',
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


def test_graph_defaults_to_open_work_and_a_meaningful_doing_focus(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.tasks = [
 {id: 'build', title: 'Make contacts easier', phase: 'doing', doers: ['sol'], files_held: 2},
 {id: 'check', title: 'Check the result', phase: 'review', did: 'astra'},
 {id: 'next', title: 'Another step', phase: 'ready'},
 {id: 'done', title: 'Old finished work', phase: 'closed', did: 'sol'}];
snapshot.roster = [{actor: 'sol', last_seen: new Date().toISOString()}];
snapshot.alerts = [{kind: 'review', text: 'Check the result'}];
source.onmessage({data: JSON.stringify(snapshot)});
process.stdout.write(JSON.stringify({tasks: elements.tasks.innerHTML, selected: GRAPH_STATE.selected, attention: elements.alarms.innerHTML}));
''')
    assert "@sol" in result["tasks"]
    assert "2 holds" in result["tasks"]
    assert "Checking" in result["tasks"]
    assert "@astra" in result["tasks"]
    assert "Up next" in result["tasks"]
    assert "Old finished work" not in result["tasks"]
    assert result["selected"] == "build"
    assert "Check the result" not in result["attention"]


def test_default_focus_prefers_a_task_with_a_visible_connection(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.code_map_available = true;
snapshot.tasks = [
 {id: 'only-hidden', title: 'Only linked to old work', phase: 'ready', related: [{task: 'old', shared: 1}]},
 {id: 'connected', title: 'Connected current work', phase: 'ready', related: [{task: 'peer', shared: 1}]},
 {id: 'peer', title: 'Visible peer', phase: 'ready', related: [{task: 'connected', shared: 1}]},
 {id: 'old', title: 'Completed peer', phase: 'closed', related: [{task: 'only-hidden', shared: 1}]}];
source.onmessage({data: JSON.stringify(snapshot)});
process.stdout.write(JSON.stringify({selected: GRAPH_STATE.selected, html: elements.tasks.innerHTML}));
''')
    assert result["selected"] == "connected"
    assert result["html"].count('class="graph-edge related') == 1


def test_show_completed_adds_results_without_hiding_open_work(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.tasks = [
 {id: 'open', title: 'Open work', phase: 'ready'},
 {id: 'done', title: 'Finished work', phase: 'closed'}];
source.onmessage({data: JSON.stringify(snapshot)});
const before = elements.tasks.innerHTML;
setGraphCompleted(true);
process.stdout.write(JSON.stringify({before, after: elements.tasks.innerHTML}));
''')
    assert "Open work" in result["before"]
    assert "Finished work" not in result["before"]
    assert "Open work" in result["after"]
    assert "Finished work" in result["after"]


def test_declared_dependencies_and_selected_code_links_stay_distinct_and_deduplicated(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.code_map_available = true;
snapshot.tasks = [
 {id: 'a', title: 'Prepare data', phase: 'doing', doers: ['sol'], related: [
   {task: 'c', shared: 2, via: ['one.py']}, {task: 'c', shared: 2, via: ['one.py']}]},
 {id: 'b', title: 'Build the view', phase: 'ready', related: []},
 {id: 'c', title: 'Update shared behavior', phase: 'ready', related: [{task: 'a', shared: 2}]},
 {id: 'd', title: 'Independent work', phase: 'ready', related: []}];
snapshot.task_edges = [{from: 'a', to: 'b', kind: 'consumes', provides: 'schema'}];
source.onmessage({data: JSON.stringify(snapshot)});
const html = elements.tasks.innerHTML;
process.stdout.write(JSON.stringify({
  dependencyEdges: (html.match(/class="graph-edge dependency/g) || []).length,
  relatedEdges: (html.match(/class="graph-edge related/g) || []).length,
  html
}));
''')
    assert result["dependencyEdges"] == 1
    assert result["relatedEdges"] == 1
    assert "Unlocks" in result["html"]
    assert "Related work" in result["html"]
    assert "Independent work" in result["html"]


def test_dependency_arrow_ends_at_the_target_circle_edge_not_behind_its_center(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.tasks = [
 {id: 'a', title: 'Prerequisite', phase: 'doing', doers: ['sol']},
 {id: 'b', title: 'Dependent work', phase: 'ready'}];
snapshot.task_edges = [{from: 'a', to: 'b', kind: 'sequence', provides: ''}];
source.onmessage({data: JSON.stringify(snapshot)});
process.stdout.write(JSON.stringify(elements.tasks.innerHTML));
''')
    path = re.search(r'class="graph-edge dependency[^"]*" d="([^"]+)"', result)
    target = re.search(r'data-graph-task="b" style="left:([\d.]+)px', result)
    assert path and target
    endpoint = re.search(r'([\d.]+),([\d.]+)$', path.group(1))
    assert endpoint, path.group(1)
    target_center_x = float(target.group(1)) + 25
    assert abs(float(endpoint.group(1)) - target_center_x) >= 18
    assert "Unlocks dependent task" in result


def test_hidden_completed_dependency_is_counted_and_can_be_revealed(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.tasks = [
 {id: 'open', title: 'Use the finished API', phase: 'doing', doers: ['sol'], related: []},
 {id: 'done', title: 'Finished API', phase: 'closed', related: []}];
snapshot.task_edges = [{from: 'done', to: 'open', kind: 'consumes', provides: 'API'}];
source.onmessage({data: JSON.stringify(snapshot)});
const before = elements.tasks.innerHTML;
setGraphCompleted(true);
const after = elements.tasks.innerHTML;
process.stdout.write(JSON.stringify({before, after}));
''')
    assert "1 connection to completed work" in result["before"]
    assert "Show completed" in result["before"]
    assert "Finished API" not in result["before"]
    assert "Finished API" in result["after"]
    assert 'class="graph-edge dependency' in result["after"]


def test_missing_code_map_is_not_reported_as_a_proven_absence_of_links(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.tasks = [{id: 'one', title: 'Standalone work', phase: 'doing', doers: ['sol'], related: []}];
source.onmessage({data: JSON.stringify(snapshot)});
const missing = elements.tasks.innerHTML;
snapshot.code_map_available = true;
source.onmessage({data: JSON.stringify(snapshot)});
process.stdout.write(JSON.stringify({missing, loaded: elements.tasks.innerHTML}));
''')
    assert "no code map is loaded" in result["missing"]
    assert "No code-related links were found in the loaded map" not in result["missing"]
    assert "No code-related links were found in the loaded map" in result["loaded"]


def test_selected_related_links_are_capped_with_an_explicit_expand_affordance(tmp_path):
    result = run_page(tmp_path, r'''
const relatives = [];
snapshot.tasks = [{id: 'focus', title: 'Focused work', phase: 'doing', doers: ['sol'], related: relatives}];
for (let i = 0; i < 14; i++) {
  const id = 'other-' + i;
  relatives.push({task: id, shared: 14 - i, via: ['src/' + i + '.py']});
  snapshot.tasks.push({id, title: 'Related task ' + i, phase: 'ready', related: [{task: 'focus', shared: 14 - i}]});
}
snapshot.code_map_available = true;
source.onmessage({data: JSON.stringify(snapshot)});
const before = elements.tasks.innerHTML;
setGraphRelatedExpanded(true);
const after = elements.tasks.innerHTML;
process.stdout.write(JSON.stringify({before, after}));
''')
    assert "Showing 12 of 14 visible related links" in result["before"]
    assert "Show all related" in result["before"]
    assert (result["before"].count('class="graph-edge related')) == 12
    assert (result["after"].count('class="graph-edge related')) == 14


def test_graph_state_survives_same_project_push_and_resets_for_another_project(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.tasks = [
 {id: 'open', title: 'Open work', phase: 'doing', doers: ['sol']},
 {id: 'done', title: 'Finished work', phase: 'closed'}];
source.onmessage({data: JSON.stringify(snapshot)});
setGraphCompleted(true); setGraphQuery('finished'); selectGraphTask('done'); setGraphZoom(1.25);
elements.graphViewport.scrollLeft = 41; elements.graphViewport.scrollTop = 17;
source.onmessage({data: JSON.stringify(snapshot)});
const same = {completed: GRAPH_STATE.showCompleted, query: GRAPH_STATE.query,
  selected: GRAPH_STATE.selected, zoom: GRAPH_STATE.zoom,
  left: elements.graphViewport.scrollLeft, top: elements.graphViewport.scrollTop};
snapshot.store_key = 'two'; snapshot.tasks = [{id: 'fresh', title: 'Fresh project task', phase: 'ready'}];
source.onmessage({data: JSON.stringify(snapshot)});
process.stdout.write(JSON.stringify({same, reset: {
  completed: GRAPH_STATE.showCompleted, query: GRAPH_STATE.query,
  selected: GRAPH_STATE.selected, zoom: GRAPH_STATE.zoom}}));
''')
    assert result["same"] == {
        "completed": True, "query": "finished", "selected": "done",
        "zoom": 1.25, "left": 41, "top": 17,
    }
    assert result["reset"] == {
        "completed": False, "query": "", "selected": "fresh", "zoom": 1,
    }


def test_typing_search_keeps_keyboard_focus_through_filter_and_live_refresh(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.tasks = [
 {id: 'first', title: 'Unrelated setup', phase: 'doing', doers: ['sol']},
 {id: 'match', title: 'Kontakt workflow', phase: 'ready'}];
source.onmessage({data: JSON.stringify(snapshot)});
const firstInput = elements.graphSearch;
firstInput.focus(); firstInput.value = 'K'; firstInput.selectionStart = 1; firstInput.selectionEnd = 1;
firstInput.oninput({target: firstInput});
const afterType = {same: document.activeElement === elements.graphSearch,
  value: elements.graphSearch.value, caret: elements.graphSearch.selectionStart,
  selected: GRAPH_STATE.selected};
source.onmessage({data: JSON.stringify(snapshot)});
const afterPush = {same: document.activeElement === elements.graphSearch,
  value: elements.graphSearch.value, caret: elements.graphSearch.selectionStart,
  selected: GRAPH_STATE.selected};
process.stdout.write(JSON.stringify({afterType, afterPush, html: elements.tasks.innerHTML}));
''')
    assert result["afterType"] == {"same": True, "value": "K", "caret": 1, "selected": "match"}
    assert result["afterPush"] == {"same": True, "value": "K", "caret": 1, "selected": "match"}
    assert "Kontakt workflow" in result["html"]
    assert "Unrelated setup" not in result["html"]


def test_sequential_multiword_search_preserves_spaces_and_selects_the_match(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.tasks = [
 {id: 'other', title: 'Other work', phase: 'doing', doers: ['sol']},
 {id: 'match', title: 'Karten design review', phase: 'ready'}];
source.onmessage({data: JSON.stringify(snapshot)});
for (const ch of 'Karten design') {
  const input = elements.graphSearch;
  input.focus(); input.value += ch;
  input.selectionStart = input.value.length; input.selectionEnd = input.value.length;
  input.oninput({target: input});
}
process.stdout.write(JSON.stringify({value: elements.graphSearch.value,
  state: GRAPH_STATE.query, selected: GRAPH_STATE.selected, html: elements.tasks.innerHTML}));
''')
    assert result["value"] == "Karten design"
    assert result["state"] == "Karten design"
    assert result["selected"] == "match"
    assert "Karten design review" in result["html"]


def test_no_match_search_does_not_show_an_unrelated_focus_summary(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.tasks = [{id: 'other', title: 'Unrelated work', phase: 'doing', doers: ['sol']}];
source.onmessage({data: JSON.stringify(snapshot)});
const input = elements.graphSearch;
input.focus(); input.value = 'nothing here'; input.selectionStart = 12; input.selectionEnd = 12;
input.oninput({target: input});
process.stdout.write(JSON.stringify({selected: GRAPH_STATE.selected, html: elements.tasks.innerHTML}));
''')
    assert result["selected"] is None
    assert 'class="graph-focus"' not in result["html"]
    assert "Unrelated work" not in result["html"]


def test_keyboard_node_selection_restores_focus_to_the_rebuilt_node(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.tasks = [
 {id: 'first', title: 'First task', phase: 'ready'},
 {id: 'second', title: 'Second task', phase: 'ready'}];
source.onmessage({data: JSON.stringify(snapshot)});
const before = elements._graphNodes.filter(n => n.getAttribute('data-graph-task') === 'second')[0];
before.focus(); before.click();
const after = elements._graphNodes.filter(n => n.getAttribute('data-graph-task') === 'second')[0];
process.stdout.write(JSON.stringify({selected: GRAPH_STATE.selected,
  focused: document.activeElement === after, rebuilt: before !== after,
  nodeIdInMarkup: elements.tasks.innerHTML.indexOf('id="graphNode-') >= 0}));
''')
    assert result == {"selected": "second", "focused": True, "rebuilt": True, "nodeIdInMarkup": True}


def test_graph_escapes_titles_and_uses_readable_labels_without_leading_ids(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.tasks = [
 {id: 'raw/internal-path', title: '<img src=x onerror=alert(1)>', phase: 'doing', doers: []},
 {id: 'ord-task', title: 'ORD09 - Make the task understandable', phase: 'ready'}];
source.onmessage({data: JSON.stringify(snapshot)});
process.stdout.write(JSON.stringify(elements.tasks.innerHTML));
''')
    assert '<img src=x' not in result
    assert '&lt;img src=x onerror=alert(1)&gt;' in result
    assert '>ORD09 - Make the task understandable<' not in result
    assert '>Make the task understandable<' in result
    assert 'title="ORD09 - Make the task understandable' in result
    assert '>raw/internal-path<' not in result


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


def test_task_detail_names_declared_dependency_direction_kind_and_provides(tmp_path):
    result = run_page(tmp_path, r'''
snapshot.tasks = [
 {id: 'api', title: 'Build the API', phase: 'ready'},
 {id: 'ui', title: 'Connect the interface', phase: 'blocked', blocked_by: ['api']},
 {id: 'ship', title: 'Publish the release', phase: 'blocked', blocked_by: ['ui']}];
snapshot.task_edges = [
 {from: 'api', to: 'ui', kind: 'consumes', provides: 'stable schema'},
 {from: 'ui', to: 'ship', kind: 'sequence', provides: ''}];
source.onmessage({data: JSON.stringify(snapshot)});
openTask('ui');
process.stdout.write(JSON.stringify(elements.tdet.innerHTML));
''')
    assert "DECLARED DEPENDENCIES (2)" in result
    assert "Depends on Build the API" in result
    assert "Used by Publish the release" in result
    assert "consumes" in result and "stable schema" in result
    assert "sequence" in result


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
source.onmessage({data: JSON.stringify(snapshot)});
setGraphCompleted(true);
source.onmessage({data: JSON.stringify(snapshot)});
process.stdout.write(JSON.stringify(elements.tasks.innerHTML));
''')
    assert "Result 1" in result and "Result 4" in result
    assert ">Hide completed<" in result


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
setGraphCompleted(true);
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
