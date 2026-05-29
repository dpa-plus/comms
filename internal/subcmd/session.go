package subcmd

import (
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/dpa-plus/comms/internal/event"
	"github.com/dpa-plus/comms/internal/state"
	"github.com/spf13/cobra"
)

// NewSessionCmd builds admin commands for the active comms session roster.
func NewSessionCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "session",
		Short: "Manage active comms session actors",
		Long: `Manage the active comms session roster without editing the append-only log.

Retiring an actor appends an audit event that removes that actor from the
active-session view and releases any claims it still holds. It does not delete
historical log rows.

Leadership can be transferred to exactly one active actor. The leader's only
extra privilege is posting --priority notes/findings.`,
	}
	cmd.AddCommand(newSessionStartCmd(), newSessionJoinCmd(), newSessionEndCmd(), newSessionRetireCmd(), newSessionLeadCmd())
	return cmd
}

func newSessionStartCmd() *cobra.Command {
	var label string
	cmd := &cobra.Command{
		Use:   `start "<name>"`,
		Short: "Create and join a named comms session",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runSessionStart(args[0], label)
		},
	}
	cmd.Flags().StringVar(&label, "label", "", "friendly display label for this actor in status/UI")
	return cmd
}

func newSessionJoinCmd() *cobra.Command {
	var label string
	cmd := &cobra.Command{
		Use:   `join "<name>"`,
		Short: "Join an active named comms session",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runSessionJoin(args[0], label)
		},
	}
	cmd.Flags().StringVar(&label, "label", "", "friendly display label for this actor in status/UI")
	return cmd
}

func newSessionEndCmd() *cobra.Command {
	var reason string
	cmd := &cobra.Command{
		Use:   `end "<name>"`,
		Short: "End one named comms session",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runSessionEnd(args[0], reason)
		},
	}
	cmd.Flags().StringVar(&reason, "reason", "", "audit reason for ending the named session")
	return cmd
}

func newSessionRetireCmd() *cobra.Command {
	var reason string
	cmd := &cobra.Command{
		Use:   "retire <actor>",
		Short: "Remove an actor from active sessions",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runSessionRetire(args[0], reason)
		},
	}
	cmd.Flags().StringVar(&reason, "reason", "", "audit reason for retiring the actor")
	return cmd
}

func newSessionLeadCmd() *cobra.Command {
	var reason string
	cmd := &cobra.Command{
		Use:   "lead [<actor>]",
		Short: "Make one active actor the leader",
		Args:  cobra.MaximumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			target := ""
			if len(args) == 1 {
				target = args[0]
			}
			return runSessionLead(target, reason)
		},
	}
	cmd.Flags().StringVar(&reason, "reason", "", "audit reason for leader transfer")
	return cmd
}

func runSessionRetire(target, reason string) error {
	target = strings.TrimSpace(target)
	if target == "" {
		return fmt.Errorf("session retire: actor is required")
	}
	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		return err
	}
	defer rt.Close()
	released, err := appendSessionRetire(rt, target, reason)
	if err != nil {
		return err
	}
	fmt.Printf("Retired @%s from active sessions; released %d claim%s. History remains in the append-only log.\n", target, released, pluralS(released))
	return nil
}

func runSessionLead(target, reason string) error {
	target = strings.TrimSpace(target)
	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		return err
	}
	defer rt.Close()
	if target == "" {
		target = rt.Actor
	}
	if err := appendLeaderTransfer(rt, target, reason); err != nil {
		return err
	}
	fmt.Printf("@%s is now the comms leader.\n", target)
	return nil
}

func runSessionStart(name, label string) error {
	name = strings.TrimSpace(name)
	if name == "" {
		return fmt.Errorf("session start: name is required")
	}
	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		return err
	}
	defer rt.Close()
	if id, _ := activeCommsSessionByName(rt.State, name); id != "" {
		return fmt.Errorf("session start: %q is already active; use `comms session join %q`", name, name)
	}
	now := time.Now().UTC()
	id := event.NewID(now)
	if err := appendSessionHello(rt, now, id, name, label, true); err != nil {
		return err
	}
	fmt.Printf("@%s started and joined comms session %q.\n  Session ID: %s\n", rt.Actor, name, id)
	return nil
}

func runSessionJoin(name, label string) error {
	name = strings.TrimSpace(name)
	if name == "" {
		return fmt.Errorf("session join: name is required")
	}
	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		return err
	}
	defer rt.Close()
	id, canonicalName := activeCommsSessionByName(rt.State, name)
	if id == "" {
		return fmt.Errorf("session join: no active comms session named %q; create it with `comms session start %q`", name, name)
	}
	if err := appendSessionHello(rt, time.Now().UTC(), id, canonicalName, label, false); err != nil {
		return err
	}
	fmt.Printf("@%s joined comms session %q.\n  Session ID: %s\n", rt.Actor, canonicalName, id)
	return nil
}

func runSessionEnd(name, reason string) error {
	name = strings.TrimSpace(name)
	if name == "" {
		return fmt.Errorf("session end: name is required")
	}
	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		return err
	}
	defer rt.Close()
	id, canonicalName := activeCommsSessionByName(rt.State, name)
	if id == "" {
		return fmt.Errorf("session end: no active comms session named %q", name)
	}
	if reason = strings.TrimSpace(reason); reason == "" {
		reason = "named comms session ended"
	}
	claims := activeClaimsByCommsSession(rt.State, id)
	refs := make([]interface{}, 0, len(claims))
	for _, c := range claims {
		refs = append(refs, c.ID)
	}
	now := time.Now().UTC()
	ev := event.Event{
		TS:    now,
		ID:    event.NewID(now),
		Actor: rt.Actor,
		Type:  event.TypeRelease,
		Data: map[string]interface{}{
			"refs":               refs,
			"comms_session_end":  true,
			"comms_session_id":   id,
			"comms_session_name": canonicalName,
			"reason":             reason,
		},
	}
	if err := rt.Append(ev); err != nil {
		return err
	}
	fmt.Printf("Ended comms session %q; released %d claim%s.\n", canonicalName, len(refs), pluralS(len(refs)))
	return nil
}

func appendSessionRetire(rt *Runtime, target, reason string) (int, error) {
	target = strings.TrimSpace(target)
	if target == "" {
		return 0, fmt.Errorf("session retire: actor is required")
	}
	claims := rt.State.ActiveClaimsByActor(target)
	if rt.State.Sessions[target] == nil && len(claims) == 0 {
		return 0, fmt.Errorf("session retire: @%s has no active session or claims", target)
	}
	if reason = strings.TrimSpace(reason); reason == "" {
		reason = "retired from active sessions"
	}
	refs := make([]interface{}, 0, len(claims))
	for _, c := range claims {
		refs = append(refs, c.ID)
	}
	now := time.Now().UTC()
	ev := event.Event{
		TS:    now,
		ID:    event.NewID(now),
		Actor: rt.Actor,
		Type:  event.TypeRelease,
		Data: map[string]interface{}{
			"refs":           refs,
			"session_retire": true,
			"retired_actor":  target,
			"reason":         reason,
		},
	}
	if err := rt.Append(ev); err != nil {
		return 0, err
	}
	return len(refs), nil
}

func appendLeaderTransfer(rt *Runtime, target, reason string) error {
	target = strings.TrimSpace(target)
	if target == "" {
		target = rt.Actor
	}
	if rt.State.Sessions[target] == nil {
		return fmt.Errorf("session lead: @%s is not active; run `COMMS_ACTOR=%s comms hello --label \"...\"` first", target, target)
	}
	if reason = strings.TrimSpace(reason); reason == "" {
		reason = "leader transfer"
	}
	now := time.Now().UTC()
	data := map[string]interface{}{
		"leader_transfer": true,
		"leader_actor":    target,
		"reason":          reason,
	}
	stampActiveCommsSession(rt, data)
	return rt.Append(event.Event{
		TS:    now,
		ID:    event.NewID(now),
		Actor: rt.Actor,
		Type:  event.TypeRelease,
		Data:  data,
	})
}

func activeCommsSessionByName(s *state.State, name string) (string, string) {
	name = strings.TrimSpace(name)
	if s == nil || name == "" {
		return "", ""
	}
	for _, sess := range s.Sessions {
		if sess.SessionID == "" {
			continue
		}
		if strings.EqualFold(sess.SessionName, name) {
			return sess.SessionID, sess.SessionName
		}
	}
	return "", ""
}

func activeClaimsByCommsSession(s *state.State, sessionID string) []*state.Claim {
	if s == nil || sessionID == "" {
		return nil
	}
	var out []*state.Claim
	for _, claim := range sortedClaims(s) {
		if claim.SessionID == sessionID {
			out = append(out, claim)
		}
	}
	return out
}

func stampActiveCommsSession(rt *Runtime, data map[string]interface{}) {
	if rt == nil || rt.State == nil || data == nil {
		return
	}
	if _, ok := data["comms_session_id"]; ok {
		return
	}
	sess := rt.State.Sessions[rt.Actor]
	if sess == nil || sess.SessionID == "" {
		return
	}
	data["comms_session_id"] = sess.SessionID
	data["comms_session_name"] = sess.SessionName
}

func appendSessionHello(rt *Runtime, now time.Time, sessionID, sessionName, label string, start bool) error {
	hostname, _ := os.Hostname()
	tty := readTTY()
	baseName := baseNameOfActor(rt.Actor)
	activeLeader := activeLeaderActor(rt.State, now.Add(-4*time.Hour))
	isLeader := activeLeader == "" || activeLeader == rt.Actor
	data := map[string]interface{}{
		"base_name":          baseName,
		"hostname":           hostname,
		"tty":                tty,
		"leader":             isLeader,
		"comms_session_id":   sessionID,
		"comms_session_name": sessionName,
	}
	if start {
		data["comms_session_start"] = true
		data["reason"] = sessionName
	} else {
		data["comms_session_join"] = true
	}
	if label = strings.TrimSpace(label); label != "" {
		data["label"] = label
	}
	return rt.Append(event.Event{
		TS:    now,
		ID:    event.NewID(now),
		Actor: rt.Actor,
		Type:  event.TypeHello,
		Data:  data,
	})
}
