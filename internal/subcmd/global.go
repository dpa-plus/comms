package subcmd

import "github.com/spf13/cobra"

var (
	globalRepoRoot string
	globalRepoID   string
)

// AddGlobalFlags registers flags shared by all CLI subcommands.
func AddGlobalFlags(root *cobra.Command) {
	root.PersistentFlags().StringVar(&globalRepoRoot, "repo", "", "explicit repo path; avoids cwd/git discovery when macOS blocks the current directory")
	root.PersistentFlags().StringVar(&globalRepoID, "repo-id", "", "deprecated; use --repo /absolute/repo/path")
	_ = root.PersistentFlags().MarkDeprecated("repo-id", "use --repo /absolute/repo/path")
}
