package subcmd

import (
	"os"
	"strings"
	"testing"
)

// The dashboard moved to the Python build. What is left here is a launcher, and
// everything worth testing about a launcher is whether the ways people already
// invoke it still work.

func TestAddrSplitsIntoHostAndPortForThePythonBoard(t *testing.T) {
	// IF THIS FAILS: an existing launchd plist or shell alias passing
	// --addr stops reaching the dashboard, which is a rename turning into an
	// outage for the person least expecting one.
	cases := []struct{ in, host, port string }{
		{"127.0.0.1:7878", "127.0.0.1", "7878"},
		{"0.0.0.0:8080", "0.0.0.0", "8080"},
		{":7878", "", "7878"},
		{"7878", "", "7878"}, // a bare number is what somebody typing quickly means
		{"localhost", "localhost", ""},
	}
	for _, c := range cases {
		host, port, ok := splitAddr(c.in)
		if !ok || host != c.host || port != c.port {
			t.Errorf("splitAddr(%q) = %q, %q, %v; want %q, %q, true",
				c.in, host, port, ok, c.host, c.port)
		}
	}
}

func TestTheFlagsThatWentAwayAreAcceptedNotRejected(t *testing.T) {
	// IF THIS FAILS: `comms ui --demo` or `--stale-after 2h` starts erroring.
	// Those live in muscle memory and in at least one committed template, and
	// refusing them buys nothing: the launcher simply has nowhere to send them.
	for _, flag := range []string{"all", "open", "demo", "stale-after"} {
		if f := NewUICmd().Flags().Lookup(flag); f == nil {
			t.Errorf("--%s is gone; it should be accepted and ignored", flag)
		} else if !f.Hidden {
			t.Errorf("--%s should be hidden, it is deprecated", flag)
		}
	}
}

func TestAMissingDashboardSaysHowToInstallIt(t *testing.T) {
	// IF THIS FAILS: somebody with only the Go binary runs `comms ui`, gets
	// "executable file not found in $PATH", and has no idea the dashboard is a
	// separate install.
	dir := t.TempDir()
	old := os.Getenv("PATH")
	t.Setenv("PATH", dir)
	defer os.Setenv("PATH", old)

	err := runUI("127.0.0.1:7878", "", true, false)
	if err == nil {
		t.Fatal("expected an error when comms-graph is not on PATH")
	}
	for _, want := range []string{"comms-graph", "install"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the error should mention %q; got: %v", want, err)
		}
	}
}
