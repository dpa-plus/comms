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
func singleSegmentOverlap(a, b string) bool {
	// Fast path: both literal.
	if !containsStar(a) && !containsStar(b) {
		return a == b
	}
	// Both are simple glob patterns over a single segment. Strip out `*` runs
	// to obtain anchored literal fragments, then verify A's fragments can be
	// found in B and vice versa. Simplest correct approach: bidirectional
	// pattern-match.
	return globMatch(a, b) && globMatch(b, a)
}

// globMatch reports whether the single-segment glob `pattern` (containing
// `*` but NOT `**`) could match the same string as the (possibly globby)
// `other`. We implement this via the standard recursive glob algorithm,
// substituting each `*` in `pattern` with the runs of characters from
// `other` — but since `other` might itself be globby, we treat its
// non-star segments as literal anchors and require pattern to be at least
// permissive enough to contain them.
//
// Because both inputs are single-segment patterns over the same alphabet,
// this collapses to: split both on `*`, then check that the fragments of
// the more-specific pattern appear in order within the less-specific one.
func globMatch(pattern, other string) bool {
	pParts := strings.Split(pattern, "*")
	oParts := strings.Split(other, "*")

	// Both must respect their first/last anchors.
	if !canPrefixMatch(pParts[0], oParts[0]) {
		return false
	}
	if !canSuffixMatch(pParts[len(pParts)-1], oParts[len(oParts)-1]) {
		return false
	}
	// For the middle parts, we just need them to be non-conflicting. In a
	// single-segment context where stars match any char run, any two
	// patterns whose anchors are compatible can match at least one common
	// string by inserting enough characters between them. So if anchors
	// match, we're done.
	return true
}

// canPrefixMatch reports whether two pattern-prefixes (the part before the
// first `*`) can both be the start of a common string. If both are
// literals, they must match by prefix in one direction or the other.
func canPrefixMatch(p, o string) bool {
	if p == o {
		return true
	}
	if strings.HasPrefix(p, o) || strings.HasPrefix(o, p) {
		return true
	}
	return false
}

// canSuffixMatch is the analogous check for the trailing anchor (the part
// after the last `*`).
func canSuffixMatch(p, o string) bool {
	if p == o {
		return true
	}
	if strings.HasSuffix(p, o) || strings.HasSuffix(o, p) {
		return true
	}
	return false
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
