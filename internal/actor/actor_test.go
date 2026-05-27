package actor

import (
	"os"
	"strings"
	"testing"
)

func withEnv(t *testing.T, kv map[string]string) {
	t.Helper()
	saved := make(map[string]string)
	saved["COMMS_ACTOR"], _ = os.LookupEnv("COMMS_ACTOR")
	saved["COMMS_ALLOW_GENERIC_ACTOR"], _ = os.LookupEnv("COMMS_ALLOW_GENERIC_ACTOR")
	saved["USER"], _ = os.LookupEnv("USER")

	for k, v := range kv {
		if v == "" {
			os.Unsetenv(k)
		} else {
			os.Setenv(k, v)
		}
	}
	t.Cleanup(func() {
		for k, v := range saved {
			if v == "" {
				os.Unsetenv(k)
			} else {
				os.Setenv(k, v)
			}
		}
	})
}

func TestResolveReadOnlyAllowsEmpty(t *testing.T) {
	withEnv(t, map[string]string{"COMMS_ACTOR": "", "USER": "eli"})
	got, err := Resolve(ReadOnly)
	if err != nil {
		t.Fatalf("ReadOnly with empty actor should not error: %v", err)
	}
	if got != "" {
		t.Fatalf("expected empty, got %q", got)
	}
}

func TestResolveMutatingRequires(t *testing.T) {
	withEnv(t, map[string]string{"COMMS_ACTOR": "", "USER": "eli"})
	_, err := Resolve(Mutating)
	if err == nil || !strings.Contains(err.Error(), "COMMS_ACTOR unset") {
		t.Fatalf("expected unset error, got %v", err)
	}
}

func TestRejectGenericNames(t *testing.T) {
	cases := []string{"eli", "Eli", "ELI", "claude", "Codex", "agent", "user"}
	for _, c := range cases {
		t.Run(c, func(t *testing.T) {
			withEnv(t, map[string]string{"COMMS_ACTOR": c, "USER": "eli"})
			_, err := Resolve(Mutating)
			if err == nil {
				t.Fatalf("expected rejection of %q", c)
			}
			if !strings.Contains(err.Error(), "looks like a per-user name") {
				t.Fatalf("wrong error message: %v", err)
			}
		})
	}
}

func TestRejectsUSEREquality(t *testing.T) {
	withEnv(t, map[string]string{"COMMS_ACTOR": "bob", "USER": "bob"})
	_, err := Resolve(Mutating)
	if err == nil {
		t.Fatalf("expected rejection when COMMS_ACTOR == $USER")
	}
}

func TestAcceptsConcreteSessionNames(t *testing.T) {
	cases := []string{"claude-3a1f", "codex-9b2c", "human-eli", "agent-0", "Claude-3a1f"}
	for _, c := range cases {
		t.Run(c, func(t *testing.T) {
			withEnv(t, map[string]string{"COMMS_ACTOR": c, "USER": "eli"})
			got, err := Resolve(Mutating)
			if err != nil {
				t.Fatalf("expected acceptance of %q, got %v", c, err)
			}
			if got != c {
				t.Fatalf("expected %q, got %q", c, got)
			}
		})
	}
}

func TestAllowGenericOverride(t *testing.T) {
	withEnv(t, map[string]string{"COMMS_ACTOR": "eli", "COMMS_ALLOW_GENERIC_ACTOR": "1", "USER": "eli"})
	got, err := Resolve(Mutating)
	if err != nil {
		t.Fatalf("override should allow generic name: %v", err)
	}
	if got != "eli" {
		t.Fatalf("got %q", got)
	}
}

func TestRejectControlCharacters(t *testing.T) {
	cases := []string{"claude\n3a1f", "claude\t3a1f", "claude 3a1f", "claude;rm-rf", "claude/etc"}
	for _, c := range cases {
		t.Run(c, func(t *testing.T) {
			withEnv(t, map[string]string{"COMMS_ACTOR": c})
			_, err := Resolve(Mutating)
			if err == nil {
				t.Fatalf("expected rejection of %q", c)
			}
		})
	}
}

func TestRejectExcessivelyLongName(t *testing.T) {
	long := strings.Repeat("a", 100)
	withEnv(t, map[string]string{"COMMS_ACTOR": long})
	_, err := Resolve(Mutating)
	if err == nil {
		t.Fatalf("expected length rejection")
	}
}

func TestRejectNonASCII(t *testing.T) {
	withEnv(t, map[string]string{"COMMS_ACTOR": "claudé-3a1f"})
	_, err := Resolve(Mutating)
	if err == nil {
		t.Fatalf("expected ASCII rejection")
	}
}
