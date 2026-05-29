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

// DiscoverExplicit is the macOS-TCC escape hatch and must not depend on the
// process cwd. A relative --repo/COMMS_REPO would force filepath.Abs ->
// os.Getwd, defeating the hatch, so it's rejected up front with a recovery hint.
func TestDiscoverExplicitRejectsRelativePath(t *testing.T) {
	_, err := DiscoverExplicit("relative/repo/path")
	if err == nil {
		t.Fatal("expected error for relative --repo path")
	}
	if !strings.Contains(err.Error(), "absolute path") {
		t.Fatalf("error should explain the path must be absolute, got: %v", err)
	}
	if !strings.Contains(err.Error(), "--repo") {
		t.Fatalf("error should mention --repo/COMMS_REPO, got: %v", err)
	}
}

// The repo hash must be identical whether or not the leaf component of the
// repo path is reachable via EvalSymlinks. resolveDeepestAncestor backs that
// guarantee: resolving the deepest reachable ancestor and re-appending the
// unresolved tail yields the same canonical path a full EvalSymlinks would,
// so a permission-class (EPERM/EACCES) failure on the leaf can't fork the log.
func TestResolveDeepestAncestorMatchesFullResolve(t *testing.T) {
	// real/ is the canonical dir; link/ is a symlink to it. A path through the
	// symlink must canonicalize to the same place regardless of which ancestor
	// we manage to resolve.
	base := t.TempDir()
	real := filepath.Join(base, "real")
	if err := os.MkdirAll(filepath.Join(real, "sub"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	link := filepath.Join(base, "link")
	if err := os.Symlink(real, link); err != nil {
		t.Skipf("symlinks unsupported: %v", err)
	}

	viaLink := filepath.Join(link, "sub")

	// Sanity: a successful full resolve dereferences the symlink.
	full, err := filepath.EvalSymlinks(viaLink)
	if err != nil {
		t.Fatalf("EvalSymlinks: %v", err)
	}

	// Leaf reachable: resolveDeepestAncestor must equal the full resolve.
	if got := resolveDeepestAncestor(viaLink); got != full {
		t.Fatalf("reachable leaf: resolveDeepestAncestor = %q, want %q", got, full)
	}

	// Leaf NOT present (simulates an unresolvable/permission-blocked tail): the
	// deepest resolvable ancestor (link -> real) plus the re-appended tail must
	// still match the canonicalized path, i.e. the hash stays stable.
	missingLeaf := filepath.Join(viaLink, "does-not-exist-yet")
	wantMissing := filepath.Join(full, "does-not-exist-yet")
	if got := resolveDeepestAncestor(missingLeaf); got != wantMissing {
		t.Fatalf("unreachable leaf: resolveDeepestAncestor = %q, want %q", got, wantMissing)
	}
}

func TestDiscoverFailsOutsideGit(t *testing.T) {
	dir := t.TempDir()
	_, err := Discover(dir, "")
	if err == nil {
		t.Fatal("expected error for non-git dir without override")
	}
	if strings.Contains(err.Error(), "--repo-id") {
		t.Fatalf("error should not recommend deprecated --repo-id: %v", err)
	}
	if !strings.Contains(err.Error(), "--repo") {
		t.Fatalf("error should recommend --repo: %v", err)
	}
}
