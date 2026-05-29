package subcmd

import "testing"

// TestFindRefFlagDoesNotSplitOnComma guards bug #R2-13: --ref must be taken
// verbatim (StringArrayVar), NOT CSV-split (StringSliceVar). A value whose
// kind:value contains a comma — e.g. a URL with query params — must remain a
// single ref.
func TestFindRefFlagDoesNotSplitOnComma(t *testing.T) {
	cmd := NewFindCmd()
	// Two distinct --ref occurrences; the first value embeds a comma.
	if err := cmd.Flags().Set("ref", "url:http://x?a=1,b=2"); err != nil {
		t.Fatalf("set ref 1: %v", err)
	}
	if err := cmd.Flags().Set("ref", "commit:cece752"); err != nil {
		t.Fatalf("set ref 2: %v", err)
	}

	got, err := cmd.Flags().GetStringArray("ref")
	if err != nil {
		t.Fatalf("get ref: %v", err)
	}
	want := []string{"url:http://x?a=1,b=2", "commit:cece752"}
	if len(got) != len(want) {
		t.Fatalf("ref count = %d %v, want %d %v", len(got), got, len(want), want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("ref[%d] = %q, want %q", i, got[i], want[i])
		}
	}

	// parseRefs must then yield exactly two refs, the first keeping the comma
	// in its value (split is on the FIRST colon only).
	parsed, err := parseRefs(got)
	if err != nil {
		t.Fatalf("parseRefs: %v", err)
	}
	if len(parsed) != 2 {
		t.Fatalf("parsed ref count = %d, want 2", len(parsed))
	}
	if parsed[0].kind != "url" || parsed[0].value != "http://x?a=1,b=2" {
		t.Fatalf("parsed[0] = %+v, want {url http://x?a=1,b=2}", parsed[0])
	}
	if parsed[1].kind != "commit" || parsed[1].value != "cece752" {
		t.Fatalf("parsed[1] = %+v, want {commit cece752}", parsed[1])
	}
}
