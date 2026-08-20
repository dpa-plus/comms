package subcmd

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// The dashboard's history merge decides whether an audit row survives, and it
// lives in JavaScript inside the uiHTML const. Asserting that certain source
// strings are present proves nothing about what the code DOES, so this test
// extracts the merge block verbatim between its sentinel comments and executes it
// under node with the surrounding page stubbed out.
//
// The scenarios are the ones that can silently lose a row: a delta merging on top
// of a full frame, a stale /api/status body resolving after a newer push, a frame
// that shares our newest timestamp while holding fewer of the rows stamped with it
// (bulk release and batch claim stamp every event they generate with one instant),
// and a failed recovery fetch that must not suppress the retry.
const (
	historyMergeStart = "// history-merge:begin"
	historyMergeEnd   = "// history-merge:end"
)

func extractHistoryMergeJS(t *testing.T) string {
	t.Helper()
	// Guard the extraction itself: a duplicated sentinel or a second copy of
	// mergeHistory would let this test silently exercise the wrong code and pass
	// while the page ran something else.
	for marker, count := range map[string]int{
		historyMergeStart:             strings.Count(uiHTML, historyMergeStart),
		historyMergeEnd:               strings.Count(uiHTML, historyMergeEnd),
		"function mergeHistory(data)": strings.Count(uiHTML, "function mergeHistory(data)"),
	} {
		if count != 1 {
			t.Fatalf("expected exactly one %q in uiHTML, found %d", marker, count)
		}
	}
	start := strings.Index(uiHTML, historyMergeStart)
	end := strings.Index(uiHTML, historyMergeEnd)
	if end < start {
		t.Fatalf("history merge sentinels are inverted (start=%d end=%d)", start, end)
	}
	block := uiHTML[start : end+len(historyMergeEnd)]
	// And prove the block we extracted is the one the page actually calls.
	if !strings.Contains(block, "function mergeHistory(data)") {
		t.Fatal("extracted block does not contain mergeHistory")
	}
	if !strings.Contains(uiHTML, "mergeHistory(data);") {
		t.Fatal("applySnapshot no longer calls mergeHistory; the extracted block is dead code")
	}
	return block
}

const historyMergeHarness = `
let loadCalls = 0;
let loadImpl = async () => {};
let lastError = null;
async function load() { loadCalls++; return loadImpl(); }
function showError(err) { lastError = String((err && err.message) || err); }
const settle = () => new Promise(r => setTimeout(r, 0));
const ev = (id, ts) => ({ id, ts });
const reset = () => {
  historyEvents = []; historySeen = new Set();
  historyResyncing = false; historyResyncedTotal = -1; historyMergeSeq = 0;
  loadCalls = 0; lastError = null;
};
const ids = () => historyEvents.map(e => e.id).join(',');
const dupes = () => historyEvents.length - new Set(historyEvents.map(e => e.id)).size;

(async () => {
  const out = {};

  // 1. The first complete frame populates an empty history.
  reset();
  mergeHistory({ events: [ev('c', '2026-08-19T10:00:03Z'), ev('b', '2026-08-19T10:00:02Z'), ev('a', '2026-08-19T10:00:01Z')], events_total: 3 });
  out.initial_ids = ids();

  // 2. A delta merges on top, repeating the watermark row, and dedupes it.
  mergeHistory({ events_delta: true, events: [ev('d', '2026-08-19T10:00:04Z'), ev('c', '2026-08-19T10:00:03Z')], events_total: 4 });
  out.after_delta_ids = ids();
  out.after_delta_dupes = dupes();

  // 3. A stale complete frame (no authoritative stamp) must not drop the row the
  //    delta added while it was in flight.
  mergeHistory({ events: [ev('c', '2026-08-19T10:00:03Z'), ev('b', '2026-08-19T10:00:02Z'), ev('a', '2026-08-19T10:00:01Z')], events_total: 3 });
  out.after_stale_full_ids = ids();

  // 4. Same newest timestamp, fewer rows at it. Judging freshness by timestamp
  //    alone loses one of a batch stamped with the same instant.
  reset();
  mergeHistory({ events: [ev('t2', '2026-08-19T11:00:00Z'), ev('t1', '2026-08-19T11:00:00Z')], events_total: 2 });
  out.batch_before = historyEvents.length;
  mergeHistory({ events: [ev('t1', '2026-08-19T11:00:00Z')], events_total: 1 });
  out.batch_after = historyEvents.length;
  out.batch_kept_both = historySeen.has('t1') && historySeen.has('t2');

  // 5. A frame proven current MAY replace, so a genuine shrink still applies.
  mergeHistory({ events: [ev('t1', '2026-08-19T11:00:00Z')], events_total: 1, history_authoritative: true });
  out.authoritative_ids = ids();

  // 6. A failed recovery fetch must not suppress the retry for the same total.
  reset();
  loadImpl = async () => { throw new Error('network down'); };
  mergeHistory({ events_delta: true, events: [], events_total: 99 });
  await settle(); await settle();
  out.failed_load_calls = loadCalls;
  out.failed_resynced_total = historyResyncedTotal;
  out.failed_reported = lastError;
  mergeHistory({ events_delta: true, events: [], events_total: 99 });
  await settle(); await settle();
  out.retry_load_calls = loadCalls;

  // 7. A recovery that actually reconciles records the total, and a later
  //    divergence at that same total does not restart the refetching.
  const rows99 = [];
  for (let i = 99; i >= 1; i--) rows99.push(ev('r' + i, '2026-08-19T13:' + String(Math.floor(i / 60)).padStart(2, '0') + ':' + String(i % 60).padStart(2, '0') + 'Z'));
  loadImpl = async () => { mergeHistory({ events: rows99, events_total: 99, history_authoritative: true }); };
  mergeHistory({ events_delta: true, events: [], events_total: 99 });
  await settle(); await settle();
  out.recorded_total = historyResyncedTotal;
  out.recovered_len = historyEvents.length;
  const drifted = historyEvents.pop();
  historySeen.delete(drifted.id);
  const before = loadCalls;
  mergeHistory({ events_delta: true, events: [], events_total: 99 });
  await settle(); await settle();
  out.no_extra_fetch = loadCalls === before;

  // 8. A recovery that resolved but left us still short — it could only union,
  //    because a frame merged while it was in flight — must NOT be recorded as
  //    handled, or the count we never reconciled is suppressed forever.
  reset();
  historyEvents = [ev('x', '2026-08-19T12:00:00Z')];
  historySeen = new Set(['x']);
  loadImpl = async () => {};
  mergeHistory({ events_delta: true, events: [], events_total: 5 });
  await settle(); await settle();
  out.unreconciled_total = historyResyncedTotal;
  const callsBeforeRetry = loadCalls;
  mergeHistory({ events_delta: true, events: [], events_total: 5 });
  await settle(); await settle();
  out.unreconciled_retried = loadCalls === callsBeforeRetry + 1;

  // 9. A matching total never triggers a fetch at all.
  reset();
  mergeHistory({ events: [ev('a', '2026-08-19T10:00:01Z')], events_total: 1 });
  await settle();
  out.matching_total_load_calls = loadCalls;

  console.log(JSON.stringify(out));
})();
`

func TestUIHistoryMergeLogic(t *testing.T) {
	node, err := exec.LookPath("node")
	if err != nil {
		t.Skip("node not installed; skipping execution of the dashboard history merge")
	}

	script := filepath.Join(t.TempDir(), "history-merge.mjs")
	if err := os.WriteFile(script, []byte(extractHistoryMergeJS(t)+historyMergeHarness), 0o600); err != nil {
		t.Fatalf("write harness: %v", err)
	}
	out, err := exec.Command(node, script).CombinedOutput()
	if err != nil {
		t.Fatalf("node: %v\n%s", err, out)
	}
	var got map[string]any
	if err := json.Unmarshal([]byte(strings.TrimSpace(string(out))), &got); err != nil {
		t.Fatalf("decode harness output: %v\nraw: %s", err, out)
	}

	for _, c := range []struct {
		key  string
		want any
		why  string
	}{
		{"initial_ids", "c,b,a", "the first complete frame must populate history newest-first"},
		{"after_delta_ids", "d,c,b,a", "a delta must merge in order on top of what we hold"},
		{"after_delta_dupes", float64(0), "the repeated watermark row must be deduped by id"},
		{"after_stale_full_ids", "d,c,b,a", "a stale complete frame must not drop a newer merged row"},
		{"batch_before", float64(2), "both rows sharing a timestamp must be held"},
		{"batch_after", float64(2), "a frame sharing our newest timestamp must not drop the other row at it"},
		{"batch_kept_both", true, "neither row of the same-timestamp batch may be lost"},
		{"authoritative_ids", "t1", "a frame proven current may replace, so a genuine shrink still applies"},
		{"failed_load_calls", float64(1), "a count mismatch must trigger exactly one recovery fetch"},
		{"failed_resynced_total", float64(-1), "a failed recovery must not record the total as handled"},
		{"failed_reported", "network down", "a failed recovery must surface the error"},
		{"retry_load_calls", float64(2), "the same total must be retried after a failed recovery"},
		{"recorded_total", float64(99), "a recovery that reconciled records the total it handled"},
		{"recovered_len", float64(99), "the recovery must actually restore the missing rows"},
		{"no_extra_fetch", true, "a handled total must not refetch on every later frame"},
		{"unreconciled_total", float64(-1), "a recovery that resolved without reconciling must not be recorded as handled"},
		{"unreconciled_retried", true, "an unreconciled total must still be retried on the next frame"},
		{"matching_total_load_calls", float64(0), "a matching total must never trigger a fetch"},
	} {
		if got[c.key] != c.want {
			t.Errorf("%s = %v, want %v — %s", c.key, got[c.key], c.want, c.why)
		}
	}
}
