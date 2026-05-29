package subcmd

import (
	"bytes"
	"io"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/dpa-plus/comms/internal/event"
)

func TestPrintEventHumanSummarizesSessionRetire(t *testing.T) {
	ev := event.Event{
		TS:    time.Date(2026, 5, 29, 13, 45, 0, 0, time.UTC),
		ID:    "01JX2Q3Y7W5B6N9P0R1S2T3L0G",
		Actor: "human-eli",
		Type:  event.TypeRelease,
		Data: map[string]interface{}{
			"session_retire": true,
			"retired_actor":  "claude-old",
			"refs":           []string{"01JX2Q3Y7W5B6N9P0R1S2T3R3A"},
			"reason":         "renamed to claude-dev",
		},
	}

	out := captureLogStdout(t, func() { printEventHuman(ev) })
	if !strings.Contains(out, "retired @claude-old") || !strings.Contains(out, "released 1 claim") {
		t.Fatalf("retire release output is not useful: %q", out)
	}
	if strings.Contains(out, `""`) {
		t.Fatalf("retire release output should not be blank result: %q", out)
	}
}

func TestRunReleaseRejectsPositionalIDWithLatest(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "codex-dev")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	claimID := "01JX2Q3Y7W5B6N9P0R1S2T3R5A"
	if err := rt.Append(event.Event{
		TS:    time.Now().Add(-10 * time.Minute).UTC(),
		ID:    claimID,
		Actor: "codex-dev",
		Type:  event.TypeClaim,
		Scope: []string{"src/foo.ts"},
		Data:  map[string]interface{}{"intent": "work"},
	}); err != nil {
		t.Fatalf("append claim: %v", err)
	}
	if err := rt.Close(); err != nil {
		t.Fatalf("close runtime: %v", err)
	}

	err = runRelease([]string{claimID[:10]}, true, false, "", "")
	if err == nil || !strings.Contains(err.Error(), "mutually exclusive") {
		t.Fatalf("release should reject positional id with --latest, got %v", err)
	}
}

func TestRunReleaseRejectsPositionalIDWithAllMine(t *testing.T) {
	err := runRelease([]string{"01JX2Q3Y7W"}, false, true, "", "")
	if err == nil || !strings.Contains(err.Error(), "mutually exclusive") {
		t.Fatalf("release should reject positional id with --all-mine, got %v", err)
	}
}

func TestParseTypeSetRejectsUnknownEventType(t *testing.T) {
	if _, err := parseTypeSet("hello,typo"); err == nil || !strings.Contains(err.Error(), "unknown event type") {
		t.Fatalf("unknown event type should be rejected, got %v", err)
	}
}

func TestValidateLogCategoryRejectsUnknownCategory(t *testing.T) {
	if err := validateLogCategory("typo"); err == nil || !strings.Contains(err.Error(), "unknown category") {
		t.Fatalf("unknown category should be rejected, got %v", err)
	}
}

func captureLogStdout(t *testing.T, fn func()) string {
	t.Helper()
	old := os.Stdout
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatalf("pipe: %v", err)
	}
	os.Stdout = w
	fn()
	if err := w.Close(); err != nil {
		t.Fatalf("close writer: %v", err)
	}
	os.Stdout = old
	var buf bytes.Buffer
	if _, err := io.Copy(&buf, r); err != nil {
		t.Fatalf("copy stdout: %v", err)
	}
	return buf.String()
}
