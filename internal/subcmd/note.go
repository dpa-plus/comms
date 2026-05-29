package subcmd

import (
	"fmt"
	"time"
	"unicode/utf8"

	"github.com/dpa-plus/comms/internal/event"
	"github.com/spf13/cobra"
)

// maxNoteRunes caps the body length. 200 Unicode runes (scalar values), NOT
// bytes — per the plan's LOW-severity clarification.
const maxNoteRunes = 200

// NewNoteCmd builds `comms note "<≤200-char FYI>"`. Short ephemeral messages
// to the team that don't require any acknowledgment.
func NewNoteCmd() *cobra.Command {
	var priority bool
	cmd := &cobra.Command{
		Use:   `note [--priority] "<≤200-char FYI>"`,
		Short: "Post a short FYI",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runNote(args[0], priority)
		},
	}
	cmd.Flags().BoolVar(&priority, "priority", false, "leader-only: pin this note as high priority in status/UI")
	return cmd
}

func runNote(body string, priority bool) error {
	if body == "" {
		Fatalf(2, "note: body is empty")
	}
	if utf8.RuneCountInString(body) > maxNoteRunes {
		Fatalf(2, "note: body exceeds %d runes (got %d)", maxNoteRunes, utf8.RuneCountInString(body))
	}

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		return err
	}
	defer rt.Close()
	if priority {
		requireLeader(rt)
	}

	now := time.Now().UTC()
	data := map[string]interface{}{"body": body}
	if priority {
		data["priority"] = true
	}
	stampActiveCommsSession(rt, data)
	ev := event.Event{
		TS:    now,
		ID:    event.NewID(now),
		Actor: rt.Actor,
		Type:  event.TypeNote,
		Data:  data,
	}
	if err := rt.Append(ev); err != nil {
		return err
	}
	label := "noted"
	if priority {
		label = "priority note"
	}
	fmt.Printf("@%s %s: %s\n", rt.Actor, label, body)
	return nil
}
