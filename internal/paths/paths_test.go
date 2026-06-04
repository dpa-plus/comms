package paths

import "testing"

func TestIsEphemeralPath(t *testing.T) {
	cases := []struct {
		path string
		want bool
	}{
		// Throwaway temp roots — a HOME override (HOME=/tmp etc.) lands here.
		{"/tmp/Library/Application Support/comms/abc123", true},
		{"/private/tmp/Library/Application Support/comms/abc123", true},
		{"/var/folders/xy/zz/T/Library/Application Support/comms/abc123", true},
		{"/private/var/folders/xy/zz/T/.local/share/comms/abc123", true},
		// Real per-user stores — must NOT warn.
		{"/Users/you/Library/Application Support/comms/abc123", false},
		{"/home/you/.local/share/comms/abc123", false},
		// Prefix look-alikes must not match the temp roots.
		{"/tmpfoo/comms", false},
		{"/var/folders-not-temp/comms", false},
	}
	for _, c := range cases {
		if got := isEphemeralPath(c.path); got != c.want {
			t.Errorf("isEphemeralPath(%q) = %v, want %v", c.path, got, c.want)
		}
	}
}

func TestEphemeralStoreUsesLogDir(t *testing.T) {
	if !(Paths{LogDir: "/tmp/Library/Application Support/comms/x"}).EphemeralStore() {
		t.Error("EphemeralStore should be true for a /tmp LogDir")
	}
	if (Paths{LogDir: "/Users/you/Library/Application Support/comms/x"}).EphemeralStore() {
		t.Error("EphemeralStore should be false for a real home LogDir")
	}
}
