// Package overlap parses scope strings and decides whether two scopes
// conflict for the purposes of an exclusive claim.
//
// Grammar:
//
//	scope  := path ('#' anchor)?
//	path   := POSIX path, optionally globbed with * or **
//	anchor := L<n>-<m>          (line range, inclusive, n ≤ m, both ≥ 1)
//	        | <symbol-name>      (opaque identifier)
//
// The `#` is escaped as `\#` when it appears in a filename. We never
// expand globs against the real filesystem — overlap is computed
// purely as a string operation on the patterns themselves.
package overlap

import (
	"fmt"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"
)

// lineRangeRe matches the exact line-range anchor shape `L<n>-<m>` and nothing
// else. Anchors that merely start with 'L' and contain '-' (e.g. "List-impl",
// "Loader-2", "L-value", "Linked-list") must NOT be treated as ranges — they
// are legitimate symbol names and have to fall through to the SYMBOL branch.
var lineRangeRe = regexp.MustCompile(`^L([0-9]+)-([0-9]+)$`)

// Scope is the parsed form of a `path[#anchor]` string.
type Scope struct {
	// Raw is the original user-supplied scope string (preserved so we can
	// echo it back in conflict messages verbatim).
	Raw string

	// Path is the normalized, repo-relative POSIX path or glob pattern.
	Path string

	// Anchor describes the slice of the file the claim covers.
	// Kind == AnchorWhole means a whole-file claim.
	Anchor Anchor
}

// AnchorKind enumerates the three legal anchor shapes (plus whole-file).
type AnchorKind int

const (
	AnchorWhole  AnchorKind = iota // no `#` anchor — claims the entire file
	AnchorLine                     // L<n>-<m>
	AnchorSymbol                   // symbol-name (opaque string)
)

// Anchor is the per-file claim refinement. Exactly one of LineStart/LineEnd
// (when Kind == AnchorLine) or Symbol (when Kind == AnchorSymbol) is set.
type Anchor struct {
	Kind      AnchorKind
	LineStart int
	LineEnd   int
	Symbol    string
}

// Parse interprets a raw scope string into a Scope. Returns a user-facing
// error on grammar violations.
//
// Path normalization (matches the plan's "Path normalization" rules):
//   - Reject absolute paths.
//   - Reject paths that normalize outside repo root (`..` escapes).
//   - filepath.Clean + backslashes-to-slashes + strip `./`.
func Parse(raw string) (Scope, error) {
	if raw == "" {
		return Scope{}, fmt.Errorf("scope: empty")
	}
	pathPart, anchorPart, hasAnchor := splitOnUnescapedHash(raw)
	pathPart = unescapeHash(pathPart)

	normPath, err := normalizePath(pathPart)
	if err != nil {
		return Scope{}, err
	}
	s := Scope{Raw: raw, Path: normPath}
	if !hasAnchor {
		s.Anchor = Anchor{Kind: AnchorWhole}
		return s, nil
	}
	anchor, err := parseAnchor(anchorPart)
	if err != nil {
		return Scope{}, err
	}
	s.Anchor = anchor
	return s, nil
}

// splitOnUnescapedHash returns (left, right, true) if raw contains an
// unescaped `#`, splitting on the first such occurrence. Backslash-escaped
// `\#` is treated as a literal '#' in the path.
func splitOnUnescapedHash(raw string) (left, right string, hasAnchor bool) {
	var b strings.Builder
	i := 0
	for i < len(raw) {
		if raw[i] == '\\' && i+1 < len(raw) && raw[i+1] == '#' {
			b.WriteByte('\\')
			b.WriteByte('#')
			i += 2
			continue
		}
		if raw[i] == '#' {
			return b.String(), raw[i+1:], true
		}
		b.WriteByte(raw[i])
		i++
	}
	return b.String(), "", false
}

// unescapeHash converts `\#` to `#` in path parts (post-split).
func unescapeHash(s string) string {
	return strings.ReplaceAll(s, `\#`, "#")
}

// NormalizePath is exported so the policy loader can apply the same rules.
func NormalizePath(raw string) (string, error) {
	return normalizePath(raw)
}

// isControlRune reports whether r is a C0 control character (< 0x20), DEL
// (0x7F), or a C1 control character (U+0080–U+009F). These code points can
// carry terminal-escape sequences — notably C1 CSI (U+009B), which many
// terminals interpret exactly like the two-byte ESC[ introducer — so we
// refuse to persist any scope path or anchor that contains them.
//
// We deliberately check by rune rather than by raw byte so that normal
// printable Unicode — whose multi-byte UTF-8 encodings contain continuation
// bytes in the 0x80–0xBF range — is never rejected: legitimate code points
// (Café, файл, 日本語) decode to runes ≥ 0x100 and pass cleanly. A C1 control,
// by contrast, is a single code point in 0x80–0x9F (e.g. valid-UTF-8 U+009B is
// bytes 0xC2 0x9B) and is caught here regardless of how it was encoded.
func isControlRune(r rune) bool {
	return r < 0x20 || r == 0x7f || (r >= 0x80 && r <= 0x9f)
}

func normalizePath(raw string) (string, error) {
	if raw == "" {
		return "", fmt.Errorf("scope: empty path")
	}
	// Reject control characters (C0, C1, and DEL) before any other processing so
	// a scope carrying terminal-escape bytes can never be normalized/persisted.
	for _, r := range raw {
		if isControlRune(r) {
			return "", fmt.Errorf("scope: path contains control character")
		}
	}
	// Reject absolute paths.
	if strings.HasPrefix(raw, "/") {
		return "", fmt.Errorf("scope: absolute paths not allowed: %q", raw)
	}
	// Convert backslashes (in case of Windows-typed input).
	cleaned := filepath.ToSlash(raw)
	cleaned = filepath.Clean(cleaned)
	cleaned = filepath.ToSlash(cleaned) // Clean may re-introduce backslashes on Windows
	// Strip leading "./" if present (filepath.Clean already does this, but be defensive).
	cleaned = strings.TrimPrefix(cleaned, "./")
	if cleaned == "." || cleaned == "" {
		return "", fmt.Errorf("scope: empty path after normalization")
	}
	// Reject any remaining `..` segments — they escape the repo root.
	segments := strings.Split(cleaned, "/")
	for _, seg := range segments {
		if seg == ".." {
			return "", fmt.Errorf("scope: path escapes repo root: %q", raw)
		}
	}
	return cleaned, nil
}

func parseAnchor(s string) (Anchor, error) {
	if s == "" {
		return Anchor{}, fmt.Errorf("scope: anchor after `#` is empty")
	}
	// Line range form: strictly `L<n>-<m>` with both halves all digits.
	// Anything else (including symbol names that merely start with 'L' and
	// contain '-', like "List-impl" or "L-value") falls through to the SYMBOL
	// branch below rather than erroring. Once a string DOES match the range
	// shape we still validate the numbers, so genuinely malformed ranges such
	// as L0-10 (zero) or L10-5 (inverted) remain hard errors.
	if m := lineRangeRe.FindStringSubmatch(s); m != nil {
		// Groups are guaranteed to be non-empty digit runs by the regexp, so
		// Atoi only fails on overflow; treat that as a bad range.
		start, err1 := strconv.Atoi(m[1])
		end, err2 := strconv.Atoi(m[2])
		if err1 != nil || err2 != nil {
			return Anchor{}, fmt.Errorf("scope: bad line range %q (want L<n>-<m>)", s)
		}
		if start < 1 {
			return Anchor{}, fmt.Errorf("scope: line numbers must be ≥ 1, got L%d-%d", start, end)
		}
		if start > end {
			return Anchor{}, fmt.Errorf("scope: inverted line range L%d-%d", start, end)
		}
		return Anchor{Kind: AnchorLine, LineStart: start, LineEnd: end}, nil
	}
	// Symbol form.
	sym := strings.TrimSpace(s)
	if sym == "" {
		return Anchor{}, fmt.Errorf("scope: symbol is empty after trimming whitespace")
	}
	for _, r := range sym {
		// Reject control bytes (C0/C1/DEL) and newlines — same hardening as the
		// path side, so anchors can't smuggle terminal-escape sequences either.
		if isControlRune(r) {
			return Anchor{}, fmt.Errorf("scope: symbol contains control character")
		}
	}
	if !utf8.ValidString(sym) {
		return Anchor{}, fmt.Errorf("scope: symbol is not valid UTF-8")
	}
	return Anchor{Kind: AnchorSymbol, Symbol: sym}, nil
}

// String renders a Scope back to its canonical (post-normalization) form.
func (s Scope) String() string {
	path := escapeHash(s.Path)
	switch s.Anchor.Kind {
	case AnchorWhole:
		return path
	case AnchorLine:
		return fmt.Sprintf("%s#L%d-%d", path, s.Anchor.LineStart, s.Anchor.LineEnd)
	case AnchorSymbol:
		return path + "#" + s.Anchor.Symbol
	}
	return path
}

func escapeHash(s string) string {
	return strings.ReplaceAll(s, "#", `\#`)
}
