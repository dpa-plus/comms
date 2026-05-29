package subcmd

import (
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/dpa-plus/comms/internal/event"
	"github.com/dpa-plus/comms/internal/lock"
	"github.com/spf13/cobra"
)

// slugRE pins the slug grammar from M11: `[a-z0-9][a-z0-9._-]{0,80}`.
var slugRE = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,80}$`)

// NewDocCmd builds `comms doc` with three forms (per Patch #2):
//
//	comms doc --list           # list slugs
//	comms doc <slug>           # print
//	comms doc <slug> --edit    # open $EDITOR under sidecar flock
func NewDocCmd() *cobra.Command {
	var (
		list bool
		edit bool
	)
	cmd := &cobra.Command{
		Use:   "doc [--list | <slug> [--edit]]",
		Short: "Read or edit the .comms/docs wiki",
		Long: `Three forms:

  comms doc --list           List all slugs in .comms/docs/
  comms doc <slug>           Print .comms/docs/<slug>.md to stdout
  comms doc <slug> --edit    Open $EDITOR on the doc under a sidecar flock

The --edit form auto-creates the doc if absent, takes a sidecar flock
(<docs>/.<slug>.lock) so two editors can't clobber each other, and
records a "decision"-category finding describing the update.`,
		Args: cobra.MaximumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runDoc(args, list, edit)
		},
	}
	cmd.Flags().BoolVar(&list, "list", false, "list slugs in .comms/docs/")
	cmd.Flags().BoolVar(&edit, "edit", false, "open the doc in $EDITOR (with sidecar flock)")
	return cmd
}

func runDoc(args []string, list, edit bool) error {
	if list {
		if len(args) > 0 || edit {
			Fatalf(2, "doc: --list takes no other arguments")
		}
		return runDocList()
	}
	if len(args) != 1 {
		Fatalf(2, "doc: provide --list or a slug (optionally with --edit)")
	}
	slug := args[0]
	if !slugRE.MatchString(slug) {
		Fatalf(2, "doc: invalid slug %q. Must match [a-z0-9][a-z0-9._-]{0,80}.", slug)
	}
	if edit {
		return runDocEdit(slug)
	}
	return runDocPrint(slug)
}

func runDocList() error {
	rt, err := Open(OpenOpts{Mutating: false})
	if err != nil {
		return err
	}
	defer rt.Close()

	entries, err := os.ReadDir(rt.Paths.Docs)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		Fatalf(2, "doc: read %s: %v", rt.Paths.Docs, err)
	}
	var slugs []string
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || strings.HasPrefix(name, ".") || !strings.HasSuffix(name, ".md") {
			continue
		}
		slugs = append(slugs, strings.TrimSuffix(name, ".md"))
	}
	sort.Strings(slugs)
	if len(slugs) == 0 {
		fmt.Println("(no docs yet)")
		return nil
	}
	for _, s := range slugs {
		// Show first non-empty, non-`#` line as a hint.
		hint := firstSummaryLine(filepath.Join(rt.Paths.Docs, s+".md"))
		if hint != "" {
			fmt.Printf("%-30s %s\n", s, hint)
		} else {
			fmt.Println(s)
		}
	}
	return nil
}

func runDocPrint(slug string) error {
	rt, err := Open(OpenOpts{Mutating: false})
	if err != nil {
		return err
	}
	defer rt.Close()

	path := rt.Paths.DocFilePath(slug)
	f, err := os.Open(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			Fatalf(1, "doc: slug %q not found in %s", slug, rt.Paths.Docs)
		}
		Fatalf(2, "doc: open %s: %v", path, err)
	}
	defer f.Close()
	if _, err := io.Copy(os.Stdout, f); err != nil {
		Fatalf(2, "doc: print %s: %v", path, err)
	}
	return nil
}

func runDocEdit(slug string) error {
	// --edit is a mutating op (writes a finding event), so we need a real
	// actor + the regular flock.
	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		return err
	}
	// We release the main flock as soon as we've created the file + acquired
	// the sidecar so other comms commands can proceed while the editor is open.
	defer rt.Close()

	docPath := rt.Paths.DocFilePath(slug)
	cmd, err := newEditorCommand(docPath)
	if err != nil {
		Fatalf(2, "doc: %v", err)
	}
	if err := ensureDocStub(docPath, slug); err != nil {
		Fatalf(2, "doc: %v", err)
	}

	// Sidecar lock — try non-blocking so we can report the existing holder.
	sidecarPath := rt.Paths.DocLockPath(slug)
	sidecar, err := lock.TryAcquire(sidecarPath)
	if err != nil {
		if errors.Is(err, lock.ErrLocked) {
			holder := readSidecarHolder(sidecarPath)
			Fatalf(1, "doc: %s is being edited by %s, retry later", slug, holder)
		}
		Fatalf(2, "doc: sidecar lock %s: %v", sidecarPath, err)
	}
	// Stamp the sidecar with our actor + timestamp so the next would-be
	// editor sees who holds it.
	stampSidecar(sidecarPath, rt.Actor)
	defer sidecar.Close()

	// Release the main flock BEFORE invoking the editor — opening $EDITOR is
	// arbitrary-duration user interaction; we don't want to hold the per-repo
	// flock that whole time.
	_ = rt.Close()

	// Run the editor.
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		// User cancelled or editor crashed; don't write a finding event.
		Fatalf(2, "doc: editor exited with error: %v", err)
	}

	// After the editor exits, reopen runtime + record the finding.
	rt2, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		return err
	}
	defer rt2.Close()
	now := time.Now().UTC()
	ev := event.Event{
		TS:    now,
		ID:    event.NewID(now),
		Actor: rt2.Actor,
		Type:  event.TypeFinding,
		Data: map[string]interface{}{
			"category": "decision",
			"summary":  fmt.Sprintf("updated doc:%s", slug),
			"refs":     []map[string]string{{"kind": "doc", "value": slug}},
		},
	}
	if err := rt2.Append(ev); err != nil {
		return err
	}
	fmt.Printf("Saved .comms/docs/%s.md\n", slug)
	return nil
}

func ensureDocStub(path, slug string) error {
	if _, err := os.Stat(path); err == nil {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("mkdir docs: %w", err)
	}
	stub := fmt.Sprintf("# %s\n\n", slug)
	return os.WriteFile(path, []byte(stub), 0o644)
}

func stampSidecar(path, actor string) {
	body := fmt.Sprintf("%s\n%s\n", actor, time.Now().UTC().Format(time.RFC3339))
	_ = os.WriteFile(path, []byte(body), 0o600)
}

func readSidecarHolder(path string) string {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "another editor"
	}
	lines := strings.SplitN(string(raw), "\n", 3)
	if len(lines) >= 2 {
		return fmt.Sprintf("@%s since %s", lines[0], lines[1])
	}
	return "another editor"
}

// newEditorCommand builds an editor command from $VISUAL, $EDITOR, then `vi`.
// EDITOR values commonly include arguments, for example "code --wait".
func newEditorCommand(path string) (*exec.Cmd, error) {
	spec, err := resolveEditorSpec()
	if err != nil {
		return nil, err
	}
	parts := strings.Fields(spec)
	if len(parts) == 0 {
		return nil, fmt.Errorf("empty editor command")
	}
	exe, err := exec.LookPath(parts[0])
	if err != nil {
		return nil, fmt.Errorf("editor %q not found", parts[0])
	}
	args := append(append([]string{}, parts[1:]...), path)
	return exec.Command(exe, args...), nil
}

func resolveEditorSpec() (string, error) {
	for _, env := range []string{"VISUAL", "EDITOR"} {
		if v := strings.TrimSpace(os.Getenv(env)); v != "" {
			return v, nil
		}
	}
	if _, err := exec.LookPath("vi"); err == nil {
		return "vi", nil
	}
	return "", fmt.Errorf("no editor found. Set $EDITOR (e.g., 'export EDITOR=nano') and retry.")
}

func firstSummaryLine(path string) string {
	raw, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if len(line) > 70 {
			return line[:67] + "..."
		}
		return line
	}
	// No body? Use the heading.
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "# ") {
			return strings.TrimSpace(line[2:])
		}
	}
	return ""
}
