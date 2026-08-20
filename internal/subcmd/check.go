package subcmd

import (
	"bytes"
	"encoding/json"
	"errors"
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
	"github.com/dpa-plus/comms/internal/repo"
	"github.com/dpa-plus/comms/internal/state"
	"github.com/spf13/cobra"
)

// NewCheckCmd builds `comms check`. Two different consumers read its exit code
// and they do NOT agree on what the numbers mean, which is worth stating plainly
// because getting it wrong silently disabled this command for its whole life.
//
// Claude Code's PreToolUse contract: 2 blocks the tool call and feeds stderr back
// to the model; ANY other non-zero code is treated as "the hook itself failed",
// reported to the user, and the edit proceeds. A git pre-commit hook, by
// contrast, aborts on any non-zero at all.
//
// So:
//
//	0 — path clear (or held by same actor)
//	2 — blocked by another actor's active claim (PreToolUse path — must be 2)
//	1 — blocked, --staged path (feeds git, where any non-zero aborts)
//	2 — system error (broken log, unreadable dir)
//
// Blocked and system-error share code 2 on the hook path, and that is the safe
// direction: if comms cannot read the log it cannot prove the path is clear, so
// it should stop rather than wave the edit through. Note what this used to do —
// a real conflict exited 1 and Claude Code let the edit through, while a comms
// bug exited 2 and blocked it. The behaviour was exactly inverted.
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
	var hookSession string
	if stdinJSON {
		p, sid, err := extractPathFromStdinJSON(os.Stdin)
		hookSession = sid
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

	// Resolve the repository from the FILE, not from the process.
	//
	// A hook does not run where the file lives. Claude Code's working directory is
	// wherever the session was started, which is routinely a parent folder holding
	// several checkouts — and is very often not a repository at all. Resolving from
	// the process meant the hook failed on every edit whenever that was true,
	// including for files that were plainly inside a repository.
	repoHint := ""
	if stdinJSON && filepath.IsAbs(path) {
		// Walk up to the nearest directory that exists. Creating a new file in a
		// new directory is ordinary — the editing tool makes the parents — but the
		// hook runs BEFORE that happens, so the file's own directory frequently
		// does not exist yet. Handing a non-existent path to repo discovery makes
		// it fail on a stat, which used to block the write.
		d := filepath.Dir(path)
		for {
			if st, err := os.Stat(d); err == nil && st.IsDir() {
				repoHint = d
				break
			}
			parent := filepath.Dir(d)
			if parent == d {
				break // reached the root without finding anything that exists
			}
			d = parent
		}
	}
	// check is read-only on the log; SkipLock=true so we never block on a
	// long-running claim/release in another process.
	rt, err := Open(OpenOpts{Mutating: false, SkipLock: true, RepoRootOverride: repoHint})
	if err != nil {
		// NOT exit 2. Failing to find a repository is not "I cannot tell whether
		// this path is clear" — it is "there is nothing here to coordinate", and the
		// safe answer to that is to let the edit through. Exiting 2 blocks it, which
		// made every edit outside a repository impossible the moment the hook was
		// installed. Only a repository we CAN identify but CANNOT read is worth
		// stopping for, and that is handled below.
		if errors.Is(err, repo.ErrNoRepo) {
			return nil
		}
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
	// A hook has no COMMS_ACTOR of its own, so fall back to the actor that said
	// hello from this same agent session. Without this the hook cannot recognise
	// the caller's OWN claims, and blocks it from editing what it just claimed.
	if checkActor == "" && hookSession != "" {
		checkActor = resolveHookActor(rt.State, hookSession)
	}
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
	// The hook refusing an edit is the same evidence as a refused claim, and it
	// is the one that fires most often. check runs read-only and unlocked on
	// every edit, so take a writable handle only here, on the rare path where
	// something was actually blocked.
	if w, err := Open(OpenOpts{Mutating: true}); err == nil {
		recordBlocked(w, scope.String(), "", conflicts)
		_ = w.Close()
	}
	// EXIT 2, NOT 1. Claude Code's PreToolUse contract treats 2 as "block this
	// tool call and show stderr to the model", and every other non-zero code as
	// "the hook itself failed" — which is reported to the user and lets the edit
	// through. This exited 1 from the day it shipped, so the pre-edit hook has
	// never once blocked an edit in any session. The conflict report was written
	// to stderr and thrown away.
	os.Exit(2)
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
		// `git diff --name-only -z` gives us a repository-relative concrete
		// filename, not a user-authored comms scope. Keep every non-NUL filename
		// byte intact; claim-scope validation deliberately rejects controls and
		// therefore does not apply at this transient Git boundary.
		concretePath := filepath.ToSlash(path)
		// Interpret metacharacters only on the claim side so a literal '*' in the
		// filename cannot create a false conflict with a different exact claim.
		holders := make([]*state.Claim, 0)
		for _, claim := range rt.State.Claims {
			if claim.Actor == checkActor {
				continue
			}
			if overlap.PatternMatchesPath(claim.Scope.Path, concretePath) {
				holders = append(holders, claim)
			}
		}
		sort.Slice(holders, func(i, j int) bool { return holders[i].TS.Before(holders[j].TS) })
		if len(holders) > 0 {
			conflicts = append(conflicts, stagedConflict{path: concretePath, holders: holders})
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
	recoveryPrefix := "(cd " + shellQuote(rt.Repo.Root) + " && "
	if render.EscapeScope(rt.Repo.Root) != rt.Repo.Root {
		// Command substitution strips trailing newlines. Append a printable
		// sentinel during reconstruction, then remove only that sentinel before
		// changing directories so every repository-root byte survives.
		encodedRoot := octalEscapeBytes(rt.Repo.Root) + `\0130`
		recoveryPrefix = "(repo=$(printf '%b' " + shellQuote(encodedRoot) + `); repo=${repo%X}; cd "$repo" && `
	}
	for _, conflict := range conflicts {
		// The diagnostic above is sanitized for terminal safety, but recovery must
		// retain the exact Git filename bytes.
		literalPathspec := ":(literal)" + conflict.path
		if render.EscapeScope(conflict.path) != conflict.path {
			// Keep control and invalid UTF-8 bytes off the terminal while preserving
			// them exactly for Git. POSIX printf reconstructs one NUL-terminated
			// literal pathspec from its octal byte escapes on a single output line.
			command := "git restore --staged"
			if !hasHead {
				command = "git rm --cached -f"
			}
			fmt.Fprintf(os.Stderr, "  %sprintf '%%b' %s | %s --pathspec-from-file=- --pathspec-file-nul)\n",
				recoveryPrefix, shellQuote(octalEscapeBytes(literalPathspec)+`\0000`), command)
			continue
		}
		if hasHead {
			fmt.Fprintf(os.Stderr, "  %sgit restore --staged -- %s)\n",
				recoveryPrefix, shellQuote(literalPathspec))
		} else {
			fmt.Fprintf(os.Stderr, "  %sgit rm --cached -f -- %s)\n",
				recoveryPrefix, shellQuote(literalPathspec))
		}
	}
	// NOT 2. This gate feeds a git pre-commit hook, where any non-zero aborts the
	// commit, and 1 is what the contract and the test say. Only the PreToolUse
	// path above needs 2, because only Claude Code distinguishes the codes.
	os.Exit(1)
	return nil
}

// shellQuote returns one literal shell argument. Single quotes stop command,
// variable, glob, and whitespace expansion; an embedded quote is represented
// by ending the quoted string, emitting a quoted quote, and reopening it.
func shellQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "'\"'\"'") + "'"
}

func octalEscapeBytes(value string) string {
	var b strings.Builder
	for i := 0; i < len(value); i++ {
		fmt.Fprintf(&b, `\0%03o`, value[i])
	}
	return b.String()
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
func extractPathFromStdinJSON(r io.Reader) (string, string, error) {
	raw, err := io.ReadAll(r)
	if err != nil {
		return "", "", fmt.Errorf("read stdin: %w", err)
	}
	if len(raw) == 0 {
		return "", "", nil
	}
	var payload struct {
		SessionID string `json:"session_id"`
		ToolInput struct {
			FilePath string `json:"file_path"`
		} `json:"tool_input"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		return "", "", fmt.Errorf("parse stdin JSON: %w", err)
	}
	return payload.ToolInput.FilePath, payload.SessionID, nil
}

// resolveHookActor works out which actor is behind a hook invocation.
//
// A hook process inherits the environment, and COMMS_ACTOR is set per command by
// agents rather than exported into the session, so the hook almost never has one.
// Without an actor every claim looks like somebody else's — including your own —
// so an agent that claimed a file was then blocked from editing it, which is the
// fastest possible way to get the hook switched off.
//
// The payload's session id is the same id the agent's environment carried when it
// ran `comms hello`, so the log can answer the question the environment cannot.
func resolveHookActor(st *state.State, agentSession string) string {
	if agentSession == "" || st == nil {
		return ""
	}
	var newest *state.Session
	for _, sess := range st.Sessions {
		if sess.AgentSession != agentSession {
			continue
		}
		if newest == nil || sess.TS.After(newest.TS) {
			newest = sess
		}
	}
	if newest == nil {
		return ""
	}
	return newest.Actor
}
