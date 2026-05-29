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
	// NFC-normalized: precomposed "é" and decomposed "e"+"´" should
	// canonicalize to the same symbol.
	if !Scopes(parse("src/foo.ts#Café"), parse("src/foo.ts#Cafe\u0301")) {
		t.Errorf("symbols must be NFC-normalized before comparison")
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
		{"src/foo.ts#L-5", true},   // missing start
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
