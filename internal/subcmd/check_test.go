package subcmd

import (
	"path/filepath"
	"testing"
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
