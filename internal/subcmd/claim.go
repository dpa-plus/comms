package subcmd

import (
	"fmt"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/dpa-plus/comms/internal/event"
	"github.com/dpa-plus/comms/internal/overlap"
	"github.com/dpa-plus/comms/internal/render"
	"github.com/dpa-plus/comms/internal/state"
	"github.com/spf13/cobra"
)

// NewClaimCmd builds the `comms claim` subcommand.
//
// Exit codes:
//
//	0 — claim granted
//	1 — blocked by another active claim (stderr = render.WriteConflict)
//	2 — system error
func NewClaimCmd() *cobra.Command {
	var (
		intent      string
		stealID     string
		stealReason string
	)
	cmd := &cobra.Command{
		Use:   `claim "<scope>" ["<scope>" ...]`,
		Short: "Open exclusive claims on one or more scopes",
		Long: `Open an exclusive claim on each scope. A scope is path[#anchor] (POSIX path,
optional line range L<n>-<m> or symbol name). Pass several scopes to claim a
whole task's worth of files in one call — each gets its own claim event under
the shared --intent, and the batch is all-or-nothing: if ANY scope conflicts
with another actor's active claim, nothing is claimed.

On conflict, exits 1 with a structured stderr report telling you exactly which
next-command to run.`,
		Args: cobra.MinimumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if stealID != "" && len(args) != 1 {
				Fatalf(2, "claim: --steal takes exactly one scope")
			}
			if len(args) == 1 {
				return runClaim(args[0], intent, stealID, stealReason)
			}
			return runClaimBatch(args, intent)
		},
	}
	cmd.Flags().StringVar(&intent, "intent", "", "one-line description of the change you're making (required)")
	cmd.Flags().StringVar(&stealID, "steal", "", "claim ID to displace; must overlap the claimed scope. --reason is required only when it is still active — a stale claim (idle >=1h) is stolen without one")
	cmd.Flags().StringVar(&stealReason, "reason", "", "justification for stealing a still-active claim (auto-filled for a stale one); printed in the audit trail")
	return cmd
}

func runClaim(scopeRaw, intent, stealID, stealReason string) error {
	if intent == "" {
		Fatalf(2, "claim: --intent is required")
	}
	scope, err := overlap.Parse(scopeRaw)
	if err != nil {
		Fatalf(2, "claim: %v", err)
	}

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		return err
	}
	defer rt.Close()

	// Policy check: if the scope path matches a risky entry, an anchor is required.
	if rt.Policy.RequiresAnchor(scope.Path) && scope.Anchor.Kind == overlap.AnchorWhole {
		Fatalf(1, "claim: anchor required for risky file %q (policy: .comms/policy.txt). Add #L<n>-<m> or #<symbol>.", scope.Path)
	}

	// Steal validation: --steal must resolve to an active claim. A STALE claim
	// (idle >= staleClaimAfter, i.e. its holder is presumed gone) can be stolen
	// freely; --reason is then optional and auto-filled. Stealing a still-active
	// claim out from under a live teammate still requires an explicit --reason.
	var displaceID string
	var autoStale bool
	if stealID != "" {
		target := rt.State.ClaimByID(stealID)
		if target == nil {
			Fatalf(2, "claim: --steal %q does not match any active claim", stealID)
		}
		// You may only steal the claim that actually overlaps the scope you are
		// claiming. Without this guard a wrong/auto-stale id would slip through
		// (the displaced claim isn't in ConflictsFor, so no conflict fires) and
		// Fold would delete an unrelated holder's claim on a file you never touched.
		if !overlap.Scopes(target.Scope, scope) {
			Fatalf(2, "claim: --steal %s targets %s, which does not overlap the scope you are claiming (%s) — steal the claim blocking THIS scope",
				short(target.ID), target.Scope.String(), scope.String())
		}
		idle := time.Since(target.TS)
		stale := idle >= staleClaimAfter
		if stealReason == "" {
			if stale {
				stealReason = fmt.Sprintf("prior claim stale: @%s idle %s (stale after %s)", target.Actor, idle.Round(time.Minute), shortAge(staleClaimAfter))
				autoStale = true
			} else {
				Fatalf(2, "claim: @%s's claim is still active (held %s; stale after %s) — stealing it requires --reason. A stale claim (idle >= %s) can be stolen without one.",
					target.Actor, shortAge(idle), shortAge(staleClaimAfter), shortAge(staleClaimAfter))
			}
		}
		displaceID = target.ID
	}

	// Conflict detection: other actors' overlapping claims (excluding the one we're stealing).
	conflicts := rt.State.ConflictsFor(scope, rt.Actor)
	if displaceID != "" {
		conflicts = filterOutClaim(conflicts, displaceID)
	}
	if len(conflicts) > 0 {
		render.WriteConflict(os.Stderr, render.Conflict{
			AttemptedScope:  scope.String(),
			AttemptedActor:  rt.Actor,
			AttemptedIntent: intent,
			Holders:         conflicts,
			StaleAfter:      staleClaimAfter,
		})
		os.Exit(1)
	}

	// Build the claim event.
	now := time.Now().UTC()
	data := map[string]interface{}{"intent": intent}
	stampActiveCommsSession(rt, data)
	if displaceID != "" {
		data["steals"] = displaceID
		data["steal_reason"] = stealReason
		if autoStale {
			// Fully automatic takeover of a stale claim — no human authorized it,
			// so record it as such instead of stamping a misleading arbitrator.
			data["steal_kind"] = "auto-stale"
		} else if a := os.Getenv("COMMS_ARBITRATOR"); a != "" {
			// Human-arbitrated steal: record who authorized it (COMMS_ARBITRATOR,
			// else $USER) for the audit trail.
			data["arbitrator"] = a
		} else if u := os.Getenv("USER"); u != "" {
			data["arbitrator"] = u
		}
	}
	ev := event.Event{
		TS:    now,
		ID:    event.NewID(now),
		Actor: rt.Actor,
		Type:  event.TypeClaim,
		Scope: []string{scope.String()},
		Data:  data,
	}
	if err := rt.Append(ev); err != nil {
		return err
	}

	// Print the claim ID + short summary on stdout.
	if displaceID != "" {
		fmt.Printf("@%s claimed %s (stole %s from prior holder)\n  ID: %s\n",
			rt.Actor, scope.String(), short(displaceID), ev.ID)
	} else {
		fmt.Printf("@%s claimed %s\n  ID: %s\n", rt.Actor, scope.String(), ev.ID)
	}
	printClaimContext(rt, []overlap.Scope{scope})
	return nil
}

// runClaimBatch claims several scopes in one locked pass. It validates policy
// and conflicts for ALL scopes first and only appends events if every scope is
// clear, so a multi-file task boundary is claimed atomically (or not at all).
func runClaimBatch(scopeRaws []string, intent string) error {
	if intent == "" {
		Fatalf(2, "claim: --intent is required")
	}
	scopes := make([]overlap.Scope, 0, len(scopeRaws))
	for _, raw := range scopeRaws {
		sc, err := overlap.Parse(raw)
		if err != nil {
			Fatalf(2, "claim: %v", err)
		}
		scopes = append(scopes, sc)
	}

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		return err
	}
	defer rt.Close()

	for _, sc := range scopes {
		if rt.Policy.RequiresAnchor(sc.Path) && sc.Anchor.Kind == overlap.AnchorWhole {
			Fatalf(1, "claim: anchor required for risky file %q (policy: .comms/policy.txt). Add #L<n>-<m> or #<symbol>.", sc.Path)
		}
	}

	var blocked []*state.Claim
	for _, sc := range scopes {
		blocked = append(blocked, rt.State.ConflictsFor(sc, rt.Actor)...)
	}
	if len(blocked) > 0 {
		// ConflictsFor is sorted only within each scope; concatenating across
		// scopes loses the global order. Re-sort oldest-first so WriteConflict's
		// "primary = Holders[0]" is the actually-oldest (most-likely-stale) holder.
		holders := dedupeClaimsByID(blocked)
		sort.SliceStable(holders, func(i, j int) bool { return holders[i].TS.Before(holders[j].TS) })
		render.WriteConflict(os.Stderr, render.Conflict{
			AttemptedScope:  joinScopeStrings(scopes),
			AttemptedActor:  rt.Actor,
			AttemptedIntent: intent,
			Holders:         holders,
			StaleAfter:      staleClaimAfter,
		})
		os.Exit(1)
	}

	// Every claim in the batch shares ONE real timestamp; event.NewID is
	// monotonic, so the IDs stay strictly ordered within the millisecond. We must
	// NOT advance the timestamp per scope: future-stamping let a subsequent
	// real-time release sort BEFORE the not-yet-reached claims, so Fold's delete
	// missed them and they dangled active forever. Fold sorts by TS with a stable
	// sort, so equal-TS claims keep append order and any later release folds after
	// them.
	now := time.Now().UTC()
	ids := make([]string, 0, len(scopes))
	evs := make([]event.Event, 0, len(scopes))
	for _, sc := range scopes {
		data := map[string]interface{}{"intent": intent}
		stampActiveCommsSession(rt, data)
		ev := event.Event{
			TS:    now,
			ID:    event.NewID(now),
			Actor: rt.Actor,
			Type:  event.TypeClaim,
			Scope: []string{sc.String()},
			Data:  data,
		}
		evs = append(evs, ev)
		ids = append(ids, ev.ID)
	}
	// Append every claim and fold once, not once per scope — shorter lock hold.
	if err := rt.AppendBatch(evs); err != nil {
		return err
	}

	fmt.Printf("@%s claimed %d scopes:\n", rt.Actor, len(scopes))
	for i, sc := range scopes {
		fmt.Printf("  • %s  (ID: %s)\n", sc.String(), short(ids[i]))
	}
	printClaimContext(rt, scopes)
	return nil
}

func joinScopeStrings(scopes []overlap.Scope) string {
	parts := make([]string, len(scopes))
	for i, sc := range scopes {
		parts[i] = sc.String()
	}
	return strings.Join(parts, ", ")
}

func dedupeClaimsByID(in []*state.Claim) []*state.Claim {
	seen := map[string]struct{}{}
	out := make([]*state.Claim, 0, len(in))
	for _, c := range in {
		if _, ok := seen[c.ID]; ok {
			continue
		}
		seen[c.ID] = struct{}{}
		out = append(out, c)
	}
	return out
}

func filterOutClaim(in []*state.Claim, id string) []*state.Claim {
	out := in[:0]
	for _, c := range in {
		if c.ID == id {
			continue
		}
		out = append(out, c)
	}
	return out
}

// printClaimContext surfaces up to 3 prior findings on the just-claimed path(s)
// — the decisions/gotchas that explain WHY a file is the way it is — at the exact
// moment an agent is about to edit it. claim is the most-run command and was
// context-free; this turns it into the natural read-trigger (most agents never
// run a separate `comms log --scope` query). Durable findings (decision/gotcha)
// are preferred over churn (bug/fix/ship). Silent when there is no prior context.
func printClaimContext(rt *Runtime, scopes []overlap.Scope) {
	matches := findingsOnScopes(rt.State, scopes, 3)
	if len(matches) == 0 {
		return
	}
	fmt.Println("  prior context on this path:")
	for _, f := range matches {
		// Truncate by RUNES (not bytes) so a multibyte summary — these contain
		// German/UTF-8 — is never cut mid-rune into mojibake.
		summary := f.Summary
		if r := []rune(summary); len(r) > 96 {
			summary = string(r[:95]) + "…"
		}
		age := shortAge(time.Since(f.TS))
		when := age + " ago"
		if age == "now" {
			when = "just now"
		}
		// Sanitize log-sourced fields before they hit the terminal — the same
		// defense the conflict renderer uses against ESC/C0/C1/DEL injection.
		fmt.Printf("    • [%s] %s  (@%s, %s)\n", render.EscapeScope(f.Category), render.EscapeScope(summary), render.EscapeActor(f.Actor), when)
	}
}

// findingsOnScopes returns prior findings whose path ref overlaps any of the
// given scopes, durable (decision/gotcha) first then newest, capped at max.
func findingsOnScopes(s *state.State, scopes []overlap.Scope, max int) []*state.Finding {
	if s == nil || len(scopes) == 0 {
		return nil
	}
	var out []*state.Finding
	for _, f := range s.Findings {
		if findingMatchesAnyScope(f, scopes) {
			out = append(out, f)
		}
	}
	sort.SliceStable(out, func(i, j int) bool {
		di, dj := durableCategory(out[i].Category), durableCategory(out[j].Category)
		if di != dj {
			return di
		}
		return out[i].TS.After(out[j].TS)
	})
	if max > 0 && len(out) > max {
		out = out[:max]
	}
	return out
}

func durableCategory(cat string) bool { return cat == "decision" || cat == "gotcha" }

func findingMatchesAnyScope(f *state.Finding, scopes []overlap.Scope) bool {
	for _, ref := range f.Refs {
		if ref.Kind != "path" {
			continue
		}
		rs, err := overlap.Parse(ref.Value)
		if err != nil {
			continue
		}
		for _, sc := range scopes {
			if overlap.Scopes(rs, sc) {
				return true
			}
		}
	}
	return false
}
