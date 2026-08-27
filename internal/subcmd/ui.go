package subcmd

// THE DASHBOARD IS THE PYTHON BUILD'S NOW, AND THERE IS ONLY ONE OF IT.
//
// This file used to hold a second dashboard, 7,177 lines of it across ui.go,
// ui_stream.go and ui_task.go. It read the same log and drew a different
// picture, and keeping the two level meant building every improvement twice.
// That is not a hypothetical cost. Over one day of work the Python board gained
// real project names, a filter that hides deleted and throwaway stores, the
// working tree read from git, the commit-guard warning, folded closed work, the
// task graph and the code map. This one gained none of them, and the user found
// out by opening it and seeing the exact complaint that started the work: a
// sidebar listing 176 projects by hash.
//
// So `comms ui` is now a launcher. Every existing entry point keeps working —
// muscle memory, the launchd service, the README, a shell alias — and there is
// one dashboard to fix.

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
	"syscall"

	"github.com/spf13/cobra"
)

// pythonUI is the command that actually serves the board.
const pythonUI = "comms-graph"

// NewUICmd hands the dashboard to the Python build.
func NewUICmd() *cobra.Command {
	addr := "127.0.0.1:7878"
	noOpen := false
	graph := ""
	cmd := &cobra.Command{
		Use:   "ui",
		Short: "Serve the local dashboard",
		Long: `Serve the local dashboard.

The dashboard is served by the Python build (` + pythonUI + `), and this command
runs it for you. There used to be two dashboards reading the same log and
drawing different pictures; the second one fell behind and nobody noticed until
it was opened. There is one now.

It shows every comms project on this machine, with a sidebar to focus one.
Beside the claims it shows what is actually changed on disk according to git,
whether this repo has a commit guard installed, the task graph and the code map.

Scope it to one repo with the global --repo, and reach the rest with
` + pythonUI + ` ui --help.`,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runUI(addr, graph, noOpen, cmd.Flags().Changed("addr"))
		},
	}
	cmd.Flags().StringVar(&addr, "addr", addr, "listen address")
	cmd.Flags().BoolVar(&noOpen, "no-open", false, "do not open a browser (useful for scripts, cron, and hooks)")
	cmd.Flags().StringVar(&graph, "graph", "", "path to a graphify graph.json for the code map")
	// Accepted and ignored rather than rejected: they exist in muscle memory, in
	// the launchd template, and in at least one shell alias. Failing on them
	// would turn a rename into an outage for the person least expecting one.
	for _, dead := range []struct{ name, why string }{
		{"all", "the unified all-projects view is the default"},
		{"open", "the browser opens automatically when run interactively"},
		{"demo", "not offered by the dashboard any more"},
		{"stale-after", "the board derives staleness from when a holder last spoke"},
	} {
		cmd.Flags().String(dead.name, "", "deprecated: "+dead.why)
		_ = cmd.Flags().MarkHidden(dead.name)
	}
	return cmd
}

func runUI(addr, graph string, noOpen, addrGiven bool) error {
	bin, err := exec.LookPath(pythonUI)
	if err != nil {
		return fmt.Errorf(`ui: the dashboard is served by %[1]s, which is not on your PATH.

  Install it:  pipx install comms-graph
           or: pip install --user comms-graph

  Everything else in this binary works without it; only the dashboard moved`, pythonUI)
	}

	argv := []string{pythonUI, "ui"}
	if addrGiven {
		// `--addr host:port` is one flag here and two there. Splitting it is
		// what keeps an existing launchd plist and every alias working.
		host, port, ok := splitAddr(addr)
		if !ok {
			return fmt.Errorf("ui: --addr %q is not host:port", addr)
		}
		if host != "" {
			argv = append(argv, "--host", host)
		}
		if port != "" {
			argv = append(argv, "--port", port)
		}
	}
	if noOpen {
		argv = append(argv, "--no-open")
	}
	if graph != "" {
		argv = append(argv, "--graph", graph)
	}
	// The repo, when this binary was scoped to one, so `comms --repo X ui`
	// still means what it says.
	if globalRepoRoot != "" {
		argv = append([]string{pythonUI, "--repo", globalRepoRoot}, argv[1:]...)
	}

	// REPLACE this process rather than spawning a child. The dashboard is a
	// long-running foreground process: a wrapper sitting in front of it would
	// swallow Ctrl-C, hide the real exit status, and leave launchd supervising
	// the wrong pid.
	return syscall.Exec(bin, argv, os.Environ())
}

// splitAddr splits host:port, allowing a bare port and a bare host.
func splitAddr(addr string) (host, port string, ok bool) {
	i := strings.LastIndex(addr, ":")
	if i < 0 {
		// A bare number is a port, which is what somebody typing quickly means.
		if addr != "" && strings.IndexFunc(addr, func(r rune) bool { return r < '0' || r > '9' }) < 0 {
			return "", addr, true
		}
		return addr, "", addr != ""
	}
	return addr[:i], addr[i+1:], true
}
