package repo

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"github.com/dpa-plus/comms/internal/paths"
)

// Bootstrap ensures every required file and directory exists for the given
// paths set. It is safe to call concurrently from multiple processes: the
// per-machine .lock file is created first (used by callers to serialize log
// writes), and per-repo files use O_EXCL so racing creators don't clobber.
//
// Bootstrap is idempotent — calling it on an already-set-up repo is a no-op.
func Bootstrap(p paths.Paths) error {
	// 1. Per-machine log dir + repo-path canary (outside iCloud).
	if err := os.MkdirAll(p.LogDir, 0o700); err != nil {
		return fmt.Errorf("bootstrap: mkdir %s: %w", p.LogDir, err)
	}
	if err := writeIfAbsent(p.RepoPath, []byte(p.Repo+"\n"), 0o600); err != nil {
		return err
	}

	// 2. Per-repo .comms tree (committed).
	if err := os.MkdirAll(p.Docs, 0o755); err != nil {
		return fmt.Errorf("bootstrap: mkdir %s: %w", p.Docs, err)
	}
	if err := writeIfAbsent(p.Policy, policyTemplate(), 0o644); err != nil {
		return err
	}
	gitignore := filepath.Join(p.Comms, ".gitignore")
	if err := writeIfAbsent(gitignore, gitignoreContent(), 0o644); err != nil {
		return err
	}
	return nil
}

// writeIfAbsent creates path with the given content + mode if and only if it
// does not already exist. Uses O_EXCL so two racing bootstrappers can't
// clobber each other's writes.
func writeIfAbsent(path string, content []byte, mode os.FileMode) error {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_EXCL, mode)
	if err != nil {
		if errors.Is(err, os.ErrExist) {
			return nil
		}
		return fmt.Errorf("bootstrap: create %s: %w", path, err)
	}
	defer f.Close()
	if _, err := f.Write(content); err != nil {
		return fmt.Errorf("bootstrap: write %s: %w", path, err)
	}
	return nil
}

func policyTemplate() []byte {
	return []byte(`# .comms/policy.txt — files where a whole-file claim is too coarse.
#
# One path per line. Listed files require an explicit anchor (line range like
# L10-L50 or a symbol name like User) on claims; bare path claims will be
# refused with "anchor required for risky file".
#
# Lines starting with # are comments. Paths are repo-relative POSIX.
#
# Examples:
#   prisma/schema.prisma
#   src/lib/aggregate.ts
#   .github/workflows/deploy.yml

`)
}

func gitignoreContent() []byte {
	return []byte(`# Sidecar editor locks from ` + "`comms doc <slug> --edit`" + `.
# These are transient flock targets, not content.
docs/.*.lock
`)
}
