package subcmd

import (
	"bytes"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/dpa-plus/comms/internal/paths"
)

func TestLessonListAndPrintUseGlobalDirOutsideRepo(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	dir, err := paths.GlobalLessonsDir()
	if err != nil {
		t.Fatalf("global lessons dir: %v", err)
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatalf("mkdir lessons: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "verify-data-first.md"), []byte("# verify-data-first\n\nCheck API data before UI.\n"), 0o600); err != nil {
		t.Fatalf("write lesson: %v", err)
	}

	list := captureStdout(t, func() {
		if err := runLessonList(); err != nil {
			t.Fatalf("runLessonList: %v", err)
		}
	})
	if !strings.Contains(list, "verify-data-first") || !strings.Contains(list, "Check API data before UI.") {
		t.Fatalf("lesson list missing slug/hint: %q", list)
	}

	printed := captureStdout(t, func() {
		if err := runLessonPrint("verify-data-first"); err != nil {
			t.Fatalf("runLessonPrint: %v", err)
		}
	})
	if !strings.Contains(printed, "Check API data before UI.") {
		t.Fatalf("lesson print output = %q", printed)
	}
}

func TestLessonEditCreatesGlobalStub(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "human-eli")
	t.Setenv("USER", "eli")
	t.Setenv("EDITOR", "true")

	if err := runLessonEdit("claim-smallest-scope"); err != nil {
		t.Fatalf("runLessonEdit: %v", err)
	}
	dir, err := paths.GlobalLessonsDir()
	if err != nil {
		t.Fatalf("global lessons dir: %v", err)
	}
	raw, err := os.ReadFile(filepath.Join(dir, "claim-smallest-scope.md"))
	if err != nil {
		t.Fatalf("read lesson stub: %v", err)
	}
	text := string(raw)
	if !strings.Contains(text, "# claim-smallest-scope") || !strings.Contains(text, "Effective pattern:") {
		t.Fatalf("bad stub: %q", text)
	}
}

func captureStdout(t *testing.T, fn func()) string {
	t.Helper()
	old := os.Stdout
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatalf("pipe: %v", err)
	}
	os.Stdout = w
	fn()
	_ = w.Close()
	os.Stdout = old
	var buf bytes.Buffer
	if _, err := io.Copy(&buf, r); err != nil {
		t.Fatalf("copy stdout: %v", err)
	}
	return buf.String()
}
