package tests

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func runGit(t *testing.T, repo string, args ...string) {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = repo
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("git %s: %v: %s", strings.Join(args, " "), err, out)
	}
}

func stagedRecoveryCommand(t *testing.T, output []byte) string {
	t.Helper()
	for _, line := range strings.Split(string(output), "\n") {
		if strings.HasPrefix(line, "  git ") || strings.HasPrefix(line, "  (") {
			return strings.TrimPrefix(line, "  ")
		}
	}
	t.Fatalf("staged check output has no recovery command:\n%s", output)
	return ""
}

func TestCheckStagedBlocksPeerClaimedPaths(t *testing.T) {
	bin := buildCommsBinary(t)
	repo := setupTestRepo(t)
	home := t.TempDir()

	peerPath := filepath.Join(repo, "src", "peer.txt")
	minePath := filepath.Join(repo, "src", "mine.txt")
	if err := os.MkdirAll(filepath.Dir(peerPath), 0o755); err != nil {
		t.Fatalf("mkdir src: %v", err)
	}
	if err := os.WriteFile(peerPath, []byte("peer work\n"), 0o644); err != nil {
		t.Fatalf("write peer file: %v", err)
	}
	if err := os.WriteFile(minePath, []byte("my work\n"), 0o644); err != nil {
		t.Fatalf("write own file: %v", err)
	}
	runGit(t, repo, "add", "--", "src/peer.txt", "src/mine.txt")

	claim := exec.Command(bin, "claim", "src/peer.txt", "--intent", "peer implementation")
	claim.Dir = repo
	claim.Env = childEnv(home, "peer-agent")
	if out, err := claim.CombinedOutput(); err != nil {
		t.Fatalf("peer claim: %v: %s", err, out)
	}

	check := exec.Command(bin, "check", "--staged")
	check.Dir = repo
	check.Env = childEnv(home, "current-agent")
	out, err := check.CombinedOutput()
	if err == nil {
		t.Fatalf("staged check should block a peer-claimed path; output:\n%s", out)
	}
	if exit, ok := err.(*exec.ExitError); !ok || exit.ExitCode() != 1 {
		t.Fatalf("staged check exit = %v, want exit 1; output:\n%s", err, out)
	}
	text := string(out)
	if !strings.Contains(text, "src/peer.txt") || !strings.Contains(text, "@peer-agent") {
		t.Fatalf("staged conflict must identify the peer path and holder; got:\n%s", text)
	}
	if strings.Contains(text, "src/mine.txt") {
		t.Fatalf("unclaimed staged paths must not be reported as conflicts; got:\n%s", text)
	}
	if !strings.Contains(text, "git restore --staged") {
		t.Fatalf("staged conflict must give a safe unstage command; got:\n%s", text)
	}
}

func TestCheckStagedAllowsUnclaimedAndOwnPaths(t *testing.T) {
	bin := buildCommsBinary(t)
	repo := setupTestRepo(t)
	home := t.TempDir()

	for _, path := range []string{"src/mine.txt", "src/unclaimed.txt"} {
		absolute := filepath.Join(repo, path)
		if err := os.MkdirAll(filepath.Dir(absolute), 0o755); err != nil {
			t.Fatalf("mkdir %s: %v", path, err)
		}
		if err := os.WriteFile(absolute, []byte(path+"\n"), 0o644); err != nil {
			t.Fatalf("write %s: %v", path, err)
		}
	}
	runGit(t, repo, "add", "--", "src/mine.txt", "src/unclaimed.txt")

	claim := exec.Command(bin, "claim", "src/mine.txt", "--intent", "my implementation")
	claim.Dir = repo
	claim.Env = childEnv(home, "current-agent")
	if out, err := claim.CombinedOutput(); err != nil {
		t.Fatalf("own claim: %v: %s", err, out)
	}

	check := exec.Command(bin, "check", "--staged")
	check.Dir = repo
	check.Env = childEnv(home, "current-agent")
	if out, err := check.CombinedOutput(); err != nil {
		t.Fatalf("own and unclaimed staged paths should pass: %v: %s", err, out)
	}
}

func TestCheckStagedShellQuotesRecoveryPaths(t *testing.T) {
	bin := buildCommsBinary(t)
	repo := setupTestRepo(t)
	home := t.TempDir()
	path := "src/$(touch should-not-run).txt"
	absolute := filepath.Join(repo, path)
	if err := os.MkdirAll(filepath.Dir(absolute), 0o755); err != nil {
		t.Fatalf("mkdir src: %v", err)
	}
	if err := os.WriteFile(absolute, []byte("peer work\n"), 0o644); err != nil {
		t.Fatalf("write peer file: %v", err)
	}
	runGit(t, repo, "add", "--", path)

	claim := exec.Command(bin, "claim", path, "--intent", "peer implementation")
	claim.Dir = repo
	claim.Env = childEnv(home, "peer-agent")
	if out, err := claim.CombinedOutput(); err != nil {
		t.Fatalf("peer claim: %v: %s", err, out)
	}

	check := exec.Command(bin, "check", "--staged")
	check.Dir = repo
	check.Env = childEnv(home, "current-agent")
	out, err := check.CombinedOutput()
	if err == nil {
		t.Fatalf("staged check should block the peer path; output:\n%s", out)
	}
	want := "git restore --staged -- ':(literal)src/$(touch should-not-run).txt'"
	if !strings.Contains(string(out), want) {
		t.Fatalf("recovery command must shell-quote the path without expansion; want %q in:\n%s", want, out)
	}
}

func TestCheckStagedTreatsHashAsLiteralFilenameCharacter(t *testing.T) {
	bin := buildCommsBinary(t)
	repo := setupTestRepo(t)
	home := t.TempDir()
	path := "src/schema#draft.txt"
	absolute := filepath.Join(repo, path)
	if err := os.MkdirAll(filepath.Dir(absolute), 0o755); err != nil {
		t.Fatalf("mkdir src: %v", err)
	}
	if err := os.WriteFile(absolute, []byte("peer work\n"), 0o644); err != nil {
		t.Fatalf("write peer file: %v", err)
	}
	runGit(t, repo, "add", "--", path)

	claim := exec.Command(bin, "claim", `src/schema\#draft.txt`, "--intent", "peer implementation")
	claim.Dir = repo
	claim.Env = childEnv(home, "peer-agent")
	if out, err := claim.CombinedOutput(); err != nil {
		t.Fatalf("peer claim: %v: %s", err, out)
	}

	check := exec.Command(bin, "check", "--staged")
	check.Dir = repo
	check.Env = childEnv(home, "current-agent")
	out, err := check.CombinedOutput()
	if err == nil {
		t.Fatalf("staged check should block a literal-hash filename claimed by a peer; output:\n%s", out)
	}
	if !strings.Contains(string(out), path) {
		t.Fatalf("staged conflict must report the literal filename %q; got:\n%s", path, out)
	}
}

func TestCheckStagedRecoveryUsesLiteralGitPathspec(t *testing.T) {
	bin := buildCommsBinary(t)
	repo := setupTestRepo(t)
	home := t.TempDir()
	claimedPath := "src/foo[1].txt"
	otherPath := "src/foo1.txt"
	for _, path := range []string{claimedPath, otherPath} {
		absolute := filepath.Join(repo, path)
		if err := os.MkdirAll(filepath.Dir(absolute), 0o755); err != nil {
			t.Fatalf("mkdir src: %v", err)
		}
		if err := os.WriteFile(absolute, []byte(path+"\n"), 0o644); err != nil {
			t.Fatalf("write %s: %v", path, err)
		}
	}
	runGit(t, repo, "add", "--", claimedPath, otherPath)

	claim := exec.Command(bin, "claim", claimedPath, "--intent", "peer implementation")
	claim.Dir = repo
	claim.Env = childEnv(home, "peer-agent")
	if out, err := claim.CombinedOutput(); err != nil {
		t.Fatalf("peer claim: %v: %s", err, out)
	}

	check := exec.Command(bin, "check", "--staged")
	check.Dir = repo
	check.Env = childEnv(home, "current-agent")
	out, err := check.CombinedOutput()
	if err == nil {
		t.Fatalf("staged check should block the peer path; output:\n%s", out)
	}
	want := "git restore --staged -- ':(literal)src/foo[1].txt'"
	if !strings.Contains(string(out), want) {
		t.Fatalf("recovery command must use a literal Git pathspec; want %q in:\n%s", want, out)
	}
}

func TestCheckStagedRecoveryWorksOutsideRepository(t *testing.T) {
	bin := buildCommsBinary(t)
	repo := setupTestRepo(t)
	home := t.TempDir()
	outside := t.TempDir()
	path := "src/peer.txt"
	absolute := filepath.Join(repo, path)
	if err := os.MkdirAll(filepath.Dir(absolute), 0o755); err != nil {
		t.Fatalf("mkdir src: %v", err)
	}
	if err := os.WriteFile(absolute, []byte("peer work\n"), 0o644); err != nil {
		t.Fatalf("write peer file: %v", err)
	}
	runGit(t, repo, "add", "--", path)

	claim := exec.Command(bin, "--repo", repo, "claim", path, "--intent", "peer implementation")
	claim.Dir = outside
	claim.Env = childEnv(home, "peer-agent")
	if out, err := claim.CombinedOutput(); err != nil {
		t.Fatalf("peer claim from outside repository: %v: %s", err, out)
	}

	check := exec.Command(bin, "--repo", repo, "check", "--staged")
	check.Dir = outside
	check.Env = childEnv(home, "current-agent")
	out, err := check.CombinedOutput()
	if err == nil {
		t.Fatalf("staged check should block the peer path; output:\n%s", out)
	}

	recovery := exec.Command("/bin/sh", "-c", stagedRecoveryCommand(t, out))
	recovery.Dir = outside
	if recoveryOut, recoveryErr := recovery.CombinedOutput(); recoveryErr != nil {
		t.Fatalf("printed recovery command must work outside the repository: %v: %s\ncheck output:\n%s", recoveryErr, recoveryOut, out)
	}
	if _, statErr := os.Stat(absolute); statErr != nil {
		t.Fatalf("recovery must preserve the working-tree file: %v", statErr)
	}
	staged := exec.Command("git", "diff", "--cached", "--name-only", "--", path)
	staged.Dir = repo
	if stagedOut, stagedErr := staged.CombinedOutput(); stagedErr != nil {
		t.Fatalf("inspect staged path: %v: %s", stagedErr, stagedOut)
	} else if len(stagedOut) != 0 {
		t.Fatalf("recovery must remove %q from the index; got %q", path, stagedOut)
	}
}

func TestCheckStagedRecoverySanitizesRepositoryRootWithNewline(t *testing.T) {
	bin := buildCommsBinary(t)
	parent := t.TempDir()
	repo := filepath.Join(parent, "repo\nname")
	if err := os.Mkdir(repo, 0o755); err != nil {
		t.Fatalf("mkdir newline repository: %v", err)
	}
	for _, args := range [][]string{
		{"init"},
		{"config", "user.email", "test@example.com"},
		{"config", "user.name", "Test"},
		{"commit", "--allow-empty", "-m", "init"},
	} {
		runGit(t, repo, args...)
	}
	home := t.TempDir()
	outside := t.TempDir()
	path := "peer.txt"
	absolute := filepath.Join(repo, path)
	if err := os.WriteFile(absolute, []byte("peer work\n"), 0o644); err != nil {
		t.Fatalf("write peer file: %v", err)
	}
	runGit(t, repo, "add", "--", path)

	claim := exec.Command(bin, "--repo", repo, "claim", path, "--intent", "peer implementation")
	claim.Dir = outside
	claim.Env = childEnv(home, "peer-agent")
	if out, err := claim.CombinedOutput(); err != nil {
		t.Fatalf("peer claim from outside newline repository: %v: %s", err, out)
	}

	check := exec.Command(bin, "--repo", repo, "check", "--staged")
	check.Dir = outside
	check.Env = childEnv(home, "current-agent")
	out, err := check.CombinedOutput()
	if err == nil {
		t.Fatalf("staged check should block the peer path; output:\n%s", out)
	}
	if strings.Contains(string(out), repo) {
		t.Fatalf("recovery output must not contain raw controls from repository root; got:\n%s", out)
	}

	recovery := exec.Command("/bin/sh", "-c", stagedRecoveryCommand(t, out))
	recovery.Dir = outside
	if recoveryOut, recoveryErr := recovery.CombinedOutput(); recoveryErr != nil {
		t.Fatalf("printed recovery must preserve exact repository root bytes: %v: %s\ncheck output:\n%s", recoveryErr, recoveryOut, out)
	}
	if _, statErr := os.Stat(absolute); statErr != nil {
		t.Fatalf("recovery must preserve the working-tree file: %v", statErr)
	}
	staged := exec.Command("git", "diff", "--cached", "--name-only", "--", path)
	staged.Dir = repo
	if stagedOut, stagedErr := staged.CombinedOutput(); stagedErr != nil {
		t.Fatalf("inspect staged path: %v: %s", stagedErr, stagedOut)
	} else if len(stagedOut) != 0 {
		t.Fatalf("recovery must remove %q from the index; got %q", path, stagedOut)
	}
}

func TestCheckStagedAllowsOwnGenericActorWhenExplicitlyEnabled(t *testing.T) {
	bin := buildCommsBinary(t)
	repo := setupTestRepo(t)
	home := t.TempDir()
	path := "src/mine.txt"
	absolute := filepath.Join(repo, path)
	if err := os.MkdirAll(filepath.Dir(absolute), 0o755); err != nil {
		t.Fatalf("mkdir src: %v", err)
	}
	if err := os.WriteFile(absolute, []byte("my work\n"), 0o644); err != nil {
		t.Fatalf("write own file: %v", err)
	}
	runGit(t, repo, "add", "--", path)

	env := append(childEnv(home, "agent"), "COMMS_ALLOW_GENERIC_ACTOR=1")
	claim := exec.Command(bin, "claim", path, "--intent", "my implementation")
	claim.Dir = repo
	claim.Env = env
	if out, err := claim.CombinedOutput(); err != nil {
		t.Fatalf("own generic claim with override: %v: %s", err, out)
	}

	check := exec.Command(bin, "check", "--staged")
	check.Dir = repo
	check.Env = env
	if out, err := check.CombinedOutput(); err != nil {
		t.Fatalf("explicitly enabled generic actor must recognize its own claim: %v: %s", err, out)
	}
}

func TestCheckStagedInitialRepositoryUsesRmCachedRecovery(t *testing.T) {
	bin := buildCommsBinary(t)
	repo := t.TempDir()
	home := t.TempDir()
	runGit(t, repo, "init")
	path := "src/new[1].txt"
	absolute := filepath.Join(repo, path)
	if err := os.MkdirAll(filepath.Dir(absolute), 0o755); err != nil {
		t.Fatalf("mkdir src: %v", err)
	}
	if err := os.WriteFile(absolute, []byte("first commit work\n"), 0o644); err != nil {
		t.Fatalf("write new file: %v", err)
	}
	runGit(t, repo, "add", "--", path)
	workingContent := []byte("changed after staging\n")
	if err := os.WriteFile(absolute, workingContent, 0o644); err != nil {
		t.Fatalf("change new file after staging: %v", err)
	}

	claim := exec.Command(bin, "claim", path, "--intent", "peer first commit work")
	claim.Dir = repo
	claim.Env = childEnv(home, "peer-agent")
	if out, err := claim.CombinedOutput(); err != nil {
		t.Fatalf("peer claim: %v: %s", err, out)
	}

	check := exec.Command(bin, "check", "--staged")
	check.Dir = repo
	check.Env = childEnv(home, "current-agent")
	out, err := check.CombinedOutput()
	if err == nil {
		t.Fatalf("staged check should block the peer path; output:\n%s", out)
	}
	want := "git rm --cached -f -- ':(literal)src/new[1].txt'"
	if !strings.Contains(string(out), want) {
		t.Fatalf("initial repository must receive HEAD-free recovery; want %q in:\n%s", want, out)
	}
	if strings.Contains(string(out), "git restore --staged") {
		t.Fatalf("initial repository must not receive restore command that requires HEAD; got:\n%s", out)
	}

	recovery := exec.Command("/bin/sh", "-c", stagedRecoveryCommand(t, out))
	if recoveryOut, recoveryErr := recovery.CombinedOutput(); recoveryErr != nil {
		t.Fatalf("initial-repository recovery must unstage content changed after git add: %v: %s\ncheck output:\n%s", recoveryErr, recoveryOut, out)
	}
	if got, readErr := os.ReadFile(absolute); readErr != nil {
		t.Fatalf("read working-tree file after recovery: %v", readErr)
	} else if string(got) != string(workingContent) {
		t.Fatalf("recovery changed working-tree content: got %q, want %q", got, workingContent)
	}
	staged := exec.Command("git", "diff", "--cached", "--name-only", "--", path)
	staged.Dir = repo
	if stagedOut, stagedErr := staged.CombinedOutput(); stagedErr != nil {
		t.Fatalf("inspect staged path: %v: %s", stagedErr, stagedOut)
	} else if len(stagedOut) != 0 {
		t.Fatalf("recovery must remove %q from the initial index; got %q", path, stagedOut)
	}
}

func TestCheckStagedInitialRepositoryRecoversChangedNewlineFilename(t *testing.T) {
	bin := buildCommsBinary(t)
	repo := t.TempDir()
	home := t.TempDir()
	runGit(t, repo, "init")
	path := "line\nbreak.txt"
	absolute := filepath.Join(repo, path)
	if err := os.WriteFile(absolute, []byte("staged content\n"), 0o644); err != nil {
		t.Fatalf("write newline filename: %v", err)
	}
	runGit(t, repo, "add", "--", path)
	workingContent := []byte("changed after staging\n")
	if err := os.WriteFile(absolute, workingContent, 0o644); err != nil {
		t.Fatalf("change newline filename after staging: %v", err)
	}

	claim := exec.Command(bin, "claim", "*.txt", "--intent", "peer first commit text files")
	claim.Dir = repo
	claim.Env = childEnv(home, "peer-agent")
	if out, err := claim.CombinedOutput(); err != nil {
		t.Fatalf("peer glob claim: %v: %s", err, out)
	}

	check := exec.Command(bin, "check", "--staged")
	check.Dir = repo
	check.Env = childEnv(home, "current-agent")
	out, err := check.CombinedOutput()
	if err == nil {
		t.Fatalf("peer glob claim must block newline filename; output:\n%s", out)
	}
	if !strings.Contains(string(out), "git rm --cached -f --pathspec-from-file=- --pathspec-file-nul") {
		t.Fatalf("byte-safe initial recovery must force index-only removal; got:\n%s", out)
	}

	shell := "/bin/sh"
	if zsh, lookupErr := exec.LookPath("zsh"); lookupErr == nil {
		shell = zsh
	}
	recovery := exec.Command(shell, "-c", stagedRecoveryCommand(t, out))
	if recoveryOut, recoveryErr := recovery.CombinedOutput(); recoveryErr != nil {
		t.Fatalf("byte-safe initial recovery must unstage content changed after git add in %s: %v: %s\ncheck output:\n%s", shell, recoveryErr, recoveryOut, out)
	}
	if got, readErr := os.ReadFile(absolute); readErr != nil {
		t.Fatalf("read working-tree file after recovery: %v", readErr)
	} else if string(got) != string(workingContent) {
		t.Fatalf("recovery changed working-tree content: got %q, want %q", got, workingContent)
	}
	staged := exec.Command("git", "diff", "--cached", "--name-only", "-z", "--", path)
	staged.Dir = repo
	if stagedOut, stagedErr := staged.CombinedOutput(); stagedErr != nil {
		t.Fatalf("inspect staged newline path: %v: %s", stagedErr, stagedOut)
	} else if len(stagedOut) != 0 {
		t.Fatalf("recovery must remove newline filename from the initial index; got %q", stagedOut)
	}
}

func TestCheckStagedTreatsStarInFilenameAsLiteral(t *testing.T) {
	bin := buildCommsBinary(t)
	repo := setupTestRepo(t)
	home := t.TempDir()
	stagedPath := "src/literal*.txt"
	absolute := filepath.Join(repo, stagedPath)
	if err := os.MkdirAll(filepath.Dir(absolute), 0o755); err != nil {
		t.Fatalf("mkdir src: %v", err)
	}
	if err := os.WriteFile(absolute, []byte("current work\n"), 0o644); err != nil {
		t.Fatalf("write staged file: %v", err)
	}
	runGit(t, repo, "add", "--", stagedPath)

	claim := exec.Command(bin, "claim", "src/literal.txt", "--intent", "peer implementation")
	claim.Dir = repo
	claim.Env = childEnv(home, "peer-agent")
	if out, err := claim.CombinedOutput(); err != nil {
		t.Fatalf("peer claim: %v: %s", err, out)
	}

	check := exec.Command(bin, "check", "--staged")
	check.Dir = repo
	check.Env = childEnv(home, "current-agent")
	if out, err := check.CombinedOutput(); err != nil {
		t.Fatalf("literal-star filename must not overlap distinct literal claim: %v: %s", err, out)
	}
}

func TestCheckStagedAllowsUnclaimedFilenameWithNewline(t *testing.T) {
	bin := buildCommsBinary(t)
	repo := setupTestRepo(t)
	home := t.TempDir()
	path := "line\nbreak.txt"
	absolute := filepath.Join(repo, path)
	if err := os.WriteFile(absolute, []byte("current work\n"), 0o644); err != nil {
		t.Fatalf("write newline filename: %v", err)
	}
	runGit(t, repo, "add", "--", path)

	check := exec.Command(bin, "check", "--staged")
	check.Dir = repo
	check.Env = childEnv(home, "current-agent")
	if out, err := check.CombinedOutput(); err != nil {
		t.Fatalf("unclaimed newline filename must pass staged check: %v: %s", err, out)
	}
}

func TestCheckStagedRecoversClaimedFilenameWithNewline(t *testing.T) {
	bin := buildCommsBinary(t)
	repo := setupTestRepo(t)
	home := t.TempDir()
	path := "line\nbreak.txt"
	absolute := filepath.Join(repo, path)
	if err := os.WriteFile(absolute, []byte("peer work\n"), 0o644); err != nil {
		t.Fatalf("write newline filename: %v", err)
	}
	runGit(t, repo, "add", "--", path)

	claim := exec.Command(bin, "claim", "*.txt", "--intent", "peer text files")
	claim.Dir = repo
	claim.Env = childEnv(home, "peer-agent")
	if out, err := claim.CombinedOutput(); err != nil {
		t.Fatalf("peer glob claim: %v: %s", err, out)
	}

	check := exec.Command(bin, "check", "--staged")
	check.Dir = repo
	check.Env = childEnv(home, "current-agent")
	out, err := check.CombinedOutput()
	if err == nil {
		t.Fatalf("peer glob claim must block newline filename; output:\n%s", out)
	}
	if !strings.Contains(string(out), "line?break.txt") {
		t.Fatalf("diagnostic must visibly sanitize the newline filename; got:\n%s", out)
	}

	shell := "/bin/sh"
	if zsh, lookupErr := exec.LookPath("zsh"); lookupErr == nil {
		shell = zsh
	}
	recovery := exec.Command(shell, "-c", stagedRecoveryCommand(t, out))
	if recoveryOut, recoveryErr := recovery.CombinedOutput(); recoveryErr != nil {
		t.Fatalf("printed recovery must preserve exact newline filename bytes in %s: %v: %s\ncheck output:\n%s", shell, recoveryErr, recoveryOut, out)
	}
	if _, statErr := os.Stat(absolute); statErr != nil {
		t.Fatalf("recovery must preserve the working-tree file: %v", statErr)
	}
	staged := exec.Command("git", "diff", "--cached", "--name-only", "-z", "--", path)
	staged.Dir = repo
	if stagedOut, stagedErr := staged.CombinedOutput(); stagedErr != nil {
		t.Fatalf("inspect staged newline path: %v: %s", stagedErr, stagedOut)
	} else if len(stagedOut) != 0 {
		t.Fatalf("recovery must remove newline filename from index; got %q", stagedOut)
	}
}
