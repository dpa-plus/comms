package subcmd

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/dpa-plus/comms/internal/paths"
	"github.com/dpa-plus/comms/internal/state"
	"github.com/spf13/cobra"
)

// NewStatusCmd builds `comms status`. Default output is the human-readable
// section format under ~500 tokens. `--json` emits canonical JSON.
func NewStatusCmd() *cobra.Command {
	var (
		asJSON bool
		since  string
	)
	cmd := &cobra.Command{
		Use:   "status",
		Short: "Show active sessions, claims, and recent findings/notes",
		RunE: func(cmd *cobra.Command, args []string) error {
			return runStatus(asJSON, since)
		},
	}
	cmd.Flags().BoolVar(&asJSON, "json", false, "machine-readable JSON output")
	cmd.Flags().StringVar(&since, "since", "24h", "lookback window for findings/notes (e.g. 1h, 24h, 7d-equivalent in hours)")
	return cmd
}

func runStatus(asJSON bool, since string) error {
	rt, err := Open(OpenOpts{Mutating: false})
	if err != nil {
		return err
	}
	defer rt.Close()

	dur, err := parseDuration(since)
	if err != nil {
		Fatalf(2, "status: %v", err)
	}
	cutoff := time.Now().Add(-dur)

	if asJSON {
		return emitStatusJSON(rt, cutoff)
	}
	emitStatusHuman(rt, cutoff, since)
	return nil
}

func emitStatusHuman(rt *Runtime, cutoff time.Time, since string) {
	now := time.Now()
	allSessions := collectActiveSessions(rt.State, now.Add(-activeWindow))
	allClaims := sortedClaims(rt.State)
	allDocs := listDocs(rt.Paths.Docs)
	allLessons := listGlobalLessons()
	sessions, omittedSessions := limitSlice(allSessions, 10)
	// Claims are sorted oldest-first, so the cap keeps the oldest (most-likely
	// stale) claims visible. The cap sits above observed real-world peaks
	// (~38 simultaneous claims) so the conflict-avoidance list an agent reads
	// before editing never silently hides an active claim.
	claims, omittedClaims := limitSlice(allClaims, 50)
	findings := recentFindings(rt.State, cutoff, 5)
	notes := recentNotes(rt.State, cutoff, 3)
	docs, omittedDocs := limitSlice(allDocs, 10)
	lessons, omittedLessons := limitSlice(allLessons, 8)
	omitted := omittedSessions + omittedClaims + omittedDocs + omittedLessons

	fmt.Printf("ACTIVE SESSIONS (active in last 4h)\n")
	if len(sessions) == 0 {
		fmt.Println("  (none)")
	} else {
		markLeaderSessions(sessions)
		for _, s := range sessions {
			cClaims, cFindings := claimAndFindingCounts(rt.State, s.Actor)
			role := ""
			if s.Leader {
				role = "  leader"
			}
			label := ""
			if s.Label != "" {
				label = fmt.Sprintf(" (%s)", s.Label)
			}
			sessionLabel := ""
			if s.SessionName != "" {
				sessionLabel = fmt.Sprintf("  session=%q", s.SessionName)
			}
			// An actor holding locks while silent past the stale threshold is the
			// crash signal worth surfacing — staleness alone (idle holder, no locks)
			// is benign. The fix is the already-built `release --all-mine` (or the
			// dashboard's release-all).
			silent := now.Sub(lastSeenOf(s))
			seen := "active now"
			if silent >= time.Minute {
				seen = fmt.Sprintf("seen %s ago", shortAge(silent))
			}
			deadFlag := ""
			if cClaims > 0 && silent >= staleClaimAfter {
				deadFlag = fmt.Sprintf("   ** LIKELY DEAD: holds %d, silent %s **", cClaims, shortAge(silent))
			}
			fmt.Printf("  @%-14s%s %s   %d claim%s  %d finding%s%s%s%s\n",
				s.Actor, label, seen, cClaims, pluralS(cClaims), cFindings, pluralS(cFindings), role, sessionLabel, deadFlag)
		}
	}

	fmt.Println()
	fmt.Println("ACTIVE CLAIMS")
	if len(claims) == 0 {
		fmt.Println("  (none)")
	} else {
		for _, c := range claims {
			sessionLabel := ""
			if c.SessionName != "" {
				sessionLabel = fmt.Sprintf("   session=%q", c.SessionName)
			}
			// Age + STALE tag so a lock opened 14h ago doesn't read the same as one
			// opened 2m ago — the web dashboard already shows this; the CLI (what
			// agents actually pipe) was blind to it.
			age := now.Sub(c.TS)
			staleTag := ""
			if age >= staleClaimAfter {
				staleTag = "  STALE"
			}
			fmt.Printf("  @%-14s %s   %q   (since %s · %s)%s%s\n",
				c.Actor, c.Scope.String(), c.Intent, c.TS.Local().Format("15:04"), shortAge(age), staleTag, sessionLabel)
		}
	}

	fmt.Println()
	fmt.Printf("RECENT FINDINGS (last %s)\n", since)
	if len(findings) == 0 {
		fmt.Println("  (none)")
	} else {
		for _, f := range findings {
			marker := ""
			if f.Priority {
				marker = "!"
			}
			fmt.Printf("  %-9s%s @%-14s %s\n", f.Category, marker, f.Actor, f.Summary)
		}
	}

	fmt.Println()
	fmt.Printf("RECENT NOTES (last %s)\n", since)
	if len(notes) == 0 {
		fmt.Println("  (none)")
	} else {
		for _, n := range notes {
			marker := ""
			if n.Priority {
				marker = "! "
			}
			fmt.Printf("  %s@%-14s %s\n", marker, n.Actor, n.Body)
		}
	}

	if len(docs) > 0 {
		fmt.Println()
		fmt.Printf("DOCS (%d): %s\n", len(docs), strings.Join(docs, ", "))
	}
	if len(lessons) > 0 {
		fmt.Println()
		fmt.Printf("GLOBAL LESSONS (%d): %s\n", len(lessons), strings.Join(lessons, ", "))
	}
	if omitted > 0 {
		fmt.Printf("\n... %d more; run `comms log --since %s` for details\n", omitted, since)
	}
}

// statusJSONShape is the canonical machine output for `comms status --json`.
type statusJSONShape struct {
	Sessions []statusSession `json:"sessions"`
	Claims   []statusClaim   `json:"claims"`
	Findings []statusFinding `json:"findings"`
	Notes    []statusNote    `json:"notes"`
	Docs     []string        `json:"docs"`
	Lessons  []string        `json:"lessons"`
}

type statusSession struct {
	Actor string    `json:"actor"`
	Label string    `json:"label,omitempty"`
	TS    time.Time `json:"ts"`
	// LastSeen is the actor's passive heartbeat (most-recent event of any type).
	LastSeen    time.Time `json:"last_seen"`
	Leader      bool      `json:"leader"`
	SessionID   string    `json:"session_id,omitempty"`
	SessionName string    `json:"session_name,omitempty"`
}

type statusClaim struct {
	ID     string    `json:"id"`
	Actor  string    `json:"actor"`
	Scope  string    `json:"scope"`
	Intent string    `json:"intent"`
	TS     time.Time `json:"ts"`
	// Age is a human-readable hold duration; Stale is true past the stale window.
	Age         string `json:"age"`
	Stale       bool   `json:"stale"`
	StoleID     string `json:"stole_id,omitempty"`
	SessionID   string `json:"session_id,omitempty"`
	SessionName string `json:"session_name,omitempty"`
}

type statusFinding struct {
	ID          string    `json:"id"`
	Actor       string    `json:"actor"`
	Category    string    `json:"category"`
	Summary     string    `json:"summary"`
	Priority    bool      `json:"priority"`
	TS          time.Time `json:"ts"`
	SessionID   string    `json:"session_id,omitempty"`
	SessionName string    `json:"session_name,omitempty"`
}

type statusNote struct {
	ID          string    `json:"id"`
	Actor       string    `json:"actor"`
	Body        string    `json:"body"`
	Priority    bool      `json:"priority"`
	TS          time.Time `json:"ts"`
	SessionID   string    `json:"session_id,omitempty"`
	SessionName string    `json:"session_name,omitempty"`
}

func emitStatusJSON(rt *Runtime, cutoff time.Time) error {
	now := time.Now()
	out := statusJSONShape{}
	sessions := collectActiveSessions(rt.State, now.Add(-activeWindow))
	markLeaderSessions(sessions)
	for _, s := range sessions {
		out.Sessions = append(out.Sessions, statusSession{Actor: s.Actor, Label: s.Label, TS: s.TS, LastSeen: lastSeenOf(s), Leader: s.Leader, SessionID: s.SessionID, SessionName: s.SessionName})
	}
	for _, c := range sortedClaims(rt.State) {
		age := now.Sub(c.TS)
		out.Claims = append(out.Claims, statusClaim{
			ID: c.ID, Actor: c.Actor, Scope: c.Scope.String(),
			Intent: c.Intent, TS: c.TS, Age: shortAge(age), Stale: age >= staleClaimAfter,
			StoleID: c.StolenFromID, SessionID: c.SessionID, SessionName: c.SessionName,
		})
	}
	for _, f := range recentFindings(rt.State, cutoff, 50) {
		out.Findings = append(out.Findings, statusFinding{
			ID: f.ID, Actor: f.Actor, Category: f.Category,
			Summary: f.Summary, Priority: f.Priority, TS: f.TS, SessionID: f.SessionID, SessionName: f.SessionName,
		})
	}
	for _, n := range recentNotes(rt.State, cutoff, 50) {
		out.Notes = append(out.Notes, statusNote{
			ID: n.ID, Actor: n.Actor, Body: n.Body, Priority: n.Priority, TS: n.TS, SessionID: n.SessionID, SessionName: n.SessionName,
		})
	}
	out.Docs = listDocs(rt.Paths.Docs)
	out.Lessons = listGlobalLessons()

	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(out)
}

// ---- helpers used by both human and JSON renderers ----

// activeWindow is the single coordination-recency window: an actor (or named
// session) counts as "active" if it has been seen within this span. Lifted from
// a 4h literal that was duplicated across ~15 sites so the liveness semantics
// live in one place.
const activeWindow = 4 * time.Hour

// staleClaimAfter is the age past which a held claim — and, when its holder has
// also gone silent that long, the holder itself — is flagged in the CLI. It
// mirrors the dashboard's default --stale-after so the CLI and UI agree.
const staleClaimAfter = 90 * time.Minute

// lastSeenOf returns the actor's passive heartbeat, falling back to the hello TS
// for any Session built without going through Fold (e.g. demo fixtures).
func lastSeenOf(sess *state.Session) time.Time {
	if sess.LastSeen.IsZero() {
		return sess.TS
	}
	return sess.LastSeen
}

// collectActiveSessions returns the roster of actors seen since cutoff, most
// recently active first. Liveness is judged by LastSeen (any event) rather than
// the one-shot hello, so a still-working agent that hello'd long ago stays on
// the roster and a crashed agent drops off once it goes silent past the window.
func collectActiveSessions(s *state.State, cutoff time.Time) []*state.Session {
	if s == nil {
		return nil
	}
	var out []*state.Session
	for _, sess := range s.Sessions {
		if lastSeenOf(sess).After(cutoff) {
			out = append(out, sess)
		}
	}
	sort.Slice(out, func(i, j int) bool { return lastSeenOf(out[i]).After(lastSeenOf(out[j])) })
	return out
}

func activeLeaderActor(s *state.State, cutoff time.Time) string {
	sessions := collectActiveSessions(s, cutoff)
	markLeaderSessions(sessions)
	for _, sess := range sessions {
		if sess.Leader {
			return sess.Actor
		}
	}
	return ""
}

func markLeaderSessions(sessions []*state.Session) {
	if len(sessions) == 0 {
		return
	}
	var explicit *state.Session
	for _, s := range sessions {
		if s.Leader {
			explicit = s
			break
		}
	}
	for _, s := range sessions {
		s.Leader = false
	}
	leader := explicit
	if leader == nil {
		leader = sessions[0]
		for _, s := range sessions {
			if s.TS.Before(leader.TS) {
				leader = s
			}
		}
	}
	leader.Leader = true
}

func requireLeader(rt *Runtime) {
	leader := activeLeaderActor(rt.State, time.Now().Add(-activeWindow))
	if leader == "" {
		Fatalf(1, "priority messages require an active leader; run `comms hello` first")
	}
	if rt.Actor != leader {
		Fatalf(1, "priority messages are leader-only; current leader is @%s", leader)
	}
}

func sortedClaims(s *state.State) []*state.Claim {
	if s == nil {
		return nil
	}
	var out []*state.Claim
	for _, c := range s.Claims {
		out = append(out, c)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].TS.Before(out[j].TS) })
	return out
}

func recentFindings(s *state.State, cutoff time.Time, max int) []*state.Finding {
	if s == nil {
		return nil
	}
	out := make([]*state.Finding, 0, len(s.Findings))
	for i := len(s.Findings) - 1; i >= 0; i-- {
		f := s.Findings[i]
		if f.TS.Before(cutoff) {
			continue
		}
		out = append(out, f)
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Priority != out[j].Priority {
			return out[i].Priority
		}
		return out[i].TS.After(out[j].TS)
	})
	if max > 0 && len(out) > max {
		out = out[:max]
	}
	return out
}

func recentReleases(s *state.State, cutoff time.Time, max int) []*state.Release {
	if s == nil {
		return nil
	}
	out := make([]*state.Release, 0, len(s.Releases))
	for i := len(s.Releases) - 1; i >= 0; i-- {
		r := s.Releases[i]
		if r.TS.Before(cutoff) {
			continue
		}
		out = append(out, r)
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].TS.After(out[j].TS) })
	if max > 0 && len(out) > max {
		out = out[:max]
	}
	return out
}

func recentNotes(s *state.State, cutoff time.Time, max int) []*state.Note {
	if s == nil {
		return nil
	}
	out := make([]*state.Note, 0, len(s.Notes))
	for i := len(s.Notes) - 1; i >= 0; i-- {
		n := s.Notes[i]
		if n.TS.Before(cutoff) {
			continue
		}
		out = append(out, n)
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Priority != out[j].Priority {
			return out[i].Priority
		}
		return out[i].TS.After(out[j].TS)
	})
	if max > 0 && len(out) > max {
		out = out[:max]
	}
	return out
}

func claimAndFindingCounts(s *state.State, actor string) (int, int) {
	claims := 0
	for _, c := range s.Claims {
		if c.Actor == actor {
			claims++
		}
	}
	findings := 0
	for _, f := range s.Findings {
		if f.Actor == actor {
			findings++
		}
	}
	return claims, findings
}

func listDocs(docsDir string) []string {
	entries, err := os.ReadDir(docsDir)
	if err != nil {
		return nil
	}
	var out []string
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".md") || strings.HasPrefix(name, ".") {
			continue
		}
		out = append(out, strings.TrimSuffix(name, ".md"))
	}
	sort.Strings(out)
	return out
}

// parseDuration accepts the standard Go duration formats (1h, 30m, 168h).
// We don't support `2d` in MVP per the plan's LOW-severity note.
func parseDuration(s string) (time.Duration, error) {
	d, err := time.ParseDuration(s)
	if err != nil {
		return 0, fmt.Errorf("invalid --since %q (use 1h, 30m, 168h, etc.)", s)
	}
	if d < 0 {
		return 0, fmt.Errorf("--since must not be negative (got %q)", s)
	}
	return d, nil
}

func limitSlice[T any](in []T, max int) ([]T, int) {
	if len(in) <= max {
		return in, 0
	}
	return in[:max], len(in) - max
}

func listGlobalLessons() []string {
	dir, err := paths.GlobalLessonsDir()
	if err != nil {
		return nil
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	slugs := markdownSlugs(entries)
	sort.Strings(slugs)
	return slugs
}
