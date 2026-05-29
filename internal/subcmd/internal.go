package subcmd

import (
	"fmt"
	"os"

	"github.com/dpa-plus/comms/internal/repo"
	"github.com/spf13/cobra"
)

// NewRepoHashCmd is a hidden helper used by verification scripts to print the
// repo hash (so they can `ls ~/Library/Application Support/comms/<hash>/`).
func NewRepoHashCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:    "_repo-hash",
		Short:  "Print the repo's 12-char identity hash",
		Hidden: true,
		Args:   cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := repoIdentityForHelper()
			if err != nil {
				return err
			}
			fmt.Println(id.Hash)
			return nil
		},
	}
	return cmd
}

func repoIdentityForHelper() (repo.Identity, error) {
	if globalRepoRoot != "" {
		return repo.DiscoverExplicit(globalRepoRoot)
	}
	if envRepo := os.Getenv("COMMS_REPO"); envRepo != "" {
		return repo.DiscoverExplicit(envRepo)
	}
	if globalRepoID != "" {
		return repo.Identity{}, fmt.Errorf("--repo-id is no longer used for repo selection; use --repo /absolute/repo/path instead")
	}
	return repo.DiscoverFromCWD("")
}
