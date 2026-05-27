// Package repo computes the repo-identity hash and bootstraps the
// per-repo `.comms/` directory plus the per-machine log directory.
package repo

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// Identity captures the resolved repo identity.
//
//   - Root is the absolute, symlinks-resolved repo root.
//   - Hash is the first 12 hex chars of sha256(Root), used as the per-machine
//     log-directory key. Renaming/moving the repo changes the hash and
//     orphans the old log (acceptable for MVP).
//   - Name is the basename of Root, used for human-readable output.
type Identity struct {
	Root string
	Hash string
	Name string
}

// Discover resolves the current repo by running `git rev-parse --show-toplevel`
// from `start`. If gitTopLevelOverride is non-empty, it bypasses git entirely
// (useful for tests / non-git directories — see --repo-id flag).
func Discover(start, gitTopLevelOverride string) (Identity, error) {
	var root string
	if gitTopLevelOverride != "" {
		root = gitTopLevelOverride
	} else {
		out, err := runGit(start, "rev-parse", "--show-toplevel")
		if err != nil {
			return Identity{}, fmt.Errorf("repo: cannot find git root from %q: %w (set --repo-id to override)", start, err)
		}
		root = strings.TrimSpace(out)
	}
	abs, err := filepath.Abs(root)
	if err != nil {
		return Identity{}, fmt.Errorf("repo: abs: %w", err)
	}
	resolved, err := filepath.EvalSymlinks(abs)
	if err != nil {
		// On macOS, /tmp resolves to /private/tmp via symlink — that's expected.
		// If EvalSymlinks fails for another reason, fall back to abs.
		if !errors.Is(err, os.ErrNotExist) {
			resolved = abs
		} else {
			return Identity{}, fmt.Errorf("repo: resolve %q: %w", abs, err)
		}
	}
	sum := sha256.Sum256([]byte(resolved))
	return Identity{
		Root: resolved,
		Hash: hex.EncodeToString(sum[:])[:12],
		Name: filepath.Base(resolved),
	}, nil
}

// DiscoverFromCWD is a convenience wrapper.
func DiscoverFromCWD(gitTopLevelOverride string) (Identity, error) {
	cwd, err := os.Getwd()
	if err != nil {
		return Identity{}, fmt.Errorf("repo: getwd: %w", err)
	}
	return Discover(cwd, gitTopLevelOverride)
}

func runGit(dir string, args ...string) (string, error) {
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("git %s: %w (stderr: %s)", strings.Join(args, " "), err, strings.TrimSpace(stderr.String()))
	}
	return stdout.String(), nil
}
