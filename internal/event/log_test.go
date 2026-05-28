package event

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func writeFile(t *testing.T, dir, name, content string) string {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, []byte(content), 0o600); err != nil {
		t.Fatalf("writeFile: %v", err)
	}
	return p
}

func TestReadMissingFile(t *testing.T) {
	events, err := Read(filepath.Join(t.TempDir(), "nope.jsonl"))
	if err != nil {
		t.Fatalf("missing file should not error, got %v", err)
	}
	if len(events) != 0 {
		t.Fatalf("expected empty, got %d events", len(events))
	}
}

func TestReadBlankLinesAndUnterminated(t *testing.T) {
	dir := t.TempDir()
	now := time.Now().UTC()
	id := NewID(now)
	body := `
{"ts":"` + now.Format(time.RFC3339Nano) + `","id":"` + id + `","actor":"a","type":"hello"}

` // trailing unterminated empty line
	path := writeFile(t, dir, "log.jsonl", body)
	events, err := Read(path)
	if err != nil {
		t.Fatalf("Read: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	if events[0].Actor != "a" {
		t.Errorf("actor: got %q", events[0].Actor)
	}
}

func TestReadCorruptMidFile(t *testing.T) {
	dir := t.TempDir()
	now := time.Now().UTC()
	body := `{"ts":"` + now.Format(time.RFC3339Nano) + `","id":"` + NewID(now) + `","actor":"a","type":"hello"}
not-json
`
	path := writeFile(t, dir, "log.jsonl", body)
	_, err := Read(path)
	if err == nil {
		t.Fatalf("expected ErrCorrupt")
	}
	var ec *ErrCorrupt
	if !errors.As(err, &ec) {
		t.Fatalf("expected *ErrCorrupt, got %T: %v", err, err)
	}
	if ec.Line != 2 {
		t.Errorf("expected line 2, got %d", ec.Line)
	}
}

func TestReadDuplicateIDs(t *testing.T) {
	dir := t.TempDir()
	now := time.Now().UTC()
	id := NewID(now)
	body := `{"ts":"` + now.Format(time.RFC3339Nano) + `","id":"` + id + `","actor":"a","type":"hello"}
{"ts":"` + now.Format(time.RFC3339Nano) + `","id":"` + id + `","actor":"b","type":"hello"}
`
	path := writeFile(t, dir, "log.jsonl", body)
	events, err := Read(path)
	if err != nil {
		t.Fatalf("Read: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 (dedup), got %d", len(events))
	}
	if events[0].Actor != "a" {
		t.Errorf("first should win, got actor %q", events[0].Actor)
	}
}

func TestReadRejectsOversizedLine(t *testing.T) {
	dir := t.TempDir()
	path := writeFile(t, dir, "log.jsonl", strings.Repeat("x", maxLineBytes+1)+"\n")
	_, err := Read(path)
	if err == nil {
		t.Fatalf("expected ErrCorrupt")
	}
	var ec *ErrCorrupt
	if !errors.As(err, &ec) {
		t.Fatalf("expected *ErrCorrupt, got %T: %v", err, err)
	}
}

func TestAppendReadRoundTrip(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "log.jsonl")
	now := time.Now().UTC()
	for i := 0; i < 5; i++ {
		ev := Event{
			TS:    now.Add(time.Duration(i) * time.Second),
			ID:    NewID(now.Add(time.Duration(i) * time.Second)),
			Actor: "agent-" + string(rune('a'+i)),
			Type:  TypeHello,
		}
		if err := Append(path, ev); err != nil {
			t.Fatalf("Append %d: %v", i, err)
		}
	}
	events, err := Read(path)
	if err != nil {
		t.Fatalf("Read: %v", err)
	}
	if len(events) != 5 {
		t.Fatalf("expected 5, got %d", len(events))
	}
}

// TestConcurrentAppendNoCorruption verifies that O_APPEND keeps individual
// event lines intact even without explicit locking. This is the in-process
// version of V4 (the cross-process race lives in tests/concurrent_test.go).
func TestConcurrentAppendNoCorruption(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "log.jsonl")
	now := time.Now().UTC()
	const N = 50
	var wg sync.WaitGroup
	wg.Add(N)
	for i := 0; i < N; i++ {
		go func(i int) {
			defer wg.Done()
			ev := Event{
				TS:    now,
				ID:    NewID(now.Add(time.Duration(i) * time.Microsecond)),
				Actor: "agent",
				Type:  TypeHello,
				Data:  map[string]interface{}{"i": i},
			}
			if err := Append(path, ev); err != nil {
				t.Errorf("Append: %v", err)
			}
		}(i)
	}
	wg.Wait()

	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("ReadFile: %v", err)
	}
	lines := strings.Split(strings.TrimRight(string(raw), "\n"), "\n")
	if len(lines) != N {
		t.Fatalf("expected %d lines, got %d", N, len(lines))
	}
	events, err := Read(path)
	if err != nil {
		t.Fatalf("Read: %v", err)
	}
	if len(events) != N {
		t.Fatalf("expected %d events after dedup, got %d", N, len(events))
	}
}
