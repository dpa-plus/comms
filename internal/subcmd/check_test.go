package subcmd

import (
	"path/filepath"
	"testing"
	"time"

	"github.com/dpa-plus/comms/internal/state"
)

func TestMakeRepoRelative_AllowsDotDotPrefixFilename(t *testing.T) {
	repo := t.TempDir()
	inside := filepath.Join(repo, "..not-parent.ts")

	got, ok := makeRepoRelative(inside, repo)
	if !ok {
		t.Fatalf("path inside repo with '..' prefix filename should be accepted")
	}
	if got != "..not-parent.ts" {
		t.Fatalf("got %q", got)
	}
}

func TestMakeRepoRelative_RejectsParentEscape(t *testing.T) {
	repo := t.TempDir()
	outside := filepath.Join(repo, "..", "outside.ts")

	if got, ok := makeRepoRelative(outside, repo); ok {
		t.Fatalf("outside path accepted as %q", got)
	}
}

// A PreToolUse hook inherits the environment, and COMMS_ACTOR is set per command
// by agents rather than exported, so the hook usually has no actor. Without one,
// every claim looks like a stranger's: including the caller's own, so an agent
// that claimed a file was then blocked from editing it. The hook payload carries
// the same session id the agent's environment had at `comms hello`, so the log
// can answer what the environment cannot.
func TestHookResolvesItsActorFromTheAgentSessionID(t *testing.T) {
	st := &state.State{Sessions: map[string]*state.Session{
		"agent-a": {Actor: "agent-a", AgentSession: "sess-1", TS: time.Unix(100, 0)},
		"agent-b": {Actor: "agent-b", AgentSession: "sess-2", TS: time.Unix(200, 0)},
		"agent-c": {Actor: "agent-c", TS: time.Unix(300, 0)},
	}}
	if got := resolveHookActor(st, "sess-1"); got != "agent-a" {
		t.Errorf("sess-1 resolved to %q, want agent-a", got)
	}
	if got := resolveHookActor(st, "sess-2"); got != "agent-b" {
		t.Errorf("sess-2 resolved to %q, want agent-b", got)
	}
	// An unknown session must resolve to nothing, so the caller falls back to the
	// sentinel and the hook stays conservative rather than guessing an identity.
	if got := resolveHookActor(st, "sess-unknown"); got != "" {
		t.Errorf("unknown session resolved to %q, want empty", got)
	}
	if got := resolveHookActor(st, ""); got != "" {
		t.Errorf("empty session resolved to %q, want empty", got)
	}
	if got := resolveHookActor(nil, "sess-1"); got != "" {
		t.Errorf("nil state resolved to %q, want empty", got)
	}
}

// Re-hello under the same agent session (a new actor name mid-session) must win.
func TestHookActorPrefersTheNewestHelloForThatSession(t *testing.T) {
	st := &state.State{Sessions: map[string]*state.Session{
		"old-name": {Actor: "old-name", AgentSession: "sess-1", TS: time.Unix(100, 0)},
		"new-name": {Actor: "new-name", AgentSession: "sess-1", TS: time.Unix(500, 0)},
	}}
	if got := resolveHookActor(st, "sess-1"); got != "new-name" {
		t.Errorf("resolved to %q, want new-name (the newest hello)", got)
	}
}
