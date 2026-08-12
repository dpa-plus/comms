package subcmd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"

	"github.com/dpa-plus/comms/internal/actor"
	"github.com/dpa-plus/comms/internal/overlap"
	"github.com/dpa-plus/comms/internal/render"
	"github.com/dpa-plus/comms/internal/state"
	"github.com/spf13/cobra"
)

// NewCheckCmd builds `comms check`. Used by Claude Code's PreToolUse hook;
// returns:
//
//	0 — path clear (or held by same actor)
//	1 — blocked by another actor's active claim
//	2 — system error (broken log, unreadable dir)
//
// In --stdin-json mode the path is extracted from the JSON Claude Code sends
// on the hook stdin. Otherwise the path is the positional argument.
func NewCheckCmd() *cobra.Command {
	var (
		stdinJSON bool
		staged    bool
	)
	cmd := &cobra.Command{
		Use:   "check <path> | check --staged",
		Short: "Check paths for claims held by another actor",
		Long: `Check whether a path is currently claimed by an actor OTHER than the
caller.

Use --staged immediately before committing to check every path in the Git index.
If a staged path is claimed by another actor, comms reports every conflict and
prints exact commands to unstage those paths without discarding their changes.

Exit codes:
  0 — path clear, or held by same actor
  1 — blocked (stderr contains structured conflict info)
  2 — system error (broken log, etc.)

Use --stdin-json to read Claude Code's PreToolUse JSON payload from stdin
instead of taking a positional path argument.`,
		Args: cobra.MaximumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runCheck(args, stdinJSON, staged)
		},
	}
	cmd.Flags().BoolVar(&stdinJSON, "stdin-json", false, "read tool_input.file_path from JSON stdin (PreToolUse hook mode)")
	cmd.Flags().BoolVar(&staged, "staged", false, "check every path staged in Git before committing")
	return cmd
}

func runCheck(args []string, stdinJSON, staged bool) error {
	if staged && (stdinJSON || len(args) > 0) {
		Fatalf(2, "check: --staged cannot be combined with a path or --stdin-json")
	}
	if staged {
		rt, err := Open(OpenOpts{Mutating: false, SkipLock: true})
		if err != nil {
			Fatalf(2, "check: %v", err)
		}
		defer rt.Close()
		paths, err := stagedGitPaths(rt.Repo.Root)
		if err != nil {
			Fatalf(2, "check: %v", err)
		}
		return checkStagedPaths(rt, paths)
	}

	var path string
	if stdinJSON {
		p, err := extractPathFromStdinJSON(os.Stdin)
		if err != nil {
			// In --stdin-json mode, malformed input is exit 2 — the hook will
			// then "warn, don't block" per the plan's failure-mode policy.
			Fatalf(2, "check: %v", err)
		}
		if p == "" {
			// No file_path in the payload (e.g., a Bash tool call). Allow.
			return nil
		}
		path = p
	} else {
		if len(args) != 1 {
			Fatalf(2, "check: provide a positional path or --stdin-json")
		}
		path = args[0]
	}

	// check is read-only on the log; SkipLock=true so we never block on a
	// long-running claim/release in another process.
	rt, err := Open(OpenOpts{Mutating: false, SkipLock: true})
	if err != nil {
		Fatalf(2, "check: %v", err)
	}
	defer rt.Close()

	// If the path is absolute, try to make it repo-relative. Outside the
	// repo → exit 0 (we don't claim anything outside).
	rel, ok := makeRepoRelative(path, rt.Repo.Root)
	if !ok {
		return nil
	}
	scope, err := overlap.Parse(rel)
	if err != nil {
		// Malformed path → exit 2; the hook will warn-not-block.
		Fatalf(2, "check: %v", err)
	}

	// Fail-safe actor handling: ConflictsFor excludes claims held by the
	// caller. A generic ("eli"/"claude"/…) or empty COMMS_ACTOR normally cannot
	// legitimately hold a claim, so do not exclude it unless the caller explicitly
	// enabled COMMS_ALLOW_GENERIC_ACTOR. Use a sentinel that matches no real actor
	// when identity is absent or unauthorized.
	checkActor := rt.Actor
	if checkActor == "" || (actor.IsGeneric(checkActor) && !actor.GenericAllowed()) {
		checkActor = "\x00not-a-real-actor"
	}
	conflicts := rt.State.ConflictsFor(scope, checkActor)
	if len(conflicts) == 0 {
		return nil // exit 0: clear
	}
	render.WriteConflict(os.Stderr, render.Conflict{
		AttemptedScope:  scope.String(),
		AttemptedActor:  rt.Actor,
		AttemptedIntent: "", // check has no --intent
		Holders:         conflicts,
		StaleAfter:      staleClaimAfter,
	})
	os.Exit(1)
	return nil
}

type stagedConflict struct {
	path    string
	holders []*state.Claim
}

func stagedGitPaths(repoRoot string) ([]string, error) {
	// --no-renames exposes both sides of a rename. Both the deleted source and
	// added destination matter because either path may be claimed by a peer.
	cmd := exec.Command("git", "-C", repoRoot, "diff", "--cached", "--name-only", "--no-renames", "-z", "--")
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("read staged Git paths: %w", err)
	}
	parts := bytes.Split(out, []byte{0})
	paths := make([]string, 0, len(parts))
	for _, part := range parts {
		if len(part) == 0 {
			continue
		}
		paths = append(paths, string(part))
	}
	return paths, nil
}

func checkStagedPaths(rt *Runtime, paths []string) error {
	checkActor := rt.Actor
	if checkActor == "" || (actor.IsGeneric(checkActor) && !actor.GenericAllowed()) {
		checkActor = "\x00not-a-real-actor"
	}

	conflicts := make([]stagedConflict, 0)
	for _, path := range paths {
		normalized, err := overlap.NormalizePath(filepath.ToSlash(path))
		if err != nil {
			return fmt.Errorf("normalize staged path %q: %w", path, err)
		}
		// Git gives us one concrete filename, not a comms glob pattern. Interpret
		// metacharacters only on the claim side so a literal '*' in the filename
		// cannot create a false conflict with a different exact claim.
		holders := make([]*state.Claim, 0)
		for _, claim := range rt.State.Claims {
			if claim.Actor == checkActor {
				continue
			}
			if overlap.PatternMatchesPath(claim.Scope.Path, normalized) {
				holders = append(holders, claim)
			}
		}
		sort.Slice(holders, func(i, j int) bool { return holders[i].TS.Before(holders[j].TS) })
		if len(holders) > 0 {
			conflicts = append(conflicts, stagedConflict{path: normalized, holders: holders})
		}
	}
	if len(conflicts) == 0 {
		return nil
	}
	hasHead, err := gitHasHEAD(rt.Repo.Root)
	if err != nil {
		return err
	}

	sort.Slice(conflicts, func(i, j int) bool { return conflicts[i].path < conflicts[j].path })
	fmt.Fprintln(os.Stderr, "BLOCKED: staged Git changes overlap claims held by other actors.")
	for _, conflict := range conflicts {
		fmt.Fprintf(os.Stderr, "  %s\n", render.EscapeScope(conflict.path))
		for _, holder := range conflict.holders {
			fmt.Fprintf(os.Stderr, "    Holder: @%s  Claim: %s  Intent: %q\n",
				render.EscapeActor(holder.Actor), render.EscapeScope(holder.ID), render.EscapeScope(holder.Intent))
		}
	}
	fmt.Fprintln(os.Stderr, "\nUnstage peer-owned paths before committing (working-tree changes are kept):")
	for _, conflict := range conflicts {
		literalPathspec := ":(literal)" + render.EscapeScope(conflict.path)
		if hasHead {
			fmt.Fprintf(os.Stderr, "  git restore --staged -- %s\n", shellQuote(literalPathspec))
		} else {
			fmt.Fprintf(os.Stderr, "  git rm --cached -- %s\n", shellQuote(literalPathspec))
		}
	}
	os.Exit(1)
	return nil
}

// shellQuote returns one literal shell argument. Single quotes stop command,
// variable, glob, and whitespace expansion; an embedded quote is represented
// by ending the quoted string, emitting a quoted quote, and reopening it.
func shellQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "'\"'\"'") + "'"
}

func gitHasHEAD(repoRoot string) (bool, error) {
	cmd := exec.Command("git", "-C", repoRoot, "rev-parse", "--verify", "--quiet", "HEAD")
	if err := cmd.Run(); err != nil {
		if exit, ok := err.(*exec.ExitError); ok && exit.ExitCode() == 1 {
			return false, nil
		}
		return false, fmt.Errorf("inspect Git HEAD: %w", err)
	}
	return true, nil
}

// makeRepoRelative converts an absolute or relative path into a
// repo-relative POSIX path. Returns (rel, true) on success, ("", false) if
// the path lies outside repoRoot.
//
// Symlink handling: the file at `path` may not exist yet (Write creates it),
// so we EvalSymlinks the deepest existing ancestor and re-append the
// remainder. This matters on macOS where /tmp is a symlink to /private/tmp.
func makeRepoRelative(path, repoRoot string) (string, bool) {
	if path == "" {
		return "", false
	}
	abs := path
	if !filepath.IsAbs(abs) {
		// Caller's CWD may differ from repo root (e.g., when Claude Code
		// runs from a subdir). Resolve relative to repo root.
		abs = filepath.Join(repoRoot, path)
	}
	abs = filepath.Clean(abs)

	resolvedAbs := resolveExistingAncestor(abs)
	// repoRoot already came through repo.DiscoverFromCWD which EvalSymlinks'd it.
	resolvedRoot := repoRoot
	if r, err := filepath.EvalSymlinks(repoRoot); err == nil {
		resolvedRoot = r
	}
	rel, err := filepath.Rel(resolvedRoot, resolvedAbs)
	if err != nil {
		return "", false
	}
	if rel == ".." || strings.HasPrefix(rel, "../") {
		return "", false
	}
	return filepath.ToSlash(rel), true
}

// resolveExistingAncestor walks up from `abs` until it finds a directory
// that exists, EvalSymlinks that, then re-attaches the missing tail.
// Lets us handle paths whose final component doesn't exist yet (Write).
func resolveExistingAncestor(abs string) string {
	cur := abs
	var tail []string
	for {
		if _, err := os.Stat(cur); err == nil {
			if resolved, err := filepath.EvalSymlinks(cur); err == nil {
				cur = resolved
			}
			break
		}
		parent := filepath.Dir(cur)
		if parent == cur {
			// Reached filesystem root without finding an existing ancestor.
			break
		}
		tail = append([]string{filepath.Base(cur)}, tail...)
		cur = parent
	}
	if len(tail) == 0 {
		return cur
	}
	return filepath.Join(append([]string{cur}, tail...)...)
}

// extractPathFromStdinJSON parses Claude Code's PreToolUse payload.
//
// Payload shape (as of CC 1.x):
//
//	{
//	  "tool_name": "Edit",
//	  "tool_input": { "file_path": "/abs/or/rel/path.ts", ... },
//	  ...
//	}
//
// We extract tool_input.file_path. Missing → return "" (no file context,
// allow). Malformed → return error.
func extractPathFromStdinJSON(r io.Reader) (string, error) {
	raw, err := io.ReadAll(r)
	if err != nil {
		return "", fmt.Errorf("read stdin: %w", err)
	}
	if len(raw) == 0 {
		return "", nil
	}
	var payload struct {
		ToolInput struct {
			FilePath string `json:"file_path"`
		} `json:"tool_input"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		return "", fmt.Errorf("parse stdin JSON: %w", err)
	}
	return payload.ToolInput.FilePath, nil
}
