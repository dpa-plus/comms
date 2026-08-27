package subcmd

import (
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/dpa-plus/comms/internal/state"
)

// Test helpers that outlived the Go dashboard.
//
// setupUITestRepo was written for the dashboard's own tests and is used by a
// dozen others that are staying. uiSession and uiSessionFrom survive for the
// same reason: status_test asserts on the 'likely dead' rule they encode, and
// that rule is still in status.

func setupUITestRepo(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	for _, args := range [][]string{
		{"init"},
		{"config", "user.email", "test@example.com"},
		{"config", "user.name", "Test"},
		{"commit", "--allow-empty", "-m", "init"},
	} {
		cmd := exec.Command("git", args...)
		cmd.Dir = dir
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %s: %v: %s", strings.Join(args, " "), err, out)
		}
	}
	return dir
}

type uiSession struct {
	Actor    string    `json:"actor"`
	Label    string    `json:"label,omitempty"`
	BaseName string    `json:"base_name"`
	Hostname string    `json:"hostname"`
	TS       time.Time `json:"ts"`
	// Liveness (derived): LastSeen is the actor's passive heartbeat (most-recent
	// event of any type); SilentFor is the human age since then; LikelyDead is
	// true when the actor holds >=1 claim AND has been silent past the stale
	// window: the crash signal that's worth an operator's attention.
	LastSeen   time.Time `json:"last_seen"`
	SilentFor  string    `json:"silent_for"`
	ClaimCount int       `json:"claim_count"`
	LikelyDead bool      `json:"likely_dead"`
	// RepoHash is set only in the unified (all-projects) snapshot so the roster's
	// per-actor Release button routes to THIS row's repo even in the merged view,
	// where the same actor name can appear under more than one repo.
	RepoHash    string `json:"repo_hash,omitempty"`
	Leader      bool   `json:"leader"`
	SessionID   string `json:"session_id,omitempty"`
	SessionName string `json:"session_name,omitempty"`
}

func uiSessionFrom(s *state.Session, now time.Time, claimCount int, staleAfter time.Duration) uiSession {
	silent := now.Sub(lastSeenOf(s))
	return uiSession{
		Actor: s.Actor, Label: s.Label, BaseName: s.BaseName, Hostname: s.Hostname,
		TS: s.TS, Leader: s.Leader, SessionID: s.SessionID, SessionName: s.SessionName,
		LastSeen: lastSeenOf(s), SilentFor: shortAge(silent), ClaimCount: claimCount,
		LikelyDead: claimCount > 0 && silent >= staleAfter,
	}
}
