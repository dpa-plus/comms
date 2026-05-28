package subcmd

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/dpa-plus/comms/internal/actor"
	"github.com/dpa-plus/comms/internal/event"
	"github.com/spf13/cobra"
)

// NewHelloCmd announces a session in the log and prints the active actor
// prominently (first line of output) so misconfiguration is visible.
func NewHelloCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "hello [<name>]",
		Short: "Announce this session in the log",
		Long: `Register the current session in the comms log.

If <name> is supplied, it overrides $COMMS_ACTOR for this single command.
Without an argument, $COMMS_ACTOR must be set (or this is a read-only
"who am I" lookup).`,
		Args: cobra.MaximumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runHello(args)
		},
	}
	return cmd
}

func runHello(args []string) error {
	// If an explicit name is given, that becomes the actor for this call.
	// We still validate it against the same rules COMMS_ACTOR would face.
	if len(args) == 1 {
		os.Setenv(actor.EnvVar, args[0])
	}
	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		return err
	}
	defer rt.Close()

	hostname, _ := os.Hostname()
	tty := readTTY()
	baseName := baseNameOfActor(rt.Actor)
	now := time.Now().UTC()
	activeLeader := activeLeaderActor(rt.State, now.Add(-4*time.Hour))
	isLeader := activeLeader == "" || activeLeader == rt.Actor

	ev := event.Event{
		TS:    now,
		ID:    event.NewID(now),
		Actor: rt.Actor,
		Type:  event.TypeHello,
		Data: map[string]interface{}{
			"base_name": baseName,
			"hostname":  hostname,
			"tty":       tty,
			"leader":    isLeader,
		},
	}
	if err := rt.Append(ev); err != nil {
		return err
	}

	// Count how many other sessions share this base name (the count is for
	// the "1st claude session active right now" line).
	activeForBase := 0
	for _, s := range rt.State.Sessions {
		if baseNameOfActor(s.Actor) == baseName {
			activeForBase++
		}
	}
	if activeForBase == 0 {
		activeForBase = 1
	}

	// FIRST LINE: actor name. Visible even when output scrolls.
	fmt.Printf("@%s registered.\n", rt.Actor)
	if isLeader {
		fmt.Println("  Role:    leader (can post priority notes/findings)")
	}
	fmt.Printf("  (%d %s session%s active right now.)\n", activeForBase, baseName, pluralS(activeForBase))
	fmt.Printf("  Project: %s  (hash: %s)\n", rt.Repo.Name, rt.Repo.Hash)
	fmt.Printf("  Log:     %s\n", rt.Paths.Log)
	if actor.IsGeneric(rt.Actor) {
		fmt.Println()
		fmt.Println("  NOTE: This actor name is generic (would normally be rejected).")
		fmt.Println("  COMMS_ALLOW_GENERIC_ACTOR is set — be aware that conflict-detection")
		fmt.Println("  is degraded when multiple runners share an actor name.")
	}
	fmt.Println()
	fmt.Println("If this is the wrong actor name, set $COMMS_ACTOR and re-run `comms hello`.")
	return nil
}

// baseNameOfActor returns everything before the first `-` — `claude-3a1f`
// → `claude`, `human-eli` → `human`, `alice` → `alice`.
func baseNameOfActor(name string) string {
	if i := strings.IndexByte(name, '-'); i > 0 {
		return name[:i]
	}
	return name
}

// readTTY returns the controlling-terminal device name, best effort.
func readTTY() string {
	// `tty` invocation; if not a TTY, it exits nonzero with "not a tty".
	out, err := exec.Command("tty").Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

func pluralS(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}
