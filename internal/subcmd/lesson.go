package subcmd

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/dpa-plus/comms/internal/actor"
	"github.com/dpa-plus/comms/internal/lock"
	"github.com/dpa-plus/comms/internal/paths"
	"github.com/spf13/cobra"
)

// NewLessonCmd builds `comms lesson` for curated cross-project agent lessons.
//
//	comms lesson --list           # list global lessons
//	comms lesson <slug>           # print
//	comms lesson <slug> --edit    # open $EDITOR under sidecar flock
func NewLessonCmd() *cobra.Command {
	var (
		list bool
		edit bool
	)
	cmd := &cobra.Command{
		Use:   "lesson [--list | <slug> [--edit]]",
		Short: "Read or edit curated global lessons",
		Long: `Three forms:

  comms lesson --list           List global lesson slugs
  comms lesson <slug>           Print one global lesson to stdout
  comms lesson <slug> --edit    Open $EDITOR on the lesson under a sidecar flock

Lessons are cross-project operating knowledge for agents. They are stored in
the user's global comms data dir, not in a project repo. Add them rarely:
only when the user explicitly asks or approves a leader's proposed lesson.`,
		Args: cobra.MaximumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runLesson(args, list, edit)
		},
	}
	cmd.Flags().BoolVar(&list, "list", false, "list global lessons")
	cmd.Flags().BoolVar(&edit, "edit", false, "open the lesson in $EDITOR (with sidecar flock)")
	return cmd
}

func runLesson(args []string, list, edit bool) error {
	if list {
		if len(args) > 0 || edit {
			Fatalf(2, "lesson: --list takes no other arguments")
		}
		return runLessonList()
	}
	if len(args) != 1 {
		Fatalf(2, "lesson: provide --list or a slug (optionally with --edit)")
	}
	slug := args[0]
	if !slugRE.MatchString(slug) {
		Fatalf(2, "lesson: invalid slug %q. Must match [a-z0-9][a-z0-9._-]{0,80}.", slug)
	}
	if edit {
		return runLessonEdit(slug)
	}
	return runLessonPrint(slug)
}

func runLessonList() error {
	dir, err := paths.GlobalLessonsDir()
	if err != nil {
		return err
	}
	entries, err := os.ReadDir(dir)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		Fatalf(2, "lesson: read %s: %v", dir, err)
	}
	slugs := markdownSlugs(entries)
	if len(slugs) == 0 {
		fmt.Println("(no global lessons yet)")
		return nil
	}
	for _, s := range slugs {
		hint := firstSummaryLine(filepath.Join(dir, s+".md"))
		if hint != "" {
			fmt.Printf("%-30s %s\n", s, hint)
		} else {
			fmt.Println(s)
		}
	}
	return nil
}

func runLessonPrint(slug string) error {
	dir, err := paths.GlobalLessonsDir()
	if err != nil {
		return err
	}
	path := filepath.Join(dir, slug+".md")
	f, err := os.Open(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			Fatalf(1, "lesson: slug %q not found in %s", slug, dir)
		}
		Fatalf(2, "lesson: open %s: %v", path, err)
	}
	defer func() { _ = f.Close() }()
	if _, err := io.Copy(os.Stdout, f); err != nil {
		Fatalf(2, "lesson: print %s: %v", path, err)
	}
	return nil
}

func runLessonEdit(slug string) error {
	a, err := actor.Resolve(actor.Mutating)
	if err != nil {
		return err
	}
	dir, err := paths.GlobalLessonsDir()
	if err != nil {
		return err
	}
	lessonPath := filepath.Join(dir, slug+".md")
	cmd, err := newEditorCommand(lessonPath)
	if err != nil {
		Fatalf(2, "lesson: %v", err)
	}
	if err := ensureLessonStub(lessonPath, slug); err != nil {
		Fatalf(2, "lesson: %v", err)
	}

	sidecarPath := filepath.Join(dir, "."+slug+".lock")
	sidecar, err := lock.TryAcquire(sidecarPath)
	if err != nil {
		if errors.Is(err, lock.ErrLocked) {
			holder := readSidecarHolder(sidecarPath)
			Fatalf(1, "lesson: %s is being edited by %s, retry later", slug, holder)
		}
		Fatalf(2, "lesson: sidecar lock %s: %v", sidecarPath, err)
	}
	stampSidecar(sidecarPath, a)
	defer func() { _ = sidecar.Close() }()

	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		Fatalf(2, "lesson: editor exited with error: %v", err)
	}
	fmt.Printf("Saved global lesson %s\n", lessonPath)
	return nil
}

func ensureLessonStub(path, slug string) error {
	if _, err := os.Stat(path); err == nil {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return fmt.Errorf("mkdir lessons: %w", err)
	}
	stub := fmt.Sprintf(`# %s

Use when:

Effective pattern:

Avoid:

Evidence:
- Added %s after user approval.
`, slug, time.Now().UTC().Format("2006-01-02"))
	return os.WriteFile(path, []byte(stub), 0o600)
}

func markdownSlugs(entries []os.DirEntry) []string {
	var slugs []string
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || strings.HasPrefix(name, ".") || !strings.HasSuffix(name, ".md") {
			continue
		}
		slugs = append(slugs, strings.TrimSuffix(name, ".md"))
	}
	sort.Strings(slugs)
	return slugs
}
