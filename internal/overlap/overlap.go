package overlap

// Scopes reports whether two parsed scopes claim overlapping territory.
//
// Rule (per the plan's "Overlap detection" spec):
//  1. Path glob intersection: false → return false.
//  2. Anchors:
//     - Both line ranges → numeric range intersection.
//     - Both symbols     → case-sensitive string equality.
//     - Mixed (line + symbol) → pessimistic overlap.
//     - Any anchor + whole-file → conflict.
//     - Whole-file + whole-file → conflict.
//
// Step 2 only runs if step 1 says paths could overlap. Anchor refinement
// can NEVER expand an overlap — it can only shrink one.
func Scopes(a, b Scope) bool {
	if !PathsOverlap(a.Path, b.Path) {
		return false
	}
	return anchorsOverlap(a.Anchor, b.Anchor)
}

func anchorsOverlap(a, b Anchor) bool {
	// Whole-file vs anything = overlap.
	if a.Kind == AnchorWhole || b.Kind == AnchorWhole {
		return true
	}
	// Mixed (line + symbol) is pessimistically treated as overlap, because we
	// have no LSP knowledge to confirm they refer to different code.
	if a.Kind != b.Kind {
		return true
	}
	switch a.Kind {
	case AnchorLine:
		return rangesIntersect(a.LineStart, a.LineEnd, b.LineStart, b.LineEnd)
	case AnchorSymbol:
		return a.Symbol == b.Symbol
	}
	return false
}

func rangesIntersect(aStart, aEnd, bStart, bEnd int) bool {
	// Closed intervals [aStart,aEnd] and [bStart,bEnd] intersect iff
	// aStart ≤ bEnd && bStart ≤ aEnd.
	return aStart <= bEnd && bStart <= aEnd
}
