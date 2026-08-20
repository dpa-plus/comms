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
	// Walk from HOME, not from a hardcoded store path: comms uses
	// ~/Library/Application Support on macOS and $XDG_DATA_HOME (or
	// ~/.local/share) on Linux, so naming either one makes this test pass on one
	// CI runner and fail on the other.
	root := home
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

// A finding only ever resurfaces through its path refs: claiming a file prints
// prior findings on that path, and that is the only automatic read path in the
// tool. On the real store 415 findings were filed while their author held an
// open claim and carried no path ref at all — comms knew the file and discarded
// it, so those findings were written once and never seen again.
func TestFindingIsAnchoredToTheClaimTheAuthorHolds(t *testing.T) {
	bin := buildCommsBinary(t)
	repo := setupTestRepo(t)
	home := t.TempDir()

	claim := exec.Command(bin, "claim", "src/billing.ts", "--intent", "vat rounding")
	claim.Dir = repo
	claim.Env = childEnv(home, "author-agent")
	if out, err := claim.CombinedOutput(); err != nil {
		t.Fatalf("claim: %v: %s", err, out)
	}

	// No --ref at all: exactly how README and the skill teach it.
	find := exec.Command(bin, "find", "gotcha", "rounding must happen after summing, not per line")
	find.Dir = repo
	find.Env = childEnv(home, "author-agent")
	if out, err := find.CombinedOutput(); err != nil {
		t.Fatalf("find: %v: %s", err, out)
	}

	release := exec.Command(bin, "release", "--all-mine", "--result", "done")
	release.Dir = repo
	release.Env = childEnv(home, "author-agent")
	if out, err := release.CombinedOutput(); err != nil {
		t.Fatalf("release: %v: %s", err, out)
	}

	// A different agent claiming that file must be shown the gotcha.
	next := exec.Command(bin, "claim", "src/billing.ts", "--intent", "add a currency")
	next.Dir = repo
	next.Env = childEnv(home, "next-agent")
	out, err := next.CombinedOutput()
	if err != nil {
		t.Fatalf("second claim: %v: %s", err, out)
	}
	if !strings.Contains(string(out), "rounding must happen after summing") {
		t.Fatalf("the prior gotcha did not resurface on the file it was about; got:\n%s", out)
	}
}

// An explicit anchor must always win over the guess.
func TestExplicitPathRefIsNotOverriddenByTheHeldClaim(t *testing.T) {
	bin := buildCommsBinary(t)
	repo := setupTestRepo(t)
	home := t.TempDir()

	claim := exec.Command(bin, "claim", "src/held.ts", "--intent", "work")
	claim.Dir = repo
	claim.Env = childEnv(home, "author-agent")
	if out, err := claim.CombinedOutput(); err != nil {
		t.Fatalf("claim: %v: %s", err, out)
	}
	find := exec.Command(bin, "find", "decision", "the real subject is elsewhere", "--ref", "path:src/other.ts")
	find.Dir = repo
	find.Env = childEnv(home, "author-agent")
	if out, err := find.CombinedOutput(); err != nil {
		t.Fatalf("find: %v: %s", err, out)
	}

	for _, line := range readLogLines(t, home) {
		var e map[string]interface{}
		if json.Unmarshal([]byte(line), &e) != nil || e["type"] != "finding" {
			continue
		}
		data, _ := e["data"].(map[string]interface{})
		refs, _ := data["refs"].([]interface{})
		var paths []string
		for _, r := range refs {
			m, _ := r.(map[string]interface{})
			if m["kind"] == "path" {
				paths = append(paths, m["value"].(string))
			}
		}
		if len(paths) != 1 || paths[0] != "src/other.ts" {
			t.Fatalf("explicit ref should be the only path ref, got %v", paths)
		}
		return
	}
	t.Fatal("no finding event found")
}

// A hook does not run where the file lives. Claude Code's working directory is
// wherever the session started — routinely a parent folder holding several
// checkouts, and very often not a repository at all. Resolving the repository
// from the process meant the hook failed on every edit whenever that was true,
// and because "no repository" exited 2, that failure BLOCKED the edit. Installing
// the hook made editing anything outside the session's own checkout impossible.
func TestHookResolvesTheRepoFromTheFileNotTheProcess(t *testing.T) {
	bin := buildCommsBinary(t)
	repo := setupTestRepo(t)
	home := t.TempDir()
	outside := t.TempDir() // a directory that is NOT a repository

	claim := exec.Command(bin, "claim", "src/held.txt", "--intent", "peer work")
	claim.Dir = repo
	claim.Env = childEnv(home, "peer-agent")
	if out, err := claim.CombinedOutput(); err != nil {
		t.Fatalf("claim: %v: %s", err, out)
	}

	payload := func(p string) string {
		return `{"session_id":"other","tool_name":"Edit","tool_input":{"file_path":"` + p + `"}}`
	}
	run := func(cwd, body string) int {
		c := exec.Command(bin, "check", "--stdin-json")
		c.Dir = cwd
		c.Env = childEnv(home, "")
		c.Stdin = strings.NewReader(body)
		if err := c.Run(); err != nil {
			if ee, ok := err.(*exec.ExitError); ok {
				return ee.ExitCode()
			}
			t.Fatalf("run: %v", err)
		}
		return 0
	}

	// The whole point: cwd is outside any repository, the file is inside one.
	if got := run(outside, payload(filepath.Join(repo, "src", "held.txt"))); got != 2 {
		t.Errorf("peer-held file with cwd outside the repo: exit %d, want 2 (blocked)", got)
	}
	if got := run(outside, payload(filepath.Join(repo, "src", "free.txt"))); got != 0 {
		t.Errorf("unclaimed file with cwd outside the repo: exit %d, want 0", got)
	}
}

// "No repository" is not "I cannot tell whether this is clear" — it is "there is
// nothing here to coordinate", and the only safe answer is to allow. Exiting 2
// made every edit outside a checkout impossible, including creating a new file in
// a directory that does not exist yet, which is what the editing tool does
// constantly.
func TestHookAllowsEditsWhereThereIsNoRepositoryToCoordinate(t *testing.T) {
	bin := buildCommsBinary(t)
	home := t.TempDir()
	plain := t.TempDir()

	for _, tc := range []struct{ name, path string }{
		{"existing directory, no repo", filepath.Join(plain, "f.txt")},
		{"new file in directories that do not exist yet", filepath.Join(plain, "a", "b", "c", "f.txt")},
	} {
		c := exec.Command(bin, "check", "--stdin-json")
		c.Dir = plain
		c.Env = childEnv(home, "")
		c.Stdin = strings.NewReader(
			`{"session_id":"x","tool_name":"Write","tool_input":{"file_path":"` + tc.path + `"}}`)
		if err := c.Run(); err != nil {
			code := -1
			if ee, ok := err.(*exec.ExitError); ok {
				code = ee.ExitCode()
			}
			t.Errorf("%s: exit %d, want 0 — a hook that blocks here makes the tool unusable", tc.name, code)
		}
	}
}
