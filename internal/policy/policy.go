// Package policy loads `.comms/policy.txt` — the list of risky files where a
// whole-file claim is too coarse.
//
// Format:
//
//	# Comments start with #.
//	# Empty lines are ignored.
//	prisma/schema.prisma
//	src/lib/aggregate.ts
//
// Paths are repo-relative POSIX. They are validated against the same
// normalizer as scopes, so `../../etc/passwd` is rejected at load time.
package policy

import (
	"bufio"
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/dpa-plus/comms/internal/overlap"
)

// Policy is the loaded set of risky paths.
type Policy struct {
	// Paths is the set of risky-file globs. We store them as the raw
	// (normalized) strings; overlap-check uses the same algorithm as claim
	// conflict-detection.
	Paths []string
}

// Load reads policy.txt from the given path. A missing file is fine — it
// returns an empty Policy. Malformed entries return a wrapping error so the
// CLI can map to exit 2.
func Load(path string) (*Policy, error) {
	f, err := os.Open(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return &Policy{}, nil
		}
		return nil, fmt.Errorf("policy: open %s: %w", path, err)
	}
	defer func() { _ = f.Close() }()

	var (
		out     Policy
		lineNum int
	)
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		lineNum++
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		norm, err := overlap.NormalizePath(line)
		if err != nil {
			return nil, fmt.Errorf("policy: %s line %d: %w", path, lineNum, err)
		}
		out.Paths = append(out.Paths, norm)
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("policy: scan %s: %w", path, err)
	}
	return &out, nil
}

// RequiresAnchor reports whether the given scope path matches any of the
// risky entries in the policy. If so, claims must supply an anchor (the
// caller enforces that downstream).
func (p *Policy) RequiresAnchor(scopePath string) bool {
	if p == nil {
		return false
	}
	for _, risky := range p.Paths {
		if overlap.PathsOverlap(risky, scopePath) {
			return true
		}
	}
	return false
}
