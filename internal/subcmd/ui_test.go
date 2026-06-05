package subcmd

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/dpa-plus/comms/internal/event"
	"github.com/dpa-plus/comms/internal/lock"
	"github.com/dpa-plus/comms/internal/paths"
	"github.com/dpa-plus/comms/internal/state"
)

func TestBuildDemoUISnapshotMarksStaleAndShowsCommsArchive(t *testing.T) {
	snap := buildDemoUISnapshot(90 * time.Minute)

	if !snap.Project.Demo {
		t.Fatalf("demo snapshot should be marked demo")
	}
	if snap.Project.MutationsEnabled {
		t.Fatalf("demo snapshot must not enable mutations")
	}
	if got := actionByIDForTest(snap.Actions, "start_comms_session"); got.Enabled {
		t.Fatalf("demo start action should be disabled: %+v", got)
	}
	if got := actionByIDForTest(snap.Actions, "select_session_log"); !got.Enabled {
		t.Fatalf("session log action should be advertised: %+v", got)
	}
	if len(snap.Claims) != 3 {
		t.Fatalf("demo claims len = %d, want 3", len(snap.Claims))
	}
	if len(snap.Lessons) != 3 {
		t.Fatalf("demo lessons len = %d, want 3", len(snap.Lessons))
	}
	if !snap.Sessions[0].Leader {
		t.Fatalf("first demo session should be leader")
	}
	if len(snap.CommsSessions) != 1 {
		t.Fatalf("demo archived comms sessions len = %d, want 1", len(snap.CommsSessions))
	}
	if snap.Current == nil || len(snap.Current.Events) == 0 {
		t.Fatalf("demo current session should include its own event log: %+v", snap.Current)
	}
	if snap.CommsSessions[0].EventCount == 0 || len(snap.CommsSessions[0].Actors) == 0 {
		t.Fatalf("demo archive should include analysis summary: %+v", snap.CommsSessions[0])
	}
	if len(snap.CommsSessions[0].Events) == 0 {
		t.Fatalf("demo archive should include its own event log")
	}
	if len(snap.Notes) == 0 || !snap.Notes[0].Priority {
		t.Fatalf("first demo note should be priority")
	}
	if len(snap.Findings) == 0 || !snap.Findings[0].Priority {
		t.Fatalf("first demo finding should be priority")
	}
	if !snap.Claims[2].Stale {
		t.Fatalf("old demo claim should be marked stale")
	}
	if snap.Claims[0].Stale {
		t.Fatalf("fresh demo claim should not be marked stale")
	}
}

func TestBuildUISnapshotUsesEmptySlicesForMissingArchiveAndLessons(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "human-eli")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	if err := rt.Append(event.Event{
		TS:    time.Now().UTC(),
		ID:    "01JX2Q3Y7W5B6N9P0R1S2T3N0A",
		Actor: "claude-dev",
		Type:  event.TypeHello,
		Data:  map[string]interface{}{"base_name": "claude"},
	}); err != nil {
		t.Fatalf("append hello: %v", err)
	}
	snap := buildUISnapshot(rt, 90*time.Minute)
	if err := rt.Close(); err != nil {
		t.Fatalf("close runtime: %v", err)
	}

	body, err := json.Marshal(snap)
	if err != nil {
		t.Fatalf("marshal snapshot: %v", err)
	}
	if strings.Contains(string(body), `"comms_sessions":null`) {
		t.Fatalf("comms_sessions should encode as [], got %s", body)
	}
	if strings.Contains(string(body), `"lessons":null`) {
		t.Fatalf("lessons should encode as [], got %s", body)
	}
}

func TestBuildGlobalUISnapshotAttachesLegacyClaimsToProjectCurrentSession(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)

	hash := "abc123legacy"
	repoRoot := filepath.Join(home, "example-project")
	if err := os.MkdirAll(repoRoot, 0o755); err != nil {
		t.Fatalf("mkdir repo root: %v", err)
	}
	dataHome, err := paths.UserDataHome()
	if err != nil {
		t.Fatalf("user data home: %v", err)
	}
	logDir := filepath.Join(dataHome, "comms", hash)
	if err := os.MkdirAll(logDir, 0o700); err != nil {
		t.Fatalf("mkdir log dir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(logDir, "repo-path.txt"), []byte(repoRoot+"\n"), 0o600); err != nil {
		t.Fatalf("write repo path: %v", err)
	}
	now := time.Now().UTC()
	for _, ev := range []event.Event{
		{TS: now.Add(-10 * time.Minute), ID: event.NewID(now.Add(-10 * time.Minute)), Actor: "claude-dev", Type: event.TypeHello, Data: map[string]interface{}{"base_name": "claude"}},
		{TS: now.Add(-9 * time.Minute), ID: event.NewID(now.Add(-9 * time.Minute)), Actor: "claude-dev", Type: event.TypeClaim, Scope: []string{"src/current.ts"}, Data: map[string]interface{}{"intent": "legacy current work"}},
	} {
		if err := event.Append(filepath.Join(logDir, "log.jsonl"), ev); err != nil {
			t.Fatalf("append event: %v", err)
		}
	}

	snap, err := buildGlobalUISnapshot(90 * time.Minute)
	if err != nil {
		t.Fatalf("build global snapshot: %v", err)
	}
	if len(snap.Active) != 1 || snap.Active[0].ID != hash+":current" {
		t.Fatalf("expected one prefixed legacy current session: %+v", snap.Active)
	}
	if len(snap.Active[0].Claims) != 1 || snap.Active[0].Claims[0].Scope != "src/current.ts" {
		t.Fatalf("legacy claim should attach to prefixed current session: active=%+v claims=%+v", snap.Active, snap.Claims)
	}
	if snap.Claims[0].RepoHash != hash {
		t.Fatalf("global claim should expose owning repo hash, got %+v", snap.Claims[0])
	}
}

func TestBuildGlobalUISnapshotOrdersPriorityFindingsFirst(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	dataHome, err := paths.UserDataHome()
	if err != nil {
		t.Fatalf("user data home: %v", err)
	}
	now := time.Now().UTC()
	mk := func(hash, name string, ts time.Time, priority bool, summary string) {
		repoRoot := filepath.Join(home, name)
		if err := os.MkdirAll(repoRoot, 0o755); err != nil {
			t.Fatalf("mkdir repo: %v", err)
		}
		logDir := filepath.Join(dataHome, "comms", hash)
		if err := os.MkdirAll(logDir, 0o700); err != nil {
			t.Fatalf("mkdir log dir: %v", err)
		}
		if err := os.WriteFile(filepath.Join(logDir, "repo-path.txt"), []byte(repoRoot+"\n"), 0o600); err != nil {
			t.Fatalf("write repo path: %v", err)
		}
		data := map[string]interface{}{"category": "decision", "summary": summary}
		if priority {
			data["priority"] = true
		}
		ev := event.Event{TS: ts, ID: event.NewID(ts), Actor: "claude-dev", Type: event.TypeFinding, Data: data}
		if err := event.Append(filepath.Join(logDir, "log.jsonl"), ev); err != nil {
			t.Fatalf("append finding: %v", err)
		}
	}
	// Project A: a NEWER, non-priority finding. Project B: an OLDER, priority one.
	mk("aaaa11112222", "proj-a", now.Add(-2*time.Minute), false, "newer normal finding")
	mk("bbbb33334444", "proj-b", now.Add(-30*time.Minute), true, "older PRIORITY finding")

	snap, err := buildGlobalUISnapshot(90 * time.Minute)
	if err != nil {
		t.Fatalf("build global snapshot: %v", err)
	}
	if len(snap.Findings) != 2 {
		t.Fatalf("want 2 merged findings, got %d", len(snap.Findings))
	}
	// The priority finding must sort first in the merged all-projects view even
	// though it is older — matching the per-repo view's priority-first ordering.
	if !snap.Findings[0].Priority {
		t.Fatalf("priority finding must sort first in the merged view, got: %+v", snap.Findings)
	}
}

func TestBuildGlobalUISnapshotPopulatesPerProjectContainers(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	dataHome, err := paths.UserDataHome()
	if err != nil {
		t.Fatalf("user data home: %v", err)
	}
	now := time.Now().UTC()
	projects := []struct{ hash, name, scope string }{
		{"hashalpha0001", "alpha", "src/a.ts"},
		{"hashbeta00002", "beta", "src/b.ts"},
	}
	for _, p := range projects {
		repoRoot := filepath.Join(home, p.name)
		if err := os.MkdirAll(repoRoot, 0o755); err != nil {
			t.Fatalf("mkdir repo: %v", err)
		}
		logDir := filepath.Join(dataHome, "comms", p.hash)
		if err := os.MkdirAll(logDir, 0o700); err != nil {
			t.Fatalf("mkdir log dir: %v", err)
		}
		if err := os.WriteFile(filepath.Join(logDir, "repo-path.txt"), []byte(repoRoot+"\n"), 0o600); err != nil {
			t.Fatalf("write repo path: %v", err)
		}
		for _, ev := range []event.Event{
			{TS: now.Add(-10 * time.Minute), ID: event.NewID(now.Add(-10 * time.Minute)), Actor: "claude-dev", Type: event.TypeHello, Data: map[string]interface{}{"base_name": "claude"}},
			{TS: now.Add(-9 * time.Minute), ID: event.NewID(now.Add(-9 * time.Minute)), Actor: "claude-dev", Type: event.TypeClaim, Scope: []string{p.scope}, Data: map[string]interface{}{"intent": "work on " + p.name}},
			{TS: now.Add(-8 * time.Minute), ID: event.NewID(now.Add(-8 * time.Minute)), Actor: "claude-dev", Type: event.TypeFinding, Data: map[string]interface{}{"category": "fix", "summary": p.name + " finding"}},
		} {
			if err := event.Append(filepath.Join(logDir, "log.jsonl"), ev); err != nil {
				t.Fatalf("append event: %v", err)
			}
		}
	}

	snap, err := buildGlobalUISnapshot(90 * time.Minute)
	if err != nil {
		t.Fatalf("build global snapshot: %v", err)
	}
	if len(snap.ProjectSessions) != 2 {
		t.Fatalf("expected 2 project_sessions, got %d", len(snap.ProjectSessions))
	}
	byHash := map[string]uiProjectSession{}
	for _, ps := range snap.ProjectSessions {
		byHash[ps.RepoHash] = ps
	}
	for _, p := range projects {
		ps, ok := byHash[p.hash]
		if !ok {
			t.Fatalf("missing project_session for %s", p.hash)
		}
		if ps.RepoName != p.name {
			t.Fatalf("project %s name = %q, want %q", p.hash, ps.RepoName, p.name)
		}
		// Container data must be UN-prefixed and self-contained.
		if len(ps.Claims) != 1 || ps.Claims[0].Scope != p.scope {
			t.Fatalf("project %s container claims = %+v", p.hash, ps.Claims)
		}
		if ps.Claims[0].RepoHash != p.hash {
			t.Fatalf("project %s container claim should carry its repo hash, got %q", p.hash, ps.Claims[0].RepoHash)
		}
		if strings.HasPrefix(ps.Claims[0].SessionName, p.name+" /") {
			t.Fatalf("container claim session_name must be UN-prefixed, got %q", ps.Claims[0].SessionName)
		}
		if len(ps.Findings) != 1 || ps.Findings[0].Summary != p.name+" finding" {
			t.Fatalf("container finding must be un-prefixed, got %+v", ps.Findings)
		}
		if ps.Current == nil || len(ps.Current.Claims) != 1 {
			t.Fatalf("project %s current session should hold its claim, got %+v", p.hash, ps.Current)
		}
	}
	// The merged flat arrays still aggregate across projects, project-prefixed.
	if len(snap.Claims) != 2 {
		t.Fatalf("merged claims = %d, want 2 (one per project)", len(snap.Claims))
	}
	if len(snap.Findings) != 2 {
		t.Fatalf("merged findings = %d, want 2", len(snap.Findings))
	}
	for _, f := range snap.Findings {
		if !strings.Contains(f.Summary, ": ") {
			t.Fatalf("merged finding should be project-prefixed, got %q", f.Summary)
		}
	}
}

func TestBuildGlobalUISnapshotDedupesDuplicateRepoRoots(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)

	repoRoot := filepath.Join(home, "002")
	if err := os.MkdirAll(repoRoot, 0o755); err != nil {
		t.Fatalf("mkdir repo root: %v", err)
	}
	dataHome, err := paths.UserDataHome()
	if err != nil {
		t.Fatalf("user data home: %v", err)
	}
	now := time.Now().UTC()
	for _, tc := range []struct {
		hash   string
		actor  string
		scope  string
		offset time.Duration
	}{
		{hash: "oldhash00001", actor: "old-agent", scope: "src/old.ts", offset: -30 * time.Minute},
		{hash: "newhash00002", actor: "new-agent", scope: "src/new.ts", offset: -5 * time.Minute},
	} {
		logDir := filepath.Join(dataHome, "comms", tc.hash)
		if err := os.MkdirAll(logDir, 0o700); err != nil {
			t.Fatalf("mkdir log dir: %v", err)
		}
		if err := os.WriteFile(filepath.Join(logDir, "repo-path.txt"), []byte(repoRoot+"\n"), 0o600); err != nil {
			t.Fatalf("write repo path: %v", err)
		}
		for _, ev := range []event.Event{
			{TS: now.Add(tc.offset), ID: event.NewID(now.Add(tc.offset)), Actor: tc.actor, Type: event.TypeHello, Data: map[string]interface{}{"base_name": "codex"}},
			{TS: now.Add(tc.offset + time.Minute), ID: event.NewID(now.Add(tc.offset + time.Minute)), Actor: tc.actor, Type: event.TypeClaim, Scope: []string{tc.scope}, Data: map[string]interface{}{"intent": "current work"}},
		} {
			if err := event.Append(filepath.Join(logDir, "log.jsonl"), ev); err != nil {
				t.Fatalf("append event: %v", err)
			}
		}
	}

	snap, err := buildGlobalUISnapshot(90 * time.Minute)
	if err != nil {
		t.Fatalf("build global snapshot: %v", err)
	}
	if len(snap.Active) != 1 || snap.Active[0].ID != "newhash00002:current" {
		t.Fatalf("duplicate repo roots should keep only newest active session: %+v", snap.Active)
	}
	if len(snap.Claims) != 1 || snap.Claims[0].Actor != "new-agent" || snap.Claims[0].Scope != "src/new.ts" {
		t.Fatalf("duplicate repo roots should keep newest log claims: %+v", snap.Claims)
	}
}

func TestUIAllModeCanReleaseClaimByRepoHash(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "human-eli")
	t.Setenv("USER", "eli")

	rt, err := Open(OpenOpts{Mutating: true, RepoRootOverride: repo})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	hash := rt.Repo.Hash
	claimID := "01JX2Q3Y7W5B6N9P0R1S2T4G0"
	if err := rt.Append(event.Event{
		TS:    time.Now().Add(-9 * time.Minute).UTC(),
		ID:    claimID,
		Actor: "claude-dev",
		Type:  event.TypeClaim,
		Scope: []string{"src/config.ts"},
		Data:  map[string]interface{}{"intent": "global release smoke"},
	}); err != nil {
		t.Fatalf("append claim: %v", err)
	}
	if err := rt.Close(); err != nil {
		t.Fatalf("close runtime: %v", err)
	}

	body := `{"claim_id":"` + claimID + `","repo_hash":"` + hash + `","reason":"cleared from global UI"}`
	req := httptest.NewRequest(http.MethodPost, "/api/claim/release", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	uiServer{all: true, staleAfter: 90 * time.Minute}.serveReleaseClaim(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var snap uiSnapshot
	if err := json.Unmarshal(rec.Body.Bytes(), &snap); err != nil {
		t.Fatalf("decode snapshot: %v", err)
	}
	if len(snap.Claims) != 0 {
		t.Fatalf("released claim should be gone from global snapshot: %+v", snap.Claims)
	}

	rt, err = Open(OpenOpts{Mutating: false, RepoRootOverride: repo})
	if err != nil {
		t.Fatalf("reopen runtime: %v", err)
	}
	defer rt.Close()
	if got := rt.State.ClaimByID(claimID); got != nil {
		t.Fatalf("claim still active after global UI release: %+v", got)
	}
}

func TestBuildGlobalUISnapshotSkipsOrphanedRepoPath(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)

	hash := "abc123orphan"
	dataHome, err := paths.UserDataHome()
	if err != nil {
		t.Fatalf("user data home: %v", err)
	}
	logDir := filepath.Join(dataHome, "comms", hash)
	if err := os.MkdirAll(logDir, 0o700); err != nil {
		t.Fatalf("mkdir log dir: %v", err)
	}
	orphanedRepoRoot := filepath.Join(home, "missing", "002")
	if err := os.WriteFile(filepath.Join(logDir, "repo-path.txt"), []byte(orphanedRepoRoot+"\n"), 0o600); err != nil {
		t.Fatalf("write repo path: %v", err)
	}
	now := time.Now().UTC()
	for _, ev := range []event.Event{
		{TS: now.Add(-10 * time.Minute), ID: event.NewID(now.Add(-10 * time.Minute)), Actor: "agent-a", Type: event.TypeHello, Data: map[string]interface{}{"base_name": "codex"}},
		{TS: now.Add(-9 * time.Minute), ID: event.NewID(now.Add(-9 * time.Minute)), Actor: "agent-a", Type: event.TypeClaim, Scope: []string{"src/current.ts"}, Data: map[string]interface{}{"intent": "deleted temp repo work"}},
	} {
		if err := event.Append(filepath.Join(logDir, "log.jsonl"), ev); err != nil {
			t.Fatalf("append event: %v", err)
		}
	}

	snap, err := buildGlobalUISnapshot(90 * time.Minute)
	if err != nil {
		t.Fatalf("build global snapshot: %v", err)
	}
	if len(snap.Active) != 0 || len(snap.Claims) != 0 || len(snap.Sessions) != 0 {
		t.Fatalf("orphaned repo log should be hidden: active=%+v claims=%+v sessions=%+v", snap.Active, snap.Claims, snap.Sessions)
	}
}

func TestBuildGlobalUISnapshotSkipsScratchTempRepoRoots(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)

	repoRoot := filepath.Join(t.TempDir(), "comms-uitest")
	if err := os.MkdirAll(repoRoot, 0o755); err != nil {
		t.Fatalf("mkdir scratch repo root: %v", err)
	}
	hash := "abc123scratch"
	dataHome, err := paths.UserDataHome()
	if err != nil {
		t.Fatalf("user data home: %v", err)
	}
	logDir := filepath.Join(dataHome, "comms", hash)
	if err := os.MkdirAll(logDir, 0o700); err != nil {
		t.Fatalf("mkdir log dir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(logDir, "repo-path.txt"), []byte(repoRoot+"\n"), 0o600); err != nil {
		t.Fatalf("write repo path: %v", err)
	}
	now := time.Now().UTC()
	for _, ev := range []event.Event{
		{TS: now.Add(-10 * time.Minute), ID: event.NewID(now.Add(-10 * time.Minute)), Actor: "agent-a", Type: event.TypeHello, Data: map[string]interface{}{"base_name": "codex"}},
		{TS: now.Add(-9 * time.Minute), ID: event.NewID(now.Add(-9 * time.Minute)), Actor: "agent-a", Type: event.TypeClaim, Scope: []string{"src/current.ts"}, Data: map[string]interface{}{"intent": "generated UI smoke work"}},
	} {
		if err := event.Append(filepath.Join(logDir, "log.jsonl"), ev); err != nil {
			t.Fatalf("append event: %v", err)
		}
	}

	snap, err := buildGlobalUISnapshot(90 * time.Minute)
	if err != nil {
		t.Fatalf("build global snapshot: %v", err)
	}
	if len(snap.Active) != 0 || len(snap.Claims) != 0 || len(snap.Sessions) != 0 {
		t.Fatalf("scratch temp repo log should be hidden: active=%+v claims=%+v sessions=%+v", snap.Active, snap.Claims, snap.Sessions)
	}
}

func TestIsScratchRepoRootRecognizesPrivateTmpRepos(t *testing.T) {
	if _, err := os.Stat("/private/tmp"); err != nil {
		t.Skip("/private/tmp is not available on this platform")
	}
	if !isScratchRepoRoot("/private/tmp/comms-uitest") {
		t.Fatalf("expected /private/tmp generated comms repo to be scratch")
	}
	if !isScratchRepoRoot("/private/tmp/test-comms-repo") {
		t.Fatalf("expected /private/tmp generated test comms repo to be scratch")
	}
	if isScratchRepoRoot("/private/tmp/my-project") {
		t.Fatalf("non-generated repo names under /private/tmp should remain visible")
	}
}

func TestBuildGlobalUISnapshotKeepsTemporarilyUnreadableRepoPath(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)

	lockedParent := filepath.Join(home, "locked")
	repoRoot := filepath.Join(lockedParent, "real-project")
	if err := os.MkdirAll(repoRoot, 0o755); err != nil {
		t.Fatalf("mkdir repo root: %v", err)
	}
	if err := os.Chmod(lockedParent, 0); err != nil {
		t.Fatalf("chmod locked parent: %v", err)
	}
	t.Cleanup(func() {
		_ = os.Chmod(lockedParent, 0o755)
	})

	hash := "abc123locked"
	dataHome, err := paths.UserDataHome()
	if err != nil {
		t.Fatalf("user data home: %v", err)
	}
	logDir := filepath.Join(dataHome, "comms", hash)
	if err := os.MkdirAll(logDir, 0o700); err != nil {
		t.Fatalf("mkdir log dir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(logDir, "repo-path.txt"), []byte(repoRoot+"\n"), 0o600); err != nil {
		t.Fatalf("write repo path: %v", err)
	}
	now := time.Now().UTC()
	for _, ev := range []event.Event{
		{TS: now.Add(-10 * time.Minute), ID: event.NewID(now.Add(-10 * time.Minute)), Actor: "agent-a", Type: event.TypeHello, Data: map[string]interface{}{"base_name": "codex"}},
		{TS: now.Add(-9 * time.Minute), ID: event.NewID(now.Add(-9 * time.Minute)), Actor: "agent-a", Type: event.TypeClaim, Scope: []string{"src/current.ts"}, Data: map[string]interface{}{"intent": "work in protected repo"}},
	} {
		if err := event.Append(filepath.Join(logDir, "log.jsonl"), ev); err != nil {
			t.Fatalf("append event: %v", err)
		}
	}

	snap, err := buildGlobalUISnapshot(90 * time.Minute)
	if err != nil {
		t.Fatalf("build global snapshot: %v", err)
	}
	if len(snap.Active) != 1 || snap.Active[0].ID != hash+":current" {
		t.Fatalf("temporarily unreadable repo path should remain visible: active=%+v", snap.Active)
	}
	if len(snap.Claims) != 1 || snap.Claims[0].Scope != "src/current.ts" {
		t.Fatalf("claim from temporarily unreadable repo should remain visible: %+v", snap.Claims)
	}
}

func TestUIServeEndCommsSessionArchivesAndReleasesAllClaims(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "human-eli")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	claimID := "01JX2Q3Y7W5B6N9P0R1S2T3U4V"
	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	err = rt.Append(event.Event{
		TS:    time.Now().Add(-10 * time.Minute).UTC(),
		ID:    "01JX2Q3Y7W5B6N9P0R1S2T3U4A",
		Actor: "claude-20260527-a",
		Type:  event.TypeHello,
		Data:  map[string]interface{}{"base_name": "claude", "hostname": "host"},
	})
	if err == nil {
		err = rt.Append(event.Event{
			TS:    time.Now().Add(-8 * time.Minute).UTC(),
			ID:    claimID,
			Actor: "claude-20260527-a",
			Type:  event.TypeClaim,
			Scope: []string{"src/session.ts"},
			Data:  map[string]interface{}{"intent": "finish work"},
		})
	}
	if err == nil {
		err = rt.Append(event.Event{
			TS:    time.Now().Add(-7 * time.Minute).UTC(),
			ID:    "01JX2Q3Y7W5B6N9P0R1S2T3U4B",
			Actor: "codex-20260527-a",
			Type:  event.TypeHello,
			Data:  map[string]interface{}{"base_name": "codex", "hostname": "host"},
		})
	}
	if err == nil {
		err = rt.Append(event.Event{
			TS:    time.Now().Add(-6 * time.Minute).UTC(),
			ID:    "01JX2Q3Y7W5B6N9P0R1S2T3U4C",
			Actor: "codex-20260527-a",
			Type:  event.TypeClaim,
			Scope: []string{"src/other.ts"},
			Data:  map[string]interface{}{"intent": "review work"},
		})
	}
	if closeErr := rt.Close(); closeErr != nil {
		t.Fatalf("close runtime: %v", closeErr)
	}
	if err != nil {
		t.Fatalf("append setup events: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/comms-session/end", strings.NewReader(`{"reason":"project done"}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	uiServer{staleAfter: 90 * time.Minute}.serveEndCommsSession(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var snap uiSnapshot
	if err := json.Unmarshal(rec.Body.Bytes(), &snap); err != nil {
		t.Fatalf("decode snapshot: %v", err)
	}
	if len(snap.Claims) != 0 {
		t.Fatalf("claims still active after session end: %+v", snap.Claims)
	}
	if len(snap.Sessions) != 0 {
		t.Fatalf("sessions still active after comms session end: %+v", snap.Sessions)
	}
	if got := actionByIDForTest(snap.Actions, "start_comms_session"); !got.Enabled {
		t.Fatalf("start should be enabled after ending session: %+v", got)
	}
	if got := actionByIDForTest(snap.Actions, "end_comms_session"); got.Enabled {
		t.Fatalf("end should be disabled after ending session: %+v", got)
	}
	if len(snap.CommsSessions) != 1 {
		t.Fatalf("comms session not archived: %+v", snap.CommsSessions)
	}
	if snap.Current != nil {
		t.Fatalf("current session should be closed after end: %+v", snap.Current)
	}
	if snap.CommsSessions[0].ReleasedRefs != 2 {
		t.Fatalf("released refs = %d, want 2", snap.CommsSessions[0].ReleasedRefs)
	}
	if snap.CommsSessions[0].Reason != "project done" || snap.CommsSessions[0].EventCount < 5 {
		t.Fatalf("bad archive summary: %+v", snap.CommsSessions[0])
	}
	if len(snap.CommsSessions[0].Events) == 0 || snap.CommsSessions[0].Events[0].Type != event.TypeRelease {
		t.Fatalf("archive should expose its own newest-first event log: %+v", snap.CommsSessions[0].Events)
	}

	rt, err = Open(OpenOpts{Mutating: false})
	if err != nil {
		t.Fatalf("reopen runtime: %v", err)
	}
	defer rt.Close()
	if got := rt.State.ClaimByID(claimID); got != nil {
		t.Fatalf("claim still active after session end: %+v", got)
	}
	if rt.State.Sessions["claude-20260527-a"] != nil {
		t.Fatalf("session still active in folded state")
	}
	if rt.State.Sessions["codex-20260527-a"] != nil {
		t.Fatalf("second session still active in folded state")
	}
	if len(rt.State.EndedCommsSessions) != 1 || rt.State.EndedCommsSessions[0].EndedBy != "human-eli" || rt.State.EndedCommsSessions[0].Reason != "project done" {
		t.Fatalf("bad ended state: %+v", rt.State.EndedCommsSessions)
	}
}

func TestUIServeEndCurrentSessionIDArchivesLegacyCurrentSession(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "human-eli")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	err = rt.Append(event.Event{
		TS:    time.Now().Add(-8 * time.Minute).UTC(),
		ID:    "01JX2Q3Y7W5B6N9P0R1S2T3U9B",
		Actor: "claude-dev",
		Type:  event.TypeHello,
		Data:  map[string]interface{}{"base_name": "claude", "hostname": "host"},
	})
	if err == nil {
		err = rt.Append(event.Event{
			TS:    time.Now().Add(-7 * time.Minute).UTC(),
			ID:    "01JX2Q3Y7W5B6N9P0R1S2T3U9A",
			Actor: "claude-dev",
			Type:  event.TypeClaim,
			Scope: []string{"src/current.ts"},
			Data:  map[string]interface{}{"intent": "legacy current work"},
		})
	}
	if closeErr := rt.Close(); closeErr != nil {
		t.Fatalf("close runtime: %v", closeErr)
	}
	if err != nil {
		t.Fatalf("append setup events: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/comms-session/end", strings.NewReader(`{"reason":"done","session_id":"current","name":"Current session"}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	uiServer{staleAfter: 90 * time.Minute}.serveEndCommsSession(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var snap uiSnapshot
	if err := json.Unmarshal(rec.Body.Bytes(), &snap); err != nil {
		t.Fatalf("decode snapshot: %v", err)
	}
	if len(snap.Claims) != 0 {
		t.Fatalf("current-session claim should be released: %+v", snap.Claims)
	}
	if len(snap.CommsSessions) != 1 {
		t.Fatalf("legacy current session should be archived: %+v", snap.CommsSessions)
	}
	if snap.CommsSessions[0].Reason != "done" || snap.CommsSessions[0].ReleasedRefs != 1 {
		t.Fatalf("bad legacy current archive summary: %+v", snap.CommsSessions[0])
	}
}

// TestUIServeEndCurrentSessionIDDoesNotEndOperatorNamedSession locks in
// round-2 finding #R2-06: ending the legacy/global "current" window
// (session_id=="current") must drive the no-session global sweep, NOT the
// operator fallback. Previously, when the operator (rt.Actor) also held its own
// NAMED comms session, "end current" wrongly resolved to that named session and
// released only its claims — leaving a different actor's legacy claim active and
// tagging the archive with the operator's named session id.
func TestUIServeEndCurrentSessionIDDoesNotEndOperatorNamedSession(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "human-eli")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	const namedSessionID = "01JX2Q3Y7W5B6N9P0R1S2T3W0S"
	operatorNamedClaim := "01JX2Q3Y7W5B6N9P0R1S2T3W0A"
	legacyClaim := "01JX2Q3Y7W5B6N9P0R1S2T3W0B"

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	// Operator opens its OWN named comms session...
	err = rt.Append(event.Event{
		TS:    time.Now().Add(-9 * time.Minute).UTC(),
		ID:    "01JX2Q3Y7W5B6N9P0R1S2T3W0H",
		Actor: "human-eli",
		Type:  event.TypeHello,
		Data: map[string]interface{}{
			"base_name":           "human",
			"hostname":            "host",
			"comms_session_start": true,
			"comms_session_id":    namedSessionID,
			"comms_session_name":  "feature-x",
		},
	})
	// ...with a claim attributed to that named session.
	if err == nil {
		err = rt.Append(event.Event{
			TS:    time.Now().Add(-8 * time.Minute).UTC(),
			ID:    operatorNamedClaim,
			Actor: "human-eli",
			Type:  event.TypeClaim,
			Scope: []string{"src/feature.ts"},
			Data: map[string]interface{}{
				"intent":             "named session work",
				"comms_session_id":   namedSessionID,
				"comms_session_name": "feature-x",
			},
		})
	}
	// A DIFFERENT actor holds a legacy claim with no comms_session_id.
	if err == nil {
		err = rt.Append(event.Event{
			TS:    time.Now().Add(-7 * time.Minute).UTC(),
			ID:    legacyClaim,
			Actor: "claude-legacy",
			Type:  event.TypeClaim,
			Scope: []string{"src/legacy.ts"},
			Data:  map[string]interface{}{"intent": "legacy current work"},
		})
	}
	if closeErr := rt.Close(); closeErr != nil {
		t.Fatalf("close runtime: %v", closeErr)
	}
	if err != nil {
		t.Fatalf("append setup events: %v", err)
	}

	// Sanity: the operator DOES have a named session that the buggy fallback
	// would otherwise latch onto.
	rt, err = Open(OpenOpts{Mutating: false})
	if err != nil {
		t.Fatalf("reopen for sanity: %v", err)
	}
	if sess := rt.State.Sessions["human-eli"]; sess == nil || sess.SessionID != namedSessionID {
		_ = rt.Close()
		t.Fatalf("setup: operator should hold named session %s, got %+v", namedSessionID, sess)
	}
	_ = rt.Close()

	req := httptest.NewRequest(http.MethodPost, "/api/comms-session/end", strings.NewReader(`{"reason":"end legacy current","session_id":"current","name":"Current session"}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	uiServer{staleAfter: 90 * time.Minute}.serveEndCommsSession(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var snap uiSnapshot
	if err := json.Unmarshal(rec.Body.Bytes(), &snap); err != nil {
		t.Fatalf("decode snapshot: %v", err)
	}
	// Global sweep: every claim must be released, INCLUDING the other actor's
	// legacy claim. The buggy operator-fallback would leave it active.
	if len(snap.Claims) != 0 {
		t.Fatalf("ending current window must release all claims, still active: %+v", snap.Claims)
	}

	rt, err = Open(OpenOpts{Mutating: false})
	if err != nil {
		t.Fatalf("reopen runtime: %v", err)
	}
	defer rt.Close()
	if got := rt.State.ClaimByID(legacyClaim); got != nil {
		t.Fatalf("legacy claim of @claude-legacy should be released by the global sweep: %+v", got)
	}
	if got := rt.State.ClaimByID(operatorNamedClaim); got != nil {
		t.Fatalf("operator's named claim should also be released by the global sweep: %+v", got)
	}

	// The archive's end event must be the GLOBAL one (empty comms_session_id),
	// not the operator's named session. A non-empty id here is the regression.
	var endEvent *event.Event
	for i := range rt.Events {
		ev := rt.Events[i]
		if ev.Type == event.TypeRelease && dataBool(ev.Data, "comms_session_end") {
			endEvent = &rt.Events[i]
		}
	}
	if endEvent == nil {
		t.Fatalf("no comms_session_end event was appended; events=%+v", rt.Events)
	}
	if id := dataString(endEvent.Data, "comms_session_id"); id != "" {
		t.Fatalf("end event scoped to a named session %q; expected global sweep (empty id)", id)
	}
}

func TestUIServeEndCommsSessionRejectsStaleNamedSessionWithoutClaims(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "human-eli")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	if err := rt.Append(event.Event{
		TS:    time.Now().Add(-5 * time.Hour).UTC(),
		ID:    "01JX2Q3Y7W5B6N9P0R1S2T3U9C",
		Actor: "claude-stale",
		Type:  event.TypeHello,
		Data: map[string]interface{}{
			"comms_session_start": true,
			"comms_session_id":    "sess-stale",
			"comms_session_name":  "stale session",
		},
	}); err != nil {
		t.Fatalf("append setup event: %v", err)
	}
	if err := rt.Close(); err != nil {
		t.Fatalf("close runtime: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/comms-session/end", strings.NewReader(`{"reason":"done","session_id":"sess-stale","name":"stale session"}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	uiServer{staleAfter: 90 * time.Minute}.serveEndCommsSession(rec, req)

	if rec.Code != http.StatusConflict {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
}

// TestAcquireLockBounded locks in round-2 finding #R2-05: the UI mutating path
// uses a BOUNDED lock acquire so a request goroutine never blocks forever behind
// a process holding the per-repo flock, while the CLI's unbounded acquire is
// unchanged.
func TestAcquireLockBounded(t *testing.T) {
	path := filepath.Join(t.TempDir(), "bounded.lock")

	held, err := lock.Acquire(path)
	if err != nil {
		t.Fatalf("hold lock: %v", err)
	}

	// Bounded acquire while held must time out promptly with ErrLockTimeout
	// rather than blocking forever.
	start := time.Now()
	h, err := acquireLock(path, 120*time.Millisecond)
	elapsed := time.Since(start)
	if h != nil {
		_ = h.Close()
		t.Fatalf("bounded acquire returned a handle while lock was held")
	}
	if !errors.Is(err, ErrLockTimeout) {
		t.Fatalf("expected ErrLockTimeout, got %v", err)
	}
	if elapsed < 100*time.Millisecond {
		t.Fatalf("bounded acquire returned too early (%v); should wait ~the full timeout", elapsed)
	}
	if elapsed > 2*time.Second {
		t.Fatalf("bounded acquire took too long (%v); should be roughly the timeout", elapsed)
	}

	// After release, the bounded acquire succeeds.
	if err := held.Close(); err != nil {
		t.Fatalf("release held lock: %v", err)
	}
	h2, err := acquireLock(path, 1*time.Second)
	if err != nil || h2 == nil {
		t.Fatalf("bounded acquire after release: handle=%v err=%v", h2, err)
	}
	if err := h2.Close(); err != nil {
		t.Fatalf("close bounded handle: %v", err)
	}

	// timeout==0 keeps the original blocking acquire (CLI behavior).
	h3, err := acquireLock(path, 0)
	if err != nil || h3 == nil {
		t.Fatalf("unbounded acquire: handle=%v err=%v", h3, err)
	}
	_ = h3.Close()
}

func TestUIServeStartCommsSessionCreatesCurrentSession(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "human-eli")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	req := httptest.NewRequest(http.MethodPost, "/api/comms-session/start", strings.NewReader(`{"reason":"new project window"}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	uiServer{staleAfter: 90 * time.Minute}.serveStartCommsSession(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var snap uiSnapshot
	if err := json.Unmarshal(rec.Body.Bytes(), &snap); err != nil {
		t.Fatalf("decode snapshot: %v", err)
	}
	if snap.Current == nil || snap.Current.EventCount != 1 {
		t.Fatalf("current session not started: %+v", snap.Current)
	}
	if len(snap.Sessions) != 1 || snap.Sessions[0].Actor != "human-eli" {
		t.Fatalf("starter actor should be active: %+v", snap.Sessions)
	}
	if len(snap.Events) != 1 || snap.Events[0].Summary != "started comms session: new project window" {
		t.Fatalf("bad current event log: %+v", snap.Events)
	}
	if got := actionByIDForTest(snap.Actions, "start_comms_session"); !got.Enabled {
		t.Fatalf("start should stay enabled so another named session can be created: %+v", got)
	}
	if got := actionByIDForTest(snap.Actions, "end_comms_session"); !got.Enabled {
		t.Fatalf("end should be enabled while session active: %+v", got)
	}
}

func TestUIServeStartCommsSessionReleasesActorPriorClaims(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "human-eli")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	mine := "01JX2Q3Y7W5B6N9P0R1S2T3V0A"
	other := "01JX2Q3Y7W5B6N9P0R1S2T3V0B"
	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	err = rt.Append(event.Event{
		TS:    time.Now().Add(-9 * time.Minute).UTC(),
		ID:    mine,
		Actor: "human-eli",
		Type:  event.TypeClaim,
		Scope: []string{"src/mine.ts"},
		Data:  map[string]interface{}{"intent": "old personal work"},
	})
	if err == nil {
		err = rt.Append(event.Event{
			TS:    time.Now().Add(-8 * time.Minute).UTC(),
			ID:    other,
			Actor: "claude-dev",
			Type:  event.TypeClaim,
			Scope: []string{"src/other.ts"},
			Data:  map[string]interface{}{"intent": "other actor work"},
		})
	}
	if closeErr := rt.Close(); closeErr != nil {
		t.Fatalf("close runtime: %v", closeErr)
	}
	if err != nil {
		t.Fatalf("append setup events: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/comms-session/start", strings.NewReader(`{"name":"fresh UI session"}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	uiServer{staleAfter: 90 * time.Minute}.serveStartCommsSession(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	rt, err = Open(OpenOpts{Mutating: false})
	if err != nil {
		t.Fatalf("reopen runtime: %v", err)
	}
	defer rt.Close()
	if got := rt.State.ClaimByID(mine); got != nil {
		t.Fatalf("starting a UI session should release actor's prior claim: %+v", got)
	}
	if got := rt.State.ClaimByID(other); got == nil {
		t.Fatalf("starting a UI session should not release other actors' claims")
	}
}

func TestUIServeRetireSessionActor(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "human-eli")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	claimID := "01JX2Q3Y7W5B6N9P0R1S2T3U5A"
	for _, ev := range []event.Event{
		{TS: time.Now().Add(-10 * time.Minute).UTC(), ID: "01JX2Q3Y7W5B6N9P0R1S2T3U5B", Actor: "claude-7e4c", Type: event.TypeHello, Data: map[string]interface{}{"base_name": "claude"}},
		{TS: time.Now().Add(-9 * time.Minute).UTC(), ID: claimID, Actor: "claude-7e4c", Type: event.TypeClaim, Scope: []string{"src/foo.ts"}, Data: map[string]interface{}{"intent": "old work"}},
	} {
		if err := rt.Append(ev); err != nil {
			t.Fatalf("append setup event: %v", err)
		}
	}
	if err := rt.Close(); err != nil {
		t.Fatalf("close runtime: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/session/retire", strings.NewReader(`{"actor":"claude-7e4c","reason":"renamed"}`))
	rec := httptest.NewRecorder()
	uiServer{staleAfter: 90 * time.Minute}.serveRetireSessionActor(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var snap uiSnapshot
	if err := json.Unmarshal(rec.Body.Bytes(), &snap); err != nil {
		t.Fatalf("decode snapshot: %v", err)
	}
	if len(snap.Sessions) != 0 || len(snap.Claims) != 0 {
		t.Fatalf("retired actor should be gone with claims released: sessions=%+v claims=%+v", snap.Sessions, snap.Claims)
	}
}

func TestUIServeReleaseClaim(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "human-eli")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	claimID := "01JX2Q3Y7W5B6N9P0R1S2T3U8A"
	if err := rt.Append(event.Event{
		TS:    time.Now().Add(-5 * time.Minute).UTC(),
		ID:    claimID,
		Actor: "human-eli",
		Type:  event.TypeClaim,
		Scope: []string{"src/foo.ts"},
		Data:  map[string]interface{}{"intent": "finish work"},
	}); err != nil {
		t.Fatalf("append setup event: %v", err)
	}
	if err := rt.Close(); err != nil {
		t.Fatalf("close runtime: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/claim/release", strings.NewReader(`{"claim_id":"`+claimID[:10]+`","result":"done"}`))
	rec := httptest.NewRecorder()
	uiServer{staleAfter: 90 * time.Minute}.serveReleaseClaim(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var snap uiSnapshot
	if err := json.Unmarshal(rec.Body.Bytes(), &snap); err != nil {
		t.Fatalf("decode snapshot: %v", err)
	}
	if len(snap.Claims) != 0 {
		t.Fatalf("released claim should be gone: %+v", snap.Claims)
	}
	if got := actionByIDForTest(snap.Actions, "release_claim"); got.Enabled {
		t.Fatalf("release action should be disabled with no claims: %+v", got)
	}
}

func TestUIServeRetireClaimOnlyActor(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "human-eli")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	claimID := "01JX2Q3Y7W5B6N9P0R1S2T3U7A"
	if err := rt.Append(event.Event{
		TS:    time.Now().Add(-9 * time.Minute).UTC(),
		ID:    claimID,
		Actor: "claude-old",
		Type:  event.TypeClaim,
		Scope: []string{"src/old.ts"},
		Data:  map[string]interface{}{"intent": "stale work"},
	}); err != nil {
		t.Fatalf("append setup event: %v", err)
	}
	if err := rt.Close(); err != nil {
		t.Fatalf("close runtime: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/session/retire", strings.NewReader(`{"actor":"claude-old","reason":"stale"}`))
	rec := httptest.NewRecorder()
	uiServer{staleAfter: 90 * time.Minute}.serveRetireSessionActor(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var snap uiSnapshot
	if err := json.Unmarshal(rec.Body.Bytes(), &snap); err != nil {
		t.Fatalf("decode snapshot: %v", err)
	}
	if len(snap.Claims) != 0 {
		t.Fatalf("claim-only actor claim should be released: %+v", snap.Claims)
	}
}

func TestUIServeTransferLeader(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "human-eli")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	for _, ev := range []event.Event{
		{TS: time.Now().Add(-10 * time.Minute).UTC(), ID: "01JX2Q3Y7W5B6N9P0R1S2T3U6A", Actor: "claude-7e4c", Type: event.TypeHello, Data: map[string]interface{}{"base_name": "claude", "leader": true}},
		{TS: time.Now().Add(-9 * time.Minute).UTC(), ID: "01JX2Q3Y7W5B6N9P0R1S2T3U6B", Actor: "claude-dev", Type: event.TypeHello, Data: map[string]interface{}{"base_name": "claude", "label": "Claude Dev"}},
	} {
		if err := rt.Append(ev); err != nil {
			t.Fatalf("append setup event: %v", err)
		}
	}
	if err := rt.Close(); err != nil {
		t.Fatalf("close runtime: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/session/lead", strings.NewReader(`{"actor":"claude-dev","reason":"lead now"}`))
	rec := httptest.NewRecorder()
	uiServer{staleAfter: 90 * time.Minute}.serveTransferLeader(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var snap uiSnapshot
	if err := json.Unmarshal(rec.Body.Bytes(), &snap); err != nil {
		t.Fatalf("decode snapshot: %v", err)
	}
	for _, session := range snap.Sessions {
		if session.Actor == "claude-dev" && !session.Leader {
			t.Fatalf("claude-dev should be leader: %+v", snap.Sessions)
		}
		if session.Actor == "claude-7e4c" && session.Leader {
			t.Fatalf("old actor should not remain leader: %+v", snap.Sessions)
		}
	}
}

func TestUISessionFromLikelyDeadConjunction(t *testing.T) {
	now := time.Now()
	stale := 90 * time.Minute
	cases := []struct {
		name     string
		lastSeen time.Time
		claims   int
		wantDead bool
	}{
		{"silent past window but holds nothing -> benign", now.Add(-3 * time.Hour), 0, false},
		{"silent past window AND holds a lock -> likely dead", now.Add(-3 * time.Hour), 2, true},
		{"active now holding locks -> alive", now.Add(-1 * time.Minute), 3, false},
		{"just under the window holding a lock -> not yet flagged", now.Add(-89 * time.Minute), 1, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			s := &state.Session{Actor: "a", TS: c.lastSeen, LastSeen: c.lastSeen}
			us := uiSessionFrom(s, now, c.claims, stale)
			if us.LikelyDead != c.wantDead {
				t.Fatalf("LikelyDead=%v want %v (silent %v, claims %d)", us.LikelyDead, c.wantDead, now.Sub(c.lastSeen), c.claims)
			}
			if us.ClaimCount != c.claims {
				t.Fatalf("ClaimCount=%d want %d", us.ClaimCount, c.claims)
			}
		})
	}
}

// A busy agent that hello'd long ago but keeps working (recent claim) is active
// everywhere via LastSeen; leader-transfer must agree and allow it. The existing
// reject test masked this because its stale actor had no post-hello events.
func TestUIServeTransferLeaderAllowsBusyActorWithOldHello(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "human-eli")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	for _, ev := range []event.Event{
		{TS: time.Now().Add(-5 * time.Hour).UTC(), ID: "01JX2Q3Y7W5B6N9P0R1S2T3U7A", Actor: "claude-busy", Type: event.TypeHello, Data: map[string]interface{}{"base_name": "claude"}},
		{TS: time.Now().Add(-2 * time.Minute).UTC(), ID: "01JX2Q3Y7W5B6N9P0R1S2T3U7B", Actor: "claude-busy", Type: event.TypeClaim, Scope: []string{"src/x.ts"}, Data: map[string]interface{}{"intent": "work"}},
	} {
		if err := rt.Append(ev); err != nil {
			t.Fatalf("append setup event: %v", err)
		}
	}
	if err := rt.Close(); err != nil {
		t.Fatalf("close runtime: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/session/lead", strings.NewReader(`{"actor":"claude-busy","reason":"lead now"}`))
	rec := httptest.NewRecorder()
	uiServer{staleAfter: 90 * time.Minute}.serveTransferLeader(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("busy actor (old hello, recent claim) must be allowed to lead; status=%d body=%s", rec.Code, rec.Body.String())
	}
}

func TestUIServeTransferLeaderRejectsStaleActor(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "human-eli")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	for _, ev := range []event.Event{
		{TS: time.Now().Add(-5 * time.Hour).UTC(), ID: "01JX2Q3Y7W5B6N9P0R1S2T3U6C", Actor: "claude-stale", Type: event.TypeHello, Data: map[string]interface{}{"base_name": "claude"}},
		{TS: time.Now().Add(-9 * time.Minute).UTC(), ID: "01JX2Q3Y7W5B6N9P0R1S2T3U6D", Actor: "codex-fresh", Type: event.TypeHello, Data: map[string]interface{}{"base_name": "codex", "leader": true}},
	} {
		if err := rt.Append(ev); err != nil {
			t.Fatalf("append setup event: %v", err)
		}
	}
	if err := rt.Close(); err != nil {
		t.Fatalf("close runtime: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/session/lead", strings.NewReader(`{"actor":"claude-stale","reason":"lead now"}`))
	rec := httptest.NewRecorder()
	uiServer{staleAfter: 90 * time.Minute}.serveTransferLeader(rec, req)

	if rec.Code != http.StatusConflict {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
}

func TestUIStartCommsSessionAllowsAnotherNamedSessionWhenLegacyEventsExist(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "human-eli")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	err = rt.Append(event.Event{
		TS:    time.Now().UTC(),
		ID:    "01JX2Q3Y7W5B6N9P0R1S2T3U4D",
		Actor: "claude-20260527-a",
		Type:  event.TypeHello,
		Data:  map[string]interface{}{"base_name": "claude"},
	})
	if closeErr := rt.Close(); closeErr != nil {
		t.Fatalf("close runtime: %v", closeErr)
	}
	if err != nil {
		t.Fatalf("append setup event: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/comms-session/start", strings.NewReader(`{"reason":"new project window"}`))
	rec := httptest.NewRecorder()
	uiServer{staleAfter: 90 * time.Minute}.serveStartCommsSession(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body = %s", rec.Code, rec.Body.String())
	}
}

func TestUIDemoRejectsEndCommsSession(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/api/comms-session/end", strings.NewReader(`{"reason":"done"}`))
	rec := httptest.NewRecorder()

	uiServer{demo: true, staleAfter: 90 * time.Minute}.serveEndCommsSession(rec, req)

	if rec.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "demo mode is read-only") {
		t.Fatalf("body = %q", rec.Body.String())
	}
}

func TestUIDemoRejectsStartCommsSession(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/api/comms-session/start", strings.NewReader(`{"reason":"start"}`))
	rec := httptest.NewRecorder()

	uiServer{demo: true, staleAfter: 90 * time.Minute}.serveStartCommsSession(rec, req)

	if rec.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "demo mode is read-only") {
		t.Fatalf("body = %q", rec.Body.String())
	}
}

func TestBuildCommsSessionViewsSplitsLogsPerSession(t *testing.T) {
	now := time.Now().UTC()
	firstHello := event.Event{TS: now, ID: "01JX2Q3Y7W5B6N9P0R1S2T3U1A", Actor: "claude-1", Type: event.TypeHello}
	firstEnd := event.Event{TS: now.Add(time.Second), ID: "01JX2Q3Y7W5B6N9P0R1S2T3U1B", Actor: "human-eli", Type: event.TypeRelease, Data: map[string]interface{}{"comms_session_end": true, "reason": "done"}}
	secondHello := event.Event{TS: now.Add(2 * time.Second), ID: "01JX2Q3Y7W5B6N9P0R1S2T3U1C", Actor: "codex-1", Type: event.TypeHello, Data: map[string]interface{}{"comms_session_start": true, "reason": "next"}}

	active, archived := buildCommsSessionViews([]event.Event{secondHello, firstEnd, firstHello})

	if len(active) != 1 || active[0].EventCount != 1 || active[0].Events[0].Actor != "codex-1" {
		t.Fatalf("bad current session: %+v", active)
	}
	if len(archived) != 1 || archived[0].EventCount != 2 {
		t.Fatalf("bad archived sessions: %+v", archived)
	}
	if archived[0].Events[0].Type != event.TypeRelease || archived[0].Events[1].Type != event.TypeHello {
		t.Fatalf("archive events should be newest first and scoped to first session: %+v", archived[0].Events)
	}
}

func TestBuildCommsSessionViewsGroupsNamedSessionsIndependently(t *testing.T) {
	now := time.Now().UTC()
	aStart := event.Event{TS: now, ID: "01JX2Q3Y7W5B6N9P0R1S2T3V1A", Actor: "claude-dev", Type: event.TypeHello, Data: map[string]interface{}{
		"comms_session_start": true, "comms_session_id": "sess-a", "comms_session_name": "dashboard fixes",
	}}
	bStart := event.Event{TS: now.Add(time.Second), ID: "01JX2Q3Y7W5B6N9P0R1S2T3V1B", Actor: "codex-dev", Type: event.TypeHello, Data: map[string]interface{}{
		"comms_session_start": true, "comms_session_id": "sess-b", "comms_session_name": "billing fixes",
	}}
	aClaim := event.Event{TS: now.Add(2 * time.Second), ID: "01JX2Q3Y7W5B6N9P0R1S2T3V1C", Actor: "claude-dev", Type: event.TypeClaim, Scope: []string{"src/a.ts"}, Data: map[string]interface{}{
		"intent": "work a", "comms_session_id": "sess-a", "comms_session_name": "dashboard fixes",
	}}
	bClaim := event.Event{TS: now.Add(3 * time.Second), ID: "01JX2Q3Y7W5B6N9P0R1S2T3V1D", Actor: "codex-dev", Type: event.TypeClaim, Scope: []string{"src/b.ts"}, Data: map[string]interface{}{
		"intent": "work b", "comms_session_id": "sess-b", "comms_session_name": "billing fixes",
	}}
	aEnd := event.Event{TS: now.Add(4 * time.Second), ID: "01JX2Q3Y7W5B6N9P0R1S2T3V1E", Actor: "human-eli", Type: event.TypeRelease, Data: map[string]interface{}{
		"comms_session_end": true, "comms_session_id": "sess-a", "comms_session_name": "dashboard fixes", "refs": []interface{}{aClaim.ID}, "reason": "done",
	}}

	active, archived := buildCommsSessionViews([]event.Event{aStart, bStart, aClaim, bClaim, aEnd})
	if len(active) != 1 || active[0].ID != "sess-b" || active[0].Name != "billing fixes" || active[0].ClaimCount != 1 {
		t.Fatalf("bad active named session: %+v", active)
	}
	if len(archived) != 1 || archived[0].ID != "sess-a" || archived[0].Name != "dashboard fixes" || archived[0].ReleasedRefs != 1 {
		t.Fatalf("bad archived named session: %+v", archived)
	}
}

func TestSessionSwitchReleasesPriorActorClaimsAndHidesDormantSession(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "claude-dev")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	if err := runSessionStart("old session", "Claude Dev"); err != nil {
		t.Fatalf("start old session: %v", err)
	}
	if err := runClaim("src/old.ts", "old work", "", ""); err != nil {
		t.Fatalf("claim old work: %v", err)
	}
	if err := runSessionStart("new session", "Claude Dev"); err != nil {
		t.Fatalf("start new session: %v", err)
	}
	if err := runClaim("src/new.ts", "new work", "", ""); err != nil {
		t.Fatalf("claim new work: %v", err)
	}

	rt, err := Open(OpenOpts{Mutating: false})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	defer rt.Close()
	if len(rt.State.Claims) != 1 {
		t.Fatalf("only the new-session claim should remain active, got %+v", rt.State.Claims)
	}
	for _, claim := range rt.State.Claims {
		if claim.Scope.String() != "src/new.ts" || claim.SessionName != "new session" {
			t.Fatalf("wrong active claim after session switch: %+v", claim)
		}
	}
	snap := buildUISnapshot(rt, 90*time.Minute)
	if len(snap.Active) != 1 || snap.Active[0].Name != "new session" {
		t.Fatalf("only new session should be active in UI: %+v", snap.Active)
	}
	if len(snap.Claims) != 1 || snap.Claims[0].SessionName != "new session" {
		t.Fatalf("UI claims should only include active new-session claim: %+v", snap.Claims)
	}
	if len(snap.Active[0].Claims) != 1 || snap.Active[0].Claims[0].Scope != "src/new.ts" {
		t.Fatalf("active session should carry only its own claims: %+v", snap.Active[0].Claims)
	}
	if len(snap.Active[0].Actors) != 1 || snap.Active[0].Actors[0] != "claude-dev" {
		t.Fatalf("old session actor should not remain active elsewhere: %+v", snap.Active[0].Actors)
	}
}

func TestActiveCommsSessionViewsIgnoreStaleHelloOnlyActors(t *testing.T) {
	now := time.Now().UTC()
	events := []event.Event{
		{TS: now.Add(-6 * time.Hour), ID: "01JX2Q3Y7W5B6N9P0R1S2T3W1A", Actor: "claude-old", Type: event.TypeHello, Data: map[string]interface{}{
			"comms_session_start": true, "comms_session_id": "sess-old", "comms_session_name": "old session",
		}},
		{TS: now.Add(-10 * time.Minute), ID: "01JX2Q3Y7W5B6N9P0R1S2T3W1B", Actor: "codex-new", Type: event.TypeHello, Data: map[string]interface{}{
			"comms_session_start": true, "comms_session_id": "sess-new", "comms_session_name": "new session",
		}},
	}
	st := state.Fold(events)
	active, _ := buildCommsSessionViews(events)
	filtered := filterActiveCommsSessionViews(active, st, now.Add(-4*time.Hour))

	if len(filtered) != 1 || filtered[0].ID != "sess-new" {
		t.Fatalf("only fresh hello-only session should remain active: %+v", filtered)
	}
	if filtered[0].Actors[0] != "codex-new" {
		t.Fatalf("fresh actor should be preserved: %+v", filtered[0].Actors)
	}
}

func TestActiveCommsSessionByNameIgnoresStaleHelloOnlySession(t *testing.T) {
	now := time.Now().UTC()
	events := []event.Event{
		{TS: now.Add(-6 * time.Hour), ID: event.NewID(now.Add(-6 * time.Hour)), Actor: "claude-old", Type: event.TypeHello, Data: map[string]interface{}{
			"comms_session_start": true, "comms_session_id": "sess-old", "comms_session_name": "shared name",
		}},
	}
	st := state.Fold(events)

	id, name := activeCommsSessionByName(st, "shared name", now.Add(-4*time.Hour))
	if id != "" || name != "" {
		t.Fatalf("stale hello-only session should not resolve as active: id=%q name=%q", id, name)
	}
}

func TestActiveCommsSessionByNameKeepsStaleSessionWithOpenClaim(t *testing.T) {
	now := time.Now().UTC()
	events := []event.Event{
		{TS: now.Add(-6 * time.Hour), ID: event.NewID(now.Add(-6 * time.Hour)), Actor: "claude-old", Type: event.TypeHello, Data: map[string]interface{}{
			"comms_session_start": true, "comms_session_id": "sess-old", "comms_session_name": "shared name",
		}},
		{TS: now.Add(-5 * time.Hour), ID: event.NewID(now.Add(-5 * time.Hour)), Actor: "claude-old", Type: event.TypeClaim, Scope: []string{"src/old.ts"}, Data: map[string]interface{}{
			"intent": "finish old session work", "comms_session_id": "sess-old", "comms_session_name": "shared name",
		}},
	}
	st := state.Fold(events)

	id, name := activeCommsSessionByName(st, "shared name", now.Add(-4*time.Hour))
	if id != "sess-old" || name != "shared name" {
		t.Fatalf("session with open claim should remain active by name: id=%q name=%q", id, name)
	}
}

func setupUITestRepo(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	for _, args := range [][]string{
		{"init"},
		{"config", "user.email", "test@example.com"},
		{"config", "user.name", "Test"},
		{"commit", "--allow-empty", "-m", "init"},
	} {
		cmd := exec.Command("git", args...)
		cmd.Dir = dir
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %s: %v: %s", strings.Join(args, " "), err, out)
		}
	}
	return dir
}

func actionByIDForTest(actions []uiAction, id string) uiAction {
	for _, action := range actions {
		if action.ID == id {
			return action
		}
	}
	return uiAction{}
}

// TestGuardMutationCSRF locks in the cross-origin / DNS-rebinding protection on
// the dashboard's state-changing endpoints (round-1 finding #04).
func TestGuardMutationCSRF(t *testing.T) {
	cases := []struct {
		name    string
		method  string
		origin  string
		host    string
		blocked bool
	}{
		{"no-origin non-browser client", http.MethodPost, "", "127.0.0.1:7878", false},
		{"same loopback origin", http.MethodPost, "http://127.0.0.1:7878", "127.0.0.1:7878", false},
		{"localhost origin", http.MethodPost, "http://localhost:7878", "localhost:7878", false},
		{"classic cross-origin CSRF", http.MethodPost, "https://evil.example", "127.0.0.1:7878", true},
		{"dns-rebinding origin==host non-loopback", http.MethodPost, "http://evil.example:7878", "evil.example:7878", true},
		{"wrong method", http.MethodGet, "", "127.0.0.1:7878", true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			req := httptest.NewRequest(c.method, "/api/claim/release", strings.NewReader("{}"))
			req.Host = c.host
			if c.origin != "" {
				req.Header.Set("Origin", c.origin)
			}
			rec := httptest.NewRecorder()
			if got := guardMutation(rec, req); got != c.blocked {
				t.Fatalf("guardMutation blocked=%v, want %v (status %d)", got, c.blocked, rec.Code)
			}
		})
	}
}
