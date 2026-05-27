package policy

import (
	"os"
	"path/filepath"
	"testing"
)

func writePolicy(t *testing.T, body string) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "policy.txt")
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatalf("WriteFile: %v", err)
	}
	return path
}

func TestLoadMissingFile(t *testing.T) {
	p, err := Load(filepath.Join(t.TempDir(), "missing.txt"))
	if err != nil {
		t.Fatalf("missing file should not error: %v", err)
	}
	if len(p.Paths) != 0 {
		t.Fatalf("expected empty, got %d entries", len(p.Paths))
	}
}

func TestLoadHappyPath(t *testing.T) {
	path := writePolicy(t, `# comment
prisma/schema.prisma
src/lib/aggregate.ts

# another comment
.github/workflows/deploy.yml
`)
	p, err := Load(path)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if len(p.Paths) != 3 {
		t.Fatalf("expected 3 entries, got %d: %v", len(p.Paths), p.Paths)
	}
}

func TestLoadRejectsEscapingPaths(t *testing.T) {
	cases := []string{
		"/etc/passwd\n",
		"../escape\n",
		"src/../../../etc/passwd\n",
	}
	for _, body := range cases {
		t.Run(body, func(t *testing.T) {
			path := writePolicy(t, body)
			_, err := Load(path)
			if err == nil {
				t.Fatalf("expected rejection of %q", body)
			}
		})
	}
}

func TestRequiresAnchor(t *testing.T) {
	path := writePolicy(t, `prisma/schema.prisma
src/lib/*.ts
`)
	p, err := Load(path)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}

	cases := []struct {
		path string
		want bool
	}{
		{"prisma/schema.prisma", true},
		{"prisma/other.prisma", false},
		{"src/lib/aggregate.ts", true},
		{"src/lib/nested/deep.ts", false}, // * doesn't cross segments
		{"frontend/src/main.tsx", false},
	}
	for _, c := range cases {
		got := p.RequiresAnchor(c.path)
		if got != c.want {
			t.Errorf("RequiresAnchor(%q) = %v, want %v", c.path, got, c.want)
		}
	}
}

func TestNilPolicyRequiresAnchorAlwaysFalse(t *testing.T) {
	var p *Policy
	if p.RequiresAnchor("anything") {
		t.Errorf("nil policy should never require anchor")
	}
}
