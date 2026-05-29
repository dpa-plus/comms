package event

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestEncodeDecodeRoundTrip(t *testing.T) {
	now := time.Date(2026, 5, 22, 14, 30, 0, 0, time.UTC)
	in := Event{
		TS:    now,
		ID:    NewID(now),
		Actor: "claude-3a1f",
		Type:  TypeClaim,
		Scope: []string{"src/foo.ts#bar"},
		Data:  map[string]interface{}{"intent": "fix N+1"},
	}
	line, err := in.Encode()
	if err != nil {
		t.Fatalf("Encode: %v", err)
	}
	if !strings.HasSuffix(string(line), "\n") {
		t.Fatalf("Encode output must end in newline, got %q", line)
	}
	out, err := Decode(line[:len(line)-1])
	if err != nil {
		t.Fatalf("Decode: %v", err)
	}
	if out.Actor != in.Actor {
		t.Errorf("actor: got %q want %q", out.Actor, in.Actor)
	}
	if out.Type != in.Type {
		t.Errorf("type: got %q want %q", out.Type, in.Type)
	}
	if !out.TS.Equal(in.TS) {
		t.Errorf("ts: got %v want %v", out.TS, in.TS)
	}
	if got := out.Data["intent"]; got != "fix N+1" {
		t.Errorf("intent: got %v", got)
	}
}

func TestEncodeRejectsInvalid(t *testing.T) {
	cases := []struct {
		name string
		e    Event
	}{
		{"missing id", Event{Actor: "a", Type: TypeHello, TS: time.Now()}},
		{"missing actor", Event{ID: "x", Type: TypeHello, TS: time.Now()}},
		{"missing ts", Event{ID: "x", Actor: "a", Type: TypeHello}},
		{"invalid type", Event{ID: "x", Actor: "a", Type: "garbage", TS: time.Now()}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if _, err := c.e.Encode(); err == nil {
				t.Fatalf("expected error for %s", c.name)
			}
		})
	}
}

func TestDecodeRejectsInvalidType(t *testing.T) {
	bad, _ := json.Marshal(Event{ID: "x", Actor: "a", Type: "garbage", TS: time.Now()})
	if _, err := Decode(bad); err == nil {
		t.Fatalf("expected error for invalid type")
	}
}

func TestDecodeRejectsMissingRequiredFields(t *testing.T) {
	cases := []struct {
		name string
		e    Event
	}{
		{"missing id", Event{Actor: "a", Type: TypeHello, TS: time.Now()}},
		{"missing actor", Event{ID: "x", Type: TypeHello, TS: time.Now()}},
		{"missing ts", Event{ID: "x", Actor: "a", Type: TypeHello}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			bad, err := json.Marshal(c.e)
			if err != nil {
				t.Fatalf("marshal: %v", err)
			}
			if _, err := Decode(bad); err == nil {
				t.Fatalf("expected decode error for %s", c.name)
			}
		})
	}
}

func TestNewIDMonotonic(t *testing.T) {
	now := time.Now()
	a := NewID(now)
	b := NewID(now.Add(time.Millisecond))
	if a == b {
		t.Fatalf("IDs from different times should differ: %q vs %q", a, b)
	}
	if len(a) != 26 {
		t.Fatalf("ULID length should be 26, got %d (%q)", len(a), a)
	}
}

func TestTSForcedUTC(t *testing.T) {
	loc, _ := time.LoadLocation("America/New_York")
	e := Event{TS: time.Date(2026, 5, 22, 10, 0, 0, 0, loc), ID: "x", Actor: "a", Type: TypeHello}
	line, err := e.Encode()
	if err != nil {
		t.Fatalf("Encode: %v", err)
	}
	if !strings.Contains(string(line), `"ts":"2026-05-22T14:00:00Z"`) {
		t.Fatalf("expected UTC-normalized timestamp, got %q", line)
	}
}
