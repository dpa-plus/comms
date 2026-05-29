package overlap

import "strings"

// PathsOverlap reports whether two POSIX-style glob patterns can match at
// least one path in common.
//
// This is a segment-aware string algorithm — we never expand against the
// real filesystem. Splits on `/`, recurses over segment pairs:
//
//	literal ∩ literal       → equal? then continue, else no-overlap
//	literal ∩ *             → single-segment match, continue
//	literal ∩ **            → ** consumes 1..n segments, recurse on both branches
//	* ∩ *                   → matches, continue
//	* ∩ **                  → ** consumes 1..n, recurse
//	** ∩ **                 → both consume freely
//
// Special handling:
//   - `**` matches zero or more segments (so `src/**/foo` overlaps `src/foo`).
//   - `*` matches exactly one segment.
//   - All other glob characters (?, [, ]) are treated as literals — overkill
//     to support full glob syntax for MVP.
//
// The function is total: it never panics and returns a definite yes/no.
func PathsOverlap(a, b string) bool {
	aSeg := strings.Split(a, "/")
	bSeg := strings.Split(b, "/")
	return segmentsOverlap(aSeg, bSeg)
}

// segmentsOverlap is the recursive worker. Operates on segment slices.
func segmentsOverlap(a, b []string) bool {
	// Base cases.
	if len(a) == 0 && len(b) == 0 {
		return true
	}
	if len(a) == 0 {
		// b is non-empty; only matches if every remaining b segment is `**`.
		return allDoubleStar(b)
	}
	if len(b) == 0 {
		return allDoubleStar(a)
	}

	// `**` on either side consumes 0..n segments from the OTHER side.
	if a[0] == "**" {
		// Try consuming 0..n segments of b.
		for k := 0; k <= len(b); k++ {
			if segmentsOverlap(a[1:], b[k:]) {
				return true
			}
		}
		return false
	}
	if b[0] == "**" {
		for k := 0; k <= len(a); k++ {
			if segmentsOverlap(a[k:], b[1:]) {
				return true
			}
		}
		return false
	}

	// Neither side is `**`; both consume exactly one segment.
	if !singleSegmentOverlap(a[0], b[0]) {
		return false
	}
	return segmentsOverlap(a[1:], b[1:])
}

// singleSegmentOverlap reports whether two single-segment patterns can match
// the same segment name. Both patterns must NOT contain `**` (caller's job).
//
// Within a segment, `*` matches any run of zero or more characters (it never
// crosses `/`, but there are no `/` here anyway). All other characters —
// including `?`, `[`, `]` — are treated as literals, matching only themselves.
func singleSegmentOverlap(a, b string) bool {
	// Fast path: both literal.
	if !containsStar(a) && !containsStar(b) {
		return a == b
	}
	return globsCanIntersect(a, b)
}

// globsCanIntersect reports whether some concrete string is matched by BOTH
// single-segment glob patterns `a` and `b`, where `*` matches any run of zero
// or more characters and every other byte is a literal.
//
// This is an intersection-emptiness test, NOT a "does a match b" test. The
// old code only compared the leading and trailing literal anchors and assumed
// anything between two compatible anchors could be reconciled. That produced
// false positives whenever interior literals imposed required characters or a
// minimum length — e.g. "a*a" vs "a" (the second has no room for the trailing
// 'a'), or "a*b*c" vs "axc" (the 'b' is mandatory but absent from "axc").
//
// We instead run a dynamic program over byte positions (i, j) where i indexes
// `a` and j indexes `b`. dp[i][j] is true when the unconsumed suffixes a[i:]
// and b[j:] can be made to match a common remaining string. Transitions:
//
//   - a[i] == '*': the star may consume one byte of the opposing pattern
//     (advance j, star stays) or be skipped, consuming zero (advance i).
//   - b[j] == '*': symmetric — advance i (star stays) or skip it (advance j).
//   - both literals: they must be the equal byte; advance both.
//
// The accept state is dp[len(a)][len(b)] == true (both fully consumed). We
// build the table bottom-up from that anchor so each cell only depends on
// cells with a larger i or j, which are already filled.
//
// Both inputs are ASCII-or-UTF-8 byte strings; matching byte-wise is correct
// here because identical literals compare equal byte-for-byte and a literal
// never needs to align against a partial multi-byte rune of a star run.
func globsCanIntersect(a, b string) bool {
	la, lb := len(a), len(b)

	// dp[i][j] == both suffixes a[i:] and b[j:] can match a common string.
	dp := make([][]bool, la+1)
	for i := range dp {
		dp[i] = make([]bool, lb+1)
	}

	// Anchor: both patterns fully consumed.
	dp[la][lb] = true

	// A trailing run of `*` in either pattern can still match the empty
	// remainder of the other, so propagate the accept state back along the
	// last row/column through stars.
	for i := la - 1; i >= 0; i-- {
		if a[i] == '*' {
			dp[i][lb] = dp[i+1][lb]
		}
	}
	for j := lb - 1; j >= 0; j-- {
		if b[j] == '*' {
			dp[la][j] = dp[la][j+1]
		}
	}

	for i := la - 1; i >= 0; i-- {
		for j := lb - 1; j >= 0; j-- {
			switch {
			case a[i] == '*':
				// Skip the star (consume zero of b) or let it eat one byte of b.
				dp[i][j] = dp[i+1][j] || dp[i][j+1]
			case b[j] == '*':
				// Symmetric: skip it, or let it eat one byte of a.
				dp[i][j] = dp[i][j+1] || dp[i+1][j]
			case a[i] == b[j]:
				// Matching literals: consume one byte from each.
				dp[i][j] = dp[i+1][j+1]
			default:
				dp[i][j] = false
			}
		}
	}
	return dp[0][0]
}

func containsStar(s string) bool { return strings.Contains(s, "*") }

func allDoubleStar(segs []string) bool {
	for _, s := range segs {
		if s != "**" {
			return false
		}
	}
	return true
}
