package subcmd

import (
	"fmt"
	"runtime"
	"runtime/debug"

	"github.com/spf13/cobra"
)

// BuildInfo carries the version metadata that identifies a comms binary. The
// fields are injected at build time via -ldflags (see the Makefile and
// .goreleaser.yaml). Resolve fills any that the build did not set from the
// embedded module build info, so `go install`-built binaries — which ignore
// -ldflags — still report a real version and commit instead of "dev".
type BuildInfo struct {
	Version string
	Commit  string
	Date    string
}

// Resolve returns a copy with empty/default fields recovered from
// runtime/debug build info. It is idempotent.
func (b BuildInfo) Resolve() BuildInfo {
	if b.Version == "" || b.Version == "dev" {
		if bi, ok := debug.ReadBuildInfo(); ok {
			if v := bi.Main.Version; v != "" && v != "(devel)" {
				b.Version = v
			}
			for _, s := range bi.Settings {
				switch s.Key {
				case "vcs.revision":
					if b.Commit == "" || b.Commit == "none" {
						b.Commit = shortCommit(s.Value)
					}
				case "vcs.time":
					if b.Date == "" || b.Date == "unknown" {
						b.Date = s.Value
					}
				}
			}
		}
	}
	if b.Version == "" {
		b.Version = "dev"
	}
	if b.Commit == "" {
		b.Commit = "none"
	}
	if b.Date == "" {
		b.Date = "unknown"
	}
	return b
}

func shortCommit(c string) string {
	if len(c) > 12 {
		return c[:12]
	}
	return c
}

// String is the one-line build summary shared by `comms version` and
// `comms --version`.
func (b BuildInfo) String() string {
	return fmt.Sprintf("comms %s (commit %s, built %s, %s %s/%s)",
		b.Version, b.Commit, b.Date, runtime.Version(), runtime.GOOS, runtime.GOARCH)
}

// NewVersionCmd builds `comms version`, the conventional scriptable surface for
// reporting the binary's build metadata (mirrors `comms --version`).
func NewVersionCmd(info BuildInfo) *cobra.Command {
	return &cobra.Command{
		Use:   "version",
		Short: "Print the comms version, commit, and build info",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			fmt.Fprintln(cmd.OutOrStdout(), info.Resolve().String())
			return nil
		},
	}
}
