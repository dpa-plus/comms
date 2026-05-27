package subcmd

import (
	"fmt"

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
			id, err := repo.DiscoverFromCWD("")
			if err != nil {
				return err
			}
			fmt.Println(id.Hash)
			return nil
		},
	}
	return cmd
}
