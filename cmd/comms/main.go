// Command comms is the multi-agent coordination CLI.
//
// See github.com/dpa-plus/comms/docs/README.md for the full picture.
package main

import (
	"fmt"
	"os"

	"github.com/dpa-plus/comms/internal/subcmd"
	"github.com/spf13/cobra"
)

// Build metadata. These are overridden via -ldflags at build time (see the
// Makefile and .goreleaser.yaml, which set -X main.Version / main.Commit /
// main.Date). For `go install` builds — which ignore -ldflags — BuildInfo.Resolve
// recovers the module version and VCS revision from the embedded build info, so a
// released binary never reports a bare "dev".
var (
	Version = "dev"
	Commit  = "none"
	Date    = "unknown"
)

func main() {
	build := subcmd.BuildInfo{Version: Version, Commit: Commit, Date: Date}.Resolve()
	root := &cobra.Command{
		Use:           "comms",
		Short:         "Lightweight multi-agent coordination CLI",
		Long:          `comms coordinates parallel coding sessions via per-session exclusive claims, a JSONL event log, and a small docs wiki under .comms/`,
		SilenceUsage:  true,
		SilenceErrors: true,
		Version:       build.String(),
	}
	// Print the full build line for `--version` (the default template would prefix
	// "comms version", duplicating what build.String() already carries).
	root.SetVersionTemplate("{{.Version}}\n")
	subcmd.AddGlobalFlags(root)
	root.AddCommand(
		subcmd.NewHelloCmd(),
		subcmd.NewClaimCmd(),
		subcmd.NewReleaseCmd(),
		subcmd.NewCheckCmd(),
		subcmd.NewStatusCmd(),
		subcmd.NewLogCmd(),
		subcmd.NewNoteCmd(),
		subcmd.NewFindCmd(),
		subcmd.NewDocCmd(),
		subcmd.NewLessonCmd(),
		subcmd.NewSessionCmd(),
		subcmd.NewUICmd(),
		subcmd.NewVersionCmd(build),
	)
	// Hidden helpers used by tests and the SessionStart hook.
	root.AddCommand(subcmd.NewRepoHashCmd())

	if err := root.Execute(); err != nil {
		fmt.Fprintf(os.Stderr, "comms: %v\n", err)
		os.Exit(2)
	}
}
