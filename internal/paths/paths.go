// Package paths centralizes filesystem locations.
//
// Two distinct trees:
//   - Per-repo (committed): <repo>/.comms/{policy.txt, docs/, .gitignore}
//   - Per-machine (NOT committed, outside iCloud):
//     ~/Library/Application Support/comms/<repo-hash>/{log.jsonl, .lock, repo-path.txt}
//
// We intentionally use ~/Library/Application Support on macOS (not ~/Library/
// Mobile Documents) so concurrent appenders don't fork the JSONL file the way
// iCloud Drive does.
package paths

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
)

// Paths bundles all the locations a single comms invocation cares about.
type Paths struct {
	Repo     string // absolute path to the repo root
	RepoHash string // 12-char hex hash identifying this repo
	Comms    string // <repo>/.comms/
	Policy   string // <repo>/.comms/policy.txt
	Docs     string // <repo>/.comms/docs/
	LogDir   string // ~/Library/Application Support/comms/<hash>/
	Log      string // <logdir>/log.jsonl
	Lock     string // <logdir>/.lock
	RepoPath string // <logdir>/repo-path.txt — collision canary for --repo-id
}

// For computes all paths for the given repo root + hash.
//
// On macOS we use ~/Library/Application Support. On Linux/BSD we use
// $XDG_DATA_HOME or ~/.local/share. Windows is not supported in MVP.
func For(repoRoot, repoHash string) (Paths, error) {
	if !filepath.IsAbs(repoRoot) {
		return Paths{}, fmt.Errorf("paths: repoRoot must be absolute, got %q", repoRoot)
	}
	dataHome, err := userDataHome()
	if err != nil {
		return Paths{}, err
	}
	logDir := filepath.Join(dataHome, "comms", repoHash)
	commsDir := filepath.Join(repoRoot, ".comms")
	return Paths{
		Repo:     repoRoot,
		RepoHash: repoHash,
		Comms:    commsDir,
		Policy:   filepath.Join(commsDir, "policy.txt"),
		Docs:     filepath.Join(commsDir, "docs"),
		LogDir:   logDir,
		Log:      filepath.Join(logDir, "log.jsonl"),
		Lock:     filepath.Join(logDir, ".lock"),
		RepoPath: filepath.Join(logDir, "repo-path.txt"),
	}, nil
}

// DocLockPath returns the sidecar editor-lock path for `comms doc <slug> --edit`.
//
// The file lives at .comms/docs/.<slug>.lock so it's adjacent to the doc but
// hidden from casual listing.
func (p Paths) DocLockPath(slug string) string {
	return filepath.Join(p.Docs, "."+slug+".lock")
}

// DocFilePath returns the markdown file path for a given slug.
func (p Paths) DocFilePath(slug string) string {
	return filepath.Join(p.Docs, slug+".md")
}

func userDataHome() (string, error) {
	switch runtime.GOOS {
	case "darwin":
		home, err := os.UserHomeDir()
		if err != nil {
			return "", fmt.Errorf("paths: user home: %w", err)
		}
		return filepath.Join(home, "Library", "Application Support"), nil
	case "linux", "freebsd", "netbsd", "openbsd":
		if xdg := os.Getenv("XDG_DATA_HOME"); xdg != "" {
			return xdg, nil
		}
		home, err := os.UserHomeDir()
		if err != nil {
			return "", fmt.Errorf("paths: user home: %w", err)
		}
		return filepath.Join(home, ".local", "share"), nil
	default:
		return "", fmt.Errorf("paths: unsupported GOOS %q (MVP supports darwin + linux)", runtime.GOOS)
	}
}
