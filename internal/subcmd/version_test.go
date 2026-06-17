package subcmd

import (
	"bytes"
	"io"
	"runtime"
	"strings"
	"testing"
)

// The version command must surface the build metadata injected at release time
// — without it, `comms version` on a real release would be useless for bug
// reports. Lock the fields (version/commit/date) plus the runtime Go version.
func TestVersionCmdPrintsInjectedBuildInfo(t *testing.T) {
	info := BuildInfo{Version: "1.2.3", Commit: "abc1234def0", Date: "2026-01-01T00:00:00Z"}
	cmd := NewVersionCmd(info)
	var buf bytes.Buffer
	cmd.SetOut(&buf)
	if err := cmd.Execute(); err != nil {
		t.Fatalf("version cmd: %v", err)
	}
	out := buf.String()
	for _, want := range []string{"1.2.3", "abc1234def0", "2026-01-01T00:00:00Z", runtime.Version(), runtime.GOOS} {
		if !strings.Contains(out, want) {
			t.Errorf("version output missing %q; got %q", want, out)
		}
	}
}

// `comms version` takes no positional args; a stray one is a usage error.
func TestVersionCmdRejectsArgs(t *testing.T) {
	cmd := NewVersionCmd(BuildInfo{Version: "1.2.3"})
	cmd.SetArgs([]string{"extra"})
	cmd.SetOut(io.Discard)
	cmd.SetErr(io.Discard)
	if err := cmd.Execute(); err == nil {
		t.Fatal("expected an error for a stray positional arg")
	}
}

// Resolve must always produce non-empty fields and be idempotent, so the
// version line never prints blanks even on a bare `go build` with no -ldflags.
func TestBuildInfoResolveFillsDefaults(t *testing.T) {
	got := BuildInfo{Version: "0.9.0", Commit: "deadbeef0000", Date: "2026-02-02"}.Resolve()
	if got.Version != "0.9.0" || got.Commit != "deadbeef0000" || got.Date != "2026-02-02" {
		t.Fatalf("Resolve mutated explicit fields: %+v", got)
	}
	if again := got.Resolve(); again != got {
		t.Fatalf("Resolve not idempotent: %+v vs %+v", got, again)
	}
	empty := BuildInfo{}.Resolve()
	if empty.Version == "" || empty.Commit == "" || empty.Date == "" {
		t.Fatalf("Resolve left a blank field: %+v", empty)
	}
}
