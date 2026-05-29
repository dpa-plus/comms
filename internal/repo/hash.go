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
	"runtime"
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
// from `start`. If gitTopLevelOverride is non-empty, it bypasses git entirely.
func Discover(start, gitTopLevelOverride string) (Identity, error) {
	var root string
	if gitTopLevelOverride != "" {
		root = gitTopLevelOverride
	} else {
		out, err := runGit(start, "rev-parse", "--show-toplevel")
		if err != nil {
			return Identity{}, fmt.Errorf("repo: cannot find git root from %q: %w (use --repo /absolute/repo/path or COMMS_REPO to target a repo explicitly)", start, err)
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
		if errors.Is(err, os.ErrNotExist) {
			return Identity{}, fmt.Errorf("repo: resolve %q: %w", abs, err)
		}
		// A non-ENOENT failure (EPERM/EACCES on a TCC-protected leaf) must not
		// change the hash: falling back to the unresolved `abs` would hash a
		// different string than a successful resolve whenever any component is
		// a symlink, orphaning the per-machine log. Instead resolve the deepest
		// reachable ancestor and re-append the unresolved tail, so the hashed
		// path is identical whether or not the leaf is reachable.
		resolved = resolveDeepestAncestor(abs)
	}
	sum := sha256.Sum256([]byte(resolved))
	return Identity{
		Root: resolved,
		Hash: hex.EncodeToString(sum[:])[:12],
		Name: filepath.Base(resolved),
	}, nil
}

// DiscoverExplicit resolves a user-supplied repo path without depending on the
// process current working directory or spawning git. This is useful on macOS
// when a desktop app process loses TCC access to its cwd but can still read an
// absolute repo path.
func DiscoverExplicit(path string) (Identity, error) {
	root, err := findGitRootByWalking(path)
	if err != nil {
		return Identity{}, err
	}
	return Discover(root, root)
}

// DiscoverFromCWD is a convenience wrapper.
func DiscoverFromCWD(gitTopLevelOverride string) (Identity, error) {
	cwd, err := os.Getwd()
	if err != nil {
		return Identity{}, fmt.Errorf("repo: getwd: %w%s", err, cwdRecoveryHint())
	}
	return Discover(cwd, gitTopLevelOverride)
}

func findGitRootByWalking(path string) (string, error) {
	if strings.TrimSpace(path) == "" {
		return "", fmt.Errorf("repo: --repo path is required")
	}
	// This is the macOS-TCC escape hatch (see DiscoverExplicit): it must work
	// without depending on the process cwd. filepath.Abs calls os.Getwd for any
	// relative input, which is exactly what fails under a lost-TCC cwd. Require
	// an absolute path so a relative --repo/COMMS_REPO can't defeat the hatch.
	if !filepath.IsAbs(path) {
		return "", fmt.Errorf("repo: --repo/COMMS_REPO must be an absolute path, got %q%s", path, cwdRecoveryHint())
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", fmt.Errorf("repo: abs %q: %w", path, err)
	}
	info, err := os.Stat(abs)
	if err != nil {
		return "", fmt.Errorf("repo: stat %q: %w", abs, err)
	}
	dir := abs
	if !info.IsDir() {
		dir = filepath.Dir(abs)
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, ".git")); err == nil {
			return dir, nil
		} else if !errors.Is(err, os.ErrNotExist) {
			return "", fmt.Errorf("repo: stat %q: %w", filepath.Join(dir, ".git"), err)
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	return "", fmt.Errorf("repo: cannot find git root from explicit path %q", abs)
}

// resolveDeepestAncestor walks up from abs until filepath.EvalSymlinks
// succeeds on an ancestor, then re-attaches the unresolved tail. This mirrors
// resolveExistingAncestor in internal/subcmd/check.go: it makes the resulting
// path identical to a fully-successful EvalSymlinks(abs) whenever the only
// obstacle is an unresolvable leaf (e.g. an EPERM/EACCES TCC-protected final
// component), keeping the repo hash deterministic. abs must be absolute.
func resolveDeepestAncestor(abs string) string {
	cur := abs
	var tail []string
	for {
		if resolved, err := filepath.EvalSymlinks(cur); err == nil {
			cur = resolved
			break
		}
		parent := filepath.Dir(cur)
		if parent == cur {
			// Reached the filesystem root with nothing resolvable; fall back to
			// the unresolved absolute path (the best we can do deterministically).
			return abs
		}
		tail = append([]string{filepath.Base(cur)}, tail...)
		cur = parent
	}
	if len(tail) == 0 {
		return cur
	}
	return filepath.Join(append([]string{cur}, tail...)...)
}

func cwdRecoveryHint() string {
	if runtime.GOOS != "darwin" {
		return " (try `comms --repo /absolute/repo/path ...` from a readable directory)"
	}
	return " (on macOS this can happen when a desktop app loses Privacy & Security access to Desktop, Documents, or Downloads; try `comms --repo /absolute/repo/path ...` from a readable directory, move the repo outside protected folders such as ~/code, or grant Full Disk Access and restart the app)"
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
