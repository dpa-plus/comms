package subcmd

import (
	"path/filepath"
	"strings"
	"testing"
)

func TestValidateLabel(t *testing.T) {
	tests := []struct {
		name    string
		label   string
		wantErr bool
	}{
		{"empty ok", "", false},
		{"plain ok", "Claude Dev", false},
		{"unicode ok", "Cläude — Dev ✨", false},
		{"max length ok", strings.Repeat("x", maxLabelRunes), false},
		{"newline rejected", "Claude\nFAKE: line", true},
		{"carriage return rejected", "Claude\rDev", true},
		{"escape rejected", "Claude\x1b[31mDev", true},
		{"del rejected", "Claude\x7fDev", true},
		{"too long rejected", strings.Repeat("x", maxLabelRunes+1), true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := validateLabel(tt.label)
			if (err != nil) != tt.wantErr {
				t.Fatalf("validateLabel(%q) error = %v, wantErr %v", tt.label, err, tt.wantErr)
			}
		})
	}
}

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
