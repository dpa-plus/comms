package subcmd

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"unicode/utf8"
)

func TestSplitEditorSpecHonorsQuotedPathWithSpaces(t *testing.T) {
	// A binary path containing spaces, quoted, followed by a flag.
	spec := `"/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code" --wait`
	got, err := splitEditorSpec(spec)
	if err != nil {
		t.Fatalf("splitEditorSpec: %v", err)
	}
	want := []string{
		"/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
		"--wait",
	}
	if strings.Join(got, "\x00") != strings.Join(want, "\x00") {
		t.Fatalf("tokens = %#v, want %#v", got, want)
	}
}

func TestSplitEditorSpecSimpleSpecs(t *testing.T) {
	cases := []struct {
		spec string
		want []string
	}{
		{"vim", []string{"vim"}},
		{"code --wait", []string{"code", "--wait"}},
		{"  code   --wait  ", []string{"code", "--wait"}},
		{`'my editor' --flag`, []string{"my editor", "--flag"}},
	}
	for _, c := range cases {
		got, err := splitEditorSpec(c.spec)
		if err != nil {
			t.Fatalf("splitEditorSpec(%q): %v", c.spec, err)
		}
		if strings.Join(got, "\x00") != strings.Join(c.want, "\x00") {
			t.Fatalf("splitEditorSpec(%q) = %#v, want %#v", c.spec, got, c.want)
		}
	}
}

func TestSplitEditorSpecUnterminatedQuote(t *testing.T) {
	if _, err := splitEditorSpec(`"/path/with space/code --wait`); err == nil {
		t.Fatalf("unterminated quote should error")
	}
}

// TestNewEditorCommandQuotedPathWithSpaces is an end-to-end check that a quoted
// binary path containing spaces is resolved as a single executable plus args.
func TestNewEditorCommandQuotedPathWithSpaces(t *testing.T) {
	dir := t.TempDir()
	binDir := filepath.Join(dir, "Code App With Spaces")
	if err := os.MkdirAll(binDir, 0o755); err != nil {
		t.Fatalf("mkdir bin: %v", err)
	}
	editor := filepath.Join(binDir, "code")
	if err := os.WriteFile(editor, []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
		t.Fatalf("write editor: %v", err)
	}
	t.Setenv("VISUAL", "")
	t.Setenv("EDITOR", `"`+editor+`" --wait`)

	cmd, err := newEditorCommand("/tmp/example.md")
	if err != nil {
		t.Fatalf("newEditorCommand: %v", err)
	}
	if cmd.Path != editor {
		t.Fatalf("editor executable = %q, want %q", cmd.Path, editor)
	}
	want := []string{"--wait", "/tmp/example.md"}
	if strings.Join(cmd.Args[1:], "\x00") != strings.Join(want, "\x00") {
		t.Fatalf("editor args = %#v, want %#v", cmd.Args[1:], want)
	}
}

// TestFirstSummaryLineTruncatesOnRuneBoundary verifies that a long summary line
// containing multi-byte UTF-8 runes is truncated on a rune boundary, so the hint
// shown by `comms doc --list` / `comms lesson --list` never emits invalid UTF-8.
func TestFirstSummaryLineTruncatesOnRuneBoundary(t *testing.T) {
	// Each "é" is 2 bytes. A line of 80 of them is 80 runes / 160 bytes — well
	// over the 70-rune cap, and byte-slicing at [:67] would land mid-rune.
	line := strings.Repeat("é", 80)
	dir := t.TempDir()
	path := filepath.Join(dir, "lesson.md")
	if err := os.WriteFile(path, []byte(line+"\n"), 0o644); err != nil {
		t.Fatalf("write doc: %v", err)
	}

	got := firstSummaryLine(path)

	if !utf8.ValidString(got) {
		t.Fatalf("summary is not valid UTF-8: %q", got)
	}
	if !strings.HasSuffix(got, "...") {
		t.Fatalf("expected truncated summary to end with ellipsis, got %q", got)
	}
	// 67 runes of content + the 3-char "..." ellipsis = 70 runes.
	if n := utf8.RuneCountInString(got); n != 70 {
		t.Fatalf("truncated summary rune count = %d, want 70 (67 + \"...\")", n)
	}
	if want := strings.Repeat("é", 67) + "..."; got != want {
		t.Fatalf("summary = %q, want %q", got, want)
	}
}

// TestFirstSummaryLineShortMultiByteLineUnchanged ensures a short multi-byte
// line (at/under the cap) is returned verbatim.
func TestFirstSummaryLineShortMultiByteLineUnchanged(t *testing.T) {
	line := "café résumé naïve" // multi-byte but well under 70 runes
	dir := t.TempDir()
	path := filepath.Join(dir, "lesson.md")
	if err := os.WriteFile(path, []byte(line+"\n"), 0o644); err != nil {
		t.Fatalf("write doc: %v", err)
	}
	if got := firstSummaryLine(path); got != line {
		t.Fatalf("summary = %q, want unchanged %q", got, line)
	}
}

func TestParseDurationRejectsNegative(t *testing.T) {
	if _, err := parseDuration("-1h"); err == nil || !strings.Contains(err.Error(), "negative") {
		t.Fatalf("negative --since should be rejected, got %v", err)
	}
	// Zero is allowed.
	if d, err := parseDuration("0s"); err != nil || d != 0 {
		t.Fatalf("zero --since: d=%v err=%v, want 0/nil", d, err)
	}
	// Positive durations still work for every caller's parser.
	if d, err := parseDuration("24h"); err != nil || d <= 0 {
		t.Fatalf("positive --since: d=%v err=%v, want >0/nil", d, err)
	}
}
