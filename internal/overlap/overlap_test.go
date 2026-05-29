package overlap

import "testing"

func TestPathsOverlap_RequiredCases(t *testing.T) {
	// These ten cases are pinned in the plan's "Glob algorithm" section.
	cases := []struct {
		a, b string
		want bool
	}{
		{"src/**", "src/foo.ts", true},
		{"src/**", "src/foo/bar.ts", true},
		{"src/**", "srcs/foo.ts", false},
		{"src/*.ts", "src/foo.ts", true},
		{"src/*.ts", "src/foo/bar.ts", false},
		{"src/*/foo.ts", "src/a/b/foo.ts", false},
		{"src/foo.ts", "src/foo.ts", true},
		{"**/foo.ts", "src/foo.ts", true},
		{"src/**/test/**", "src/a/test/b.ts", true},
		{"src/**/foo", "src/foo", true},
	}
	for _, c := range cases {
		t.Run(c.a+" ∩ "+c.b, func(t *testing.T) {
			got := PathsOverlap(c.a, c.b)
			if got != c.want {
				t.Fatalf("PathsOverlap(%q,%q) = %v, want %v", c.a, c.b, got, c.want)
			}
			// Should be symmetric.
			gotRev := PathsOverlap(c.b, c.a)
			if gotRev != c.want {
				t.Fatalf("PathsOverlap symmetric mismatch (%q,%q): %v vs %v", c.a, c.b, got, gotRev)
			}
		})
	}
}

func TestPathsOverlap_AdditionalEdgeCases(t *testing.T) {
	cases := []struct {
		a, b string
		want bool
	}{
		// Identical paths.
		{"a", "a", true},
		{"a/b/c", "a/b/c", true},

		// Different segment counts without glob.
		{"a/b", "a/b/c", false},
		{"a", "a/b", false},

		// `*` does not cross segments.
		{"*", "a/b", false},
		{"a/*", "a/b", true},

		// `**` matches zero segments mid-pattern.
		{"a/**/b", "a/b", true},

		// Trailing `**` matches zero or more.
		{"a/**", "a", true},
		{"a", "a/**", true},

		// Leading `**` matches any prefix.
		{"**/x", "a/b/x", true},
		{"**/x", "x", true},

		// Different bases.
		{"a/b", "x/y", false},
	}
	for _, c := range cases {
		t.Run(c.a+" ∩ "+c.b, func(t *testing.T) {
			got := PathsOverlap(c.a, c.b)
			if got != c.want {
				t.Fatalf("PathsOverlap(%q,%q) = %v, want %v", c.a, c.b, got, c.want)
			}
		})
	}
}

// TestPathsOverlap_InteriorLiteralFragments covers BUG 1: the old
// prefix/suffix-anchor heuristic ignored interior literal fragments, reporting
// false-positive overlaps. The DP intersection test must honor required
// characters and minimum length imposed by literals between stars.
func TestPathsOverlap_InteriorLiteralFragments(t *testing.T) {
	cases := []struct {
		a, b string
		want bool
	}{
		// "a*a" needs at least two 'a's; the plain literal "a" cannot satisfy
		// the trailing 'a'. Old code returned true (anchors a/a matched).
		{"a*a", "a", false},
		{"a", "a*a", false},
		// "aa" is the shortest match for "a*a"; it must overlap.
		{"a*a", "aa", true},
		{"a*a", "aba", true},
		// Mandatory interior 'b' is absent from "axc".
		{"a*b*c", "axc", false},
		{"a*b*c", "abc", true},
		{"a*b*c", "axbyc", true},
		// Suffix anchors match ("c"/"c") but the interior 'b' is required.
		{"ab*c", "axc", false},
		{"ab*c", "abxc", true},
		// Pure star still matches anything in-segment.
		{"*", "anything", true},
		{"a*", "abc", true},
		{"*c", "abc", true},
		// Two globs whose anchors look compatible but lengths conflict.
		{"x*y*z", "xz", false},
		{"x*y*z", "xyz", true},
		// Star against star with mandatory literals on both sides.
		{"a*c", "a*x", false}, // one demands trailing c, other trailing x
		{"a*c", "a*c", true},
		{"ab*", "a*c", true}, // "abc" matches both
	}
	for _, c := range cases {
		t.Run(c.a+" ∩ "+c.b, func(t *testing.T) {
			got := PathsOverlap(c.a, c.b)
			if got != c.want {
				t.Fatalf("PathsOverlap(%q,%q) = %v, want %v", c.a, c.b, got, c.want)
			}
			// Single-segment intersection must be symmetric.
			if rev := PathsOverlap(c.b, c.a); rev != c.want {
				t.Fatalf("PathsOverlap not symmetric for (%q,%q): %v vs %v", c.a, c.b, got, rev)
			}
		})
	}
}

// TestParse_SymbolAnchorsThatLookLikeRanges covers BUG 2: symbol names that
// start with 'L' and contain '-' must parse as SYMBOL anchors, not be rejected
// as malformed line ranges.
func TestParse_SymbolAnchorsThatLookLikeRanges(t *testing.T) {
	symbols := []string{"List-impl", "Loader-2", "L-value", "Linked-list", "L-5", "Lo-fi"}
	for _, name := range symbols {
		t.Run(name, func(t *testing.T) {
			s, err := Parse("src/foo.ts#" + name)
			if err != nil {
				t.Fatalf("Parse symbol-like anchor %q: unexpected error %v", name, err)
			}
			if s.Anchor.Kind != AnchorSymbol {
				t.Fatalf("anchor %q: kind = %v, want AnchorSymbol", name, s.Anchor.Kind)
			}
			if s.Anchor.Symbol != name {
				t.Fatalf("anchor %q: symbol = %q, want %q", name, s.Anchor.Symbol, name)
			}
		})
	}
	// Genuine ranges must still parse as line ranges.
	if s, err := Parse("src/foo.ts#L1-10"); err != nil || s.Anchor.Kind != AnchorLine {
		t.Fatalf("L1-10 should be a line range: kind=%v err=%v", s.Anchor.Kind, err)
	}
}

// TestParse_RejectsControlCharacters covers BUG 3: C0/C1/DEL control bytes in
// either the path or the anchor must be rejected so terminal-escape sequences
// can never be persisted.
func TestParse_RejectsControlCharacters(t *testing.T) {
	bad := []struct {
		name, in string
	}{
		{"esc-in-path", "src/\x1bfoo.ts"},
		{"nul-in-path", "src/\x00foo.ts"},
		{"bell-in-path", "src/foo\x07.ts"},
		{"del-in-path", "src/foo\x7f.ts"},
		{"tab-in-path", "src/foo\t.ts"},
		{"esc-in-symbol", "src/foo.ts#ba\x1br"},
		{"nul-in-symbol", "src/foo.ts#ba\x00r"},
		{"del-in-symbol", "src/foo.ts#ba\x7fr"},
		{"newline-in-symbol", "src/foo.ts#ba\nr"},
		// C1 CSI (U+009B) is valid UTF-8 (bytes 0xC2 0x9B) yet many terminals
		// interpret it like ESC[, so it must be rejected in BOTH a path and a
		// symbol anchor. Built as a real code point — not a raw byte — so it
		// survives the symbol branch's utf8.ValidString check and would slip
		// past a C0/DEL-only predicate.
		{"c1-csi-in-path", "src/foo" + string(rune(0x9b)) + "2K.ts"},
		{"c1-csi-in-symbol", "src/foo.ts#foo" + string(rune(0x9b)) + "2K"},
	}
	for _, c := range bad {
		t.Run(c.name, func(t *testing.T) {
			if _, err := Parse(c.in); err == nil {
				t.Fatalf("Parse(%q) = nil error, want rejection of control character", c.in)
			}
		})
	}
	// NormalizePath (used by the policy loader) must reject them too.
	if _, err := NormalizePath("src/\x1bfoo.ts"); err == nil {
		t.Fatalf("NormalizePath should reject control characters")
	}
	// Sanity: normal printable Unicode must NOT be rejected.
	for _, ok := range []string{"src/Café.ts", "src/файл.ts", "src/foo.ts#Café"} {
		if _, err := Parse(ok); err != nil {
			t.Fatalf("Parse(%q) rejected legitimate Unicode: %v", ok, err)
		}
	}
}

func TestScopes_LineRanges(t *testing.T) {
	parse := func(s string) Scope {
		x, err := Parse(s)
		if err != nil {
			t.Fatalf("Parse %q: %v", s, err)
		}
		return x
	}
	cases := []struct {
		a, b string
		want bool
	}{
		{"src/foo.ts#L1-10", "src/foo.ts#L5-20", true},   // overlap
		{"src/foo.ts#L1-10", "src/foo.ts#L11-20", false}, // adjacent, disjoint
		{"src/foo.ts#L1-10", "src/foo.ts#L10-20", true},  // boundary touches
		{"src/foo.ts#L100-200", "src/foo.ts#L50-60", false},
	}
	for _, c := range cases {
		got := Scopes(parse(c.a), parse(c.b))
		if got != c.want {
			t.Errorf("Scopes(%s, %s) = %v, want %v", c.a, c.b, got, c.want)
		}
	}
}

func TestScopes_Symbols(t *testing.T) {
	parse := func(s string) Scope {
		x, err := Parse(s)
		if err != nil {
			t.Fatalf("Parse %q: %v", s, err)
		}
		return x
	}
	if !Scopes(parse("src/foo.ts#bar"), parse("src/foo.ts#bar")) {
		t.Errorf("same symbols should overlap")
	}
	if Scopes(parse("src/foo.ts#bar"), parse("src/foo.ts#baz")) {
		t.Errorf("different symbols should not overlap")
	}
	// Case-sensitive.
	if Scopes(parse("src/foo.ts#Bar"), parse("src/foo.ts#bar")) {
		t.Errorf("symbol comparison must be case-sensitive")
	}
}

func TestScopes_MixedPessimistic(t *testing.T) {
	parse := func(s string) Scope {
		x, err := Parse(s)
		if err != nil {
			t.Fatalf("Parse %q: %v", s, err)
		}
		return x
	}
	if !Scopes(parse("src/foo.ts#bar"), parse("src/foo.ts#L1-10")) {
		t.Errorf("mixed (symbol + line range) must pessimistically overlap")
	}
}

func TestScopes_WholeFile(t *testing.T) {
	parse := func(s string) Scope {
		x, err := Parse(s)
		if err != nil {
			t.Fatalf("Parse %q: %v", s, err)
		}
		return x
	}
	if !Scopes(parse("src/foo.ts"), parse("src/foo.ts#bar")) {
		t.Errorf("whole-file should conflict with anchored claim")
	}
	if !Scopes(parse("src/foo.ts#L1-10"), parse("src/foo.ts")) {
		t.Errorf("anchored should conflict with whole-file")
	}
	if !Scopes(parse("src/foo.ts"), parse("src/foo.ts")) {
		t.Errorf("two whole-file claims on same path should conflict")
	}
}

func TestParse_PathNormalization(t *testing.T) {
	cases := []struct {
		in, want string
		wantErr  bool
	}{
		{"src/foo.ts", "src/foo.ts", false},
		{"./src/foo.ts", "src/foo.ts", false},
		{"src/./foo.ts", "src/foo.ts", false},
		{"src/../src/foo.ts", "src/foo.ts", false},
		{"/etc/passwd", "", true},
		{"../escape", "", true},
		{"", "", true},
	}
	for _, c := range cases {
		t.Run(c.in, func(t *testing.T) {
			got, err := Parse(c.in)
			if c.wantErr {
				if err == nil {
					t.Fatalf("expected error for %q, got path %q", c.in, got.Path)
				}
				return
			}
			if err != nil {
				t.Fatalf("Parse %q: %v", c.in, err)
			}
			if got.Path != c.want {
				t.Errorf("path %q normalized to %q, want %q", c.in, got.Path, c.want)
			}
		})
	}
}

func TestParse_AnchorValidation(t *testing.T) {
	cases := []struct {
		in      string
		wantErr bool
	}{
		{"src/foo.ts#L1-10", false},
		{"src/foo.ts#L5-5", false}, // single line OK
		{"src/foo.ts#L10-5", true}, // inverted
		{"src/foo.ts#L0-10", true}, // zero
		{"src/foo.ts#L-5", false},  // not a range shape → valid SYMBOL "L-5"
		{"src/foo.ts#bar", false},
		{"src/foo.ts#", true},  // empty anchor
		{"src/foo.ts# ", true}, // whitespace-only
	}
	for _, c := range cases {
		t.Run(c.in, func(t *testing.T) {
			_, err := Parse(c.in)
			if c.wantErr && err == nil {
				t.Fatalf("expected error for %q", c.in)
			}
			if !c.wantErr && err != nil {
				t.Fatalf("unexpected error for %q: %v", c.in, err)
			}
		})
	}
}

func TestParse_HashEscape(t *testing.T) {
	// `weird\#name.ts` is a literal filename with `#` in it.
	s, err := Parse(`weird\#name.ts`)
	if err != nil {
		t.Fatalf("Parse with escaped hash: %v", err)
	}
	if s.Path != `weird#name.ts` {
		t.Errorf("expected unescaped path %q, got %q", `weird#name.ts`, s.Path)
	}
	if s.Anchor.Kind != AnchorWhole {
		t.Errorf("expected whole-file anchor, got %v", s.Anchor.Kind)
	}
}

func TestScopeStringEscapesLiteralHashInPath(t *testing.T) {
	s, err := Parse(`weird\#name.ts`)
	if err != nil {
		t.Fatalf("Parse with escaped hash: %v", err)
	}
	rendered := s.String()
	if rendered != `weird\#name.ts` {
		t.Fatalf("String() = %q, want escaped literal hash", rendered)
	}
	roundTrip, err := Parse(rendered)
	if err != nil {
		t.Fatalf("Parse rendered scope: %v", err)
	}
	if roundTrip.Path != s.Path || roundTrip.Anchor.Kind != AnchorWhole {
		t.Fatalf("round-trip changed scope: before=%+v after=%+v", s, roundTrip)
	}
}

func TestScope_String(t *testing.T) {
	parse := func(s string) Scope {
		x, err := Parse(s)
		if err != nil {
			t.Fatalf("Parse %q: %v", s, err)
		}
		return x
	}
	cases := []struct {
		in, want string
	}{
		{"src/foo.ts", "src/foo.ts"},
		{"src/foo.ts#L1-10", "src/foo.ts#L1-10"},
		{"src/foo.ts#bar", "src/foo.ts#bar"},
		{"./src/foo.ts", "src/foo.ts"},
	}
	for _, c := range cases {
		got := parse(c.in).String()
		if got != c.want {
			t.Errorf("String(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}
