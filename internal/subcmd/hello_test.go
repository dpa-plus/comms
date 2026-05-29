package subcmd

import (
	"path/filepath"
	"testing"
)

func TestRunHelloKeepsLeaderOnReentry(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "codex-1")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	if err := runHello(nil, "Codex Dev"); err != nil {
		t.Fatalf("first hello: %v", err)
	}
	if err := runHello(nil, "Codex Dev"); err != nil {
		t.Fatalf("second hello: %v", err)
	}

	rt, err := Open(OpenOpts{Mutating: false})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	defer rt.Close()

	leader := activeLeaderActor(rt.State, rt.Events[0].TS.Add(-1))
	if leader != "codex-1" {
		t.Fatalf("leader after re-entry = %q, want codex-1", leader)
	}
	if got := rt.State.Sessions["codex-1"].Label; got != "Codex Dev" {
		t.Fatalf("label = %q, want Codex Dev", got)
	}
}

func TestOpenWithExplicitRepoRootWorksOutsideRepo(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "codex-1")
	t.Setenv("USER", "eli")
	t.Chdir(t.TempDir())

	rt, err := Open(OpenOpts{Mutating: true, RepoRootOverride: repo})
	if err != nil {
		t.Fatalf("open runtime with repo root override: %v", err)
	}
	defer rt.Close()

	want, err := filepath.EvalSymlinks(repo)
	if err != nil {
		t.Fatalf("eval want root: %v", err)
	}
	if rt.Repo.Root != want {
		t.Fatalf("repo root = %q, want %q", rt.Repo.Root, want)
	}
}
