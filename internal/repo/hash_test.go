package repo

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func initTestGitRepo(t *testing.T) string {
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

func TestDiscoverFromGitRepo(t *testing.T) {
	dir := initTestGitRepo(t)
	id, err := Discover(dir, "")
	if err != nil {
		t.Fatalf("Discover: %v", err)
	}
	if id.Hash == "" || len(id.Hash) != 12 {
		t.Errorf("hash should be 12 chars, got %q (len %d)", id.Hash, len(id.Hash))
	}
	if id.Root == "" {
		t.Errorf("root should be set")
	}
	if id.Name == "" {
		t.Errorf("name should be set")
	}
}

func TestDiscoverDeterministic(t *testing.T) {
	dir := initTestGitRepo(t)
	a, err := Discover(dir, "")
	if err != nil {
		t.Fatal(err)
	}
	b, err := Discover(dir, "")
	if err != nil {
		t.Fatal(err)
	}
	if a.Hash != b.Hash {
		t.Errorf("hash should be deterministic: %q vs %q", a.Hash, b.Hash)
	}
}

func TestDiscoverWithOverride(t *testing.T) {
	dir := t.TempDir()
	id, err := Discover(dir, dir) // not a git repo, override bypasses git
	if err != nil {
		t.Fatalf("override should bypass git: %v", err)
	}
	if id.Root == "" || id.Hash == "" {
		t.Errorf("expected populated identity, got %+v", id)
	}
}

func TestDiscoverExplicitRepoRootFromSubdir(t *testing.T) {
	dir := initTestGitRepo(t)
	subdir := dir + "/nested/path"
	if err := os.MkdirAll(subdir, 0o755); err != nil {
		t.Fatalf("mkdir subdir: %v", err)
	}

	id, err := DiscoverExplicit(subdir)
	if err != nil {
		t.Fatalf("DiscoverExplicit: %v", err)
	}
	want, err := filepath.EvalSymlinks(dir)
	if err != nil {
		t.Fatalf("eval want root: %v", err)
	}
	if id.Root != want {
		t.Fatalf("root = %q, want %q", id.Root, want)
	}
	if id.Hash == "" || len(id.Hash) != 12 {
		t.Fatalf("hash = %q, want 12 chars", id.Hash)
	}
}

func TestDiscoverExplicitFailsOutsideGit(t *testing.T) {
	_, err := DiscoverExplicit(t.TempDir())
	if err == nil {
		t.Fatal("expected error outside git repo")
	}
}

func TestDiscoverFailsOutsideGit(t *testing.T) {
	dir := t.TempDir()
	_, err := Discover(dir, "")
	if err == nil {
		t.Fatal("expected error for non-git dir without override")
	}
}
