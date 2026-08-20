package tests

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// The pre-edit hook has to exit 2 to block. Claude Code treats 2 as "block this
// tool call and show stderr to the model" and EVERY other non-zero code as "the
// hook itself failed", which lets the edit through. comms exited 1 here from the
// day it shipped, so this hook never once blocked an edit in any session — while
// a comms system error exited 2 and DID block. The behaviour was inverted.
func TestPreEditHookExitsTwoSoClaudeCodeActuallyBlocks(t *testing.T) {
	bin := buildCommsBinary(t)
	repo := setupTestRepo(t)
	home := t.TempDir()

	claim := exec.Command(bin, "claim", "src/held.txt", "--intent", "peer work")
	claim.Dir = repo
	claim.Env = childEnv(home, "peer-agent")
	if out, err := claim.CombinedOutput(); err != nil {
		t.Fatalf("peer claim: %v: %s", err, out)
	}

	check := exec.Command(bin, "check", "src/held.txt")
	check.Dir = repo
	check.Env = childEnv(home, "other-agent")
	out, err := check.CombinedOutput()
	exit, ok := err.(*exec.ExitError)
	if !ok {
		t.Fatalf("check should have blocked a peer-held path; got err=%v output:\n%s", err, out)
	}
	if exit.ExitCode() != 2 {
		t.Fatalf("pre-edit hook exit = %d, want 2 (anything else lets the edit through); output:\n%s",
			exit.ExitCode(), out)
	}

	// Clear path must still be clean, or the hook blocks everything.
	clear := exec.Command(bin, "check", "src/free.txt")
	clear.Dir = repo
	clear.Env = childEnv(home, "other-agent")
	if out, err := clear.CombinedOutput(); err != nil {
		t.Fatalf("unclaimed path should be clear, got %v: %s", err, out)
	}
}

// A refused claim is the only event that proves comms prevented something. It
// used to be printed to stderr and thrown away, which is why a store of 4,356
// claims could report zero collisions ever prevented.
func TestRefusedClaimIsRecordedInTheLog(t *testing.T) {
	bin := buildCommsBinary(t)
	repo := setupTestRepo(t)
	home := t.TempDir()

	claim := exec.Command(bin, "claim", "src/contested.txt", "--intent", "peer work")
	claim.Dir = repo
	claim.Env = childEnv(home, "peer-agent")
	if out, err := claim.CombinedOutput(); err != nil {
		t.Fatalf("peer claim: %v: %s", err, out)
	}

	blocked := exec.Command(bin, "claim", "src/contested.txt", "--intent", "my work")
	blocked.Dir = repo
	blocked.Env = childEnv(home, "second-agent")
	if out, err := blocked.CombinedOutput(); err == nil {
		t.Fatalf("second claim on a held path should be refused; output:\n%s", out)
	}

	var found map[string]interface{}
	for _, line := range readLogLines(t, home) {
		var e map[string]interface{}
		if json.Unmarshal([]byte(line), &e) != nil {
			continue
		}
		if e["type"] == "blocked" {
			found = e
		}
	}
	if found == nil {
		t.Fatal("no blocked event was written; a prevented collision left no trace")
	}
	if found["actor"] != "second-agent" {
		t.Errorf("blocked event actor = %v, want second-agent", found["actor"])
	}
	data, _ := found["data"].(map[string]interface{})
	if data["holder"] != "peer-agent" {
		t.Errorf("blocked event holder = %v, want peer-agent — without it you cannot tell who you collided with", data["holder"])
	}
	if data["intent"] != "my work" {
		t.Errorf("blocked event intent = %v, want the refused claim's intent", data["intent"])
	}
}

// readLogLines returns every JSONL line of every log under the test HOME.
func readLogLines(t *testing.T, home string) []string {
	t.Helper()
	var out []string
	root := filepath.Join(home, "Library", "Application Support", "comms")
	_ = filepath.Walk(root, func(p string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() || filepath.Base(p) != "log.jsonl" {
			return nil
		}
		b, rerr := os.ReadFile(p)
		if rerr != nil {
			return nil
		}
		for _, l := range strings.Split(string(b), "\n") {
			if strings.TrimSpace(l) != "" {
				out = append(out, l)
			}
		}
		return nil
	})
	if len(out) == 0 {
		t.Fatalf("no log lines found under %s", root)
	}
	return out
}
