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

// Version is overridden via -ldflags at build time; left as "dev" in source.
var Version = "dev"

func main() {
	root := &cobra.Command{
		Use:           "comms",
		Short:         "Lightweight multi-agent coordination CLI",
		Long:          `comms coordinates parallel coding sessions via per-session exclusive claims, a JSONL event log, and a small docs wiki under .comms/`,
		SilenceUsage:  true,
		SilenceErrors: true,
		Version:       Version,
	}
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
	)
	// Hidden helpers used by tests and the SessionStart hook.
	root.AddCommand(subcmd.NewRepoHashCmd())

	if err := root.Execute(); err != nil {
		fmt.Fprintf(os.Stderr, "comms: %v\n", err)
		os.Exit(2)
	}
}
