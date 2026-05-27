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
	cmd := &cobra.Command{
		Use:   `note "<≤200-char FYI>"`,
		Short: "Post a short FYI",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runNote(args[0])
		},
	}
	return cmd
}

func runNote(body string) error {
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

	now := time.Now().UTC()
	ev := event.Event{
		TS:    now,
		ID:    event.NewID(now),
		Actor: rt.Actor,
		Type:  event.TypeNote,
		Data:  map[string]interface{}{"body": body},
	}
	if err := rt.Append(ev); err != nil {
		return err
	}
	fmt.Printf("@%s noted: %s\n", rt.Actor, body)
	return nil
}
