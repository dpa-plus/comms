// Package tests is the cross-process integration suite. These tests build
// the `comms` binary to a temp dir and exec it as N concurrent child
// processes, asserting that flock + atomic events keep the log consistent.
package tests

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// buildCommsBinary builds the comms CLI to a unique path under t.TempDir().
// Returns the absolute path. Skips the test if `go` isn't on PATH.
func buildCommsBinary(t *testing.T) string {
	t.Helper()
	if _, err := exec.LookPath("go"); err != nil {
		t.Skipf("go not available: %v", err)
	}
	bin := filepath.Join(t.TempDir(), "comms")
	cmd := exec.Command("go", "build", "-o", bin, "./cmd/comms")
	cmd.Dir = repoRoot(t)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("go build: %v\n%s", err, out)
	}
	return bin
}

func repoRoot(t *testing.T) string {
	t.Helper()
	// tests/ is a direct subdirectory of the repo root.
	cwd, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	return filepath.Dir(cwd)
}

func setupTestRepo(t *testing.T) string {
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

func childEnv(home, actor string) []string {
	env := os.Environ()
	return append(env, "HOME="+home, "COMMS_ACTOR="+actor)
}

// TestConcurrentClaim spawns N processes that all try to claim the same
// scope at the same instant. Exactly ONE must win.
func TestConcurrentClaim(t *testing.T) {
	const N = 5
	bin := buildCommsBinary(t)
	repo := setupTestRepo(t)
	home := t.TempDir()

	var wg sync.WaitGroup
	results := make([]error, N)
	stderrs := make([]string, N)
	startBarrier := time.Now().Add(300 * time.Millisecond)

	for i := 0; i < N; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			time.Sleep(time.Until(startBarrier))
			cmd := exec.Command(bin, "claim", "src/race.ts", "--intent", fmt.Sprintf("agent-%d", idx))
			cmd.Env = childEnv(home, "agent-"+strconv.Itoa(idx))
			cmd.Dir = repo
			out, err := cmd.CombinedOutput()
			results[idx] = err
			stderrs[idx] = string(out)
		}(i)
	}
	wg.Wait()

	zeroCount := 0
	for i, r := range results {
		if r == nil {
			zeroCount++
			t.Logf("agent-%d WON: %s", i, stderrs[i])
		} else {
			t.Logf("agent-%d lost (expected): exit %v", i, r)
		}
	}
	if zeroCount != 1 {
		t.Fatalf("expected exactly 1 winner, got %d", zeroCount)
	}
}

// TestConcurrentDifferentScopes spawns N processes that claim DIFFERENT
// scopes. All N should succeed (no false-positive conflicts).
func TestConcurrentDifferentScopes(t *testing.T) {
	const N = 5
	bin := buildCommsBinary(t)
	repo := setupTestRepo(t)
	home := t.TempDir()

	var wg sync.WaitGroup
	results := make([]error, N)
	startBarrier := time.Now().Add(300 * time.Millisecond)

	for i := 0; i < N; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			time.Sleep(time.Until(startBarrier))
			cmd := exec.Command(bin, "claim", fmt.Sprintf("src/path%d.ts", idx), "--intent", "x")
			cmd.Env = childEnv(home, "agent-"+strconv.Itoa(idx))
			cmd.Dir = repo
			results[idx] = cmd.Run()
		}(i)
	}
	wg.Wait()

	for i, r := range results {
		if r != nil {
			t.Errorf("agent-%d should have succeeded: %v", i, r)
		}
	}
}
