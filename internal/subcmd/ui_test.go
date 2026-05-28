package subcmd

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/dpa-plus/comms/internal/event"
)

func TestBuildDemoUISnapshotMarksStaleWithoutMutations(t *testing.T) {
	snap := buildDemoUISnapshot(90 * time.Minute)

	if !snap.Project.Demo {
		t.Fatalf("demo snapshot should be marked demo")
	}
	if snap.Project.MutationsEnabled {
		t.Fatalf("demo snapshot must not enable mutations")
	}
	if len(snap.Claims) != 3 {
		t.Fatalf("demo claims len = %d, want 3", len(snap.Claims))
	}
	if !snap.Sessions[0].Leader {
		t.Fatalf("first demo session should be leader")
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

func TestUIServeReleaseClaimAppendsReleaseEvent(t *testing.T) {
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
		TS:    time.Now().Add(-8 * time.Hour).UTC(),
		ID:    claimID,
		Actor: "claude-20260527-a",
		Type:  event.TypeClaim,
		Scope: []string{"src/stale.ts"},
		Data:  map[string]interface{}{"intent": "stale work"},
	})
	if closeErr := rt.Close(); closeErr != nil {
		t.Fatalf("close runtime: %v", closeErr)
	}
	if err != nil {
		t.Fatalf("append claim: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/claims/release", strings.NewReader(`{"id":"`+claimID[:10]+`","reason":"user verified session ended"}`))
	req.Header.Set("Content-Type", "application/json")
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
		t.Fatalf("claims still active after release: %+v", snap.Claims)
	}
	if len(snap.Events) != 2 || snap.Events[0].Type != event.TypeRelease {
		t.Fatalf("newest event should be release, got %+v", snap.Events)
	}

	rt, err = Open(OpenOpts{Mutating: false})
	if err != nil {
		t.Fatalf("reopen runtime: %v", err)
	}
	defer rt.Close()
	if got := rt.State.ClaimByID(claimID); got != nil {
		t.Fatalf("claim still active after UI release: %+v", got)
	}
	if len(rt.Events) != 2 {
		t.Fatalf("event count = %d, want 2", len(rt.Events))
	}
	release := rt.Events[1]
	if release.Type != event.TypeRelease {
		t.Fatalf("last event type = %s, want release", release.Type)
	}
	if release.Data["original_actor"] != "claude-20260527-a" {
		t.Fatalf("original_actor = %v", release.Data["original_actor"])
	}
}

func TestUIServeReleaseClaimRejectsFreshClaimWithoutForce(t *testing.T) {
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
		TS:    time.Now().Add(-2 * time.Minute).UTC(),
		ID:    claimID,
		Actor: "claude-20260527-a",
		Type:  event.TypeClaim,
		Scope: []string{"src/fresh.ts"},
		Data:  map[string]interface{}{"intent": "fresh work"},
	})
	if closeErr := rt.Close(); closeErr != nil {
		t.Fatalf("close runtime: %v", closeErr)
	}
	if err != nil {
		t.Fatalf("append claim: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/claims/release", strings.NewReader(`{"id":"`+claimID+`","reason":"should not close"}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	uiServer{staleAfter: 90 * time.Minute}.serveReleaseClaim(rec, req)

	if rec.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409, body = %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "force=true") {
		t.Fatalf("body = %q", rec.Body.String())
	}

	rt, err = Open(OpenOpts{Mutating: false})
	if err != nil {
		t.Fatalf("reopen runtime: %v", err)
	}
	defer rt.Close()
	if got := rt.State.ClaimByID(claimID); got == nil {
		t.Fatalf("fresh claim should remain active")
	}
	if len(rt.Events) != 1 {
		t.Fatalf("event count = %d, want only original claim", len(rt.Events))
	}
}

func TestUIServeReleaseClaimClearsFreshClaimWithForce(t *testing.T) {
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
		TS:    time.Now().Add(-2 * time.Minute).UTC(),
		ID:    claimID,
		Actor: "claude-20260527-a",
		Type:  event.TypeClaim,
		Scope: []string{"src/fresh.ts"},
		Data:  map[string]interface{}{"intent": "fresh work"},
	})
	if closeErr := rt.Close(); closeErr != nil {
		t.Fatalf("close runtime: %v", closeErr)
	}
	if err != nil {
		t.Fatalf("append claim: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/claims/release", strings.NewReader(`{"id":"`+claimID+`","reason":"user cleared active claim","force":true}`))
	req.Header.Set("Content-Type", "application/json")
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
		t.Fatalf("claims still active after forced clear: %+v", snap.Claims)
	}

	rt, err = Open(OpenOpts{Mutating: false})
	if err != nil {
		t.Fatalf("reopen runtime: %v", err)
	}
	defer rt.Close()
	if got := rt.State.ClaimByID(claimID); got != nil {
		t.Fatalf("fresh claim should have been cleared: %+v", got)
	}
	if len(rt.Events) != 2 {
		t.Fatalf("event count = %d, want claim + release", len(rt.Events))
	}
	release := rt.Events[1]
	if release.Data["result"] != "claim cleared from comms ui" {
		t.Fatalf("release result = %v", release.Data["result"])
	}
}

func TestUIDemoRejectsRelease(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/api/claims/release", strings.NewReader(`{"id":"01JX"}`))
	rec := httptest.NewRecorder()

	uiServer{demo: true, staleAfter: 90 * time.Minute}.serveReleaseClaim(rec, req)

	if rec.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "demo mode is read-only") {
		t.Fatalf("body = %q", rec.Body.String())
	}
}

func TestUIActionEndpointsAppendEventsAndDocs(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "claude-leader")
	t.Setenv("USER", "eli")
	t.Chdir(repo)
	server := uiServer{staleAfter: 90 * time.Minute}

	post := func(path, body string) *httptest.ResponseRecorder {
		t.Helper()
		req := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		rec := httptest.NewRecorder()
		switch path {
		case "/api/hello":
			server.serveHello(rec, req)
		case "/api/claims":
			server.serveCreateClaim(rec, req)
		case "/api/claims/release-mine":
			server.serveReleaseMine(rec, req)
		case "/api/notes":
			server.serveCreateNote(rec, req)
		case "/api/findings":
			server.serveCreateFinding(rec, req)
		default:
			t.Fatalf("unknown path %s", path)
		}
		return rec
	}

	if rec := post("/api/hello", `{}`); rec.Code != http.StatusOK {
		t.Fatalf("hello status = %d body = %s", rec.Code, rec.Body.String())
	}
	if rec := post("/api/claims", `{"scope":"src/ui.ts","intent":"edit ui"}`); rec.Code != http.StatusOK {
		t.Fatalf("claim status = %d body = %s", rec.Code, rec.Body.String())
	}
	if rec := post("/api/notes", `{"body":"everyone should know","priority":true}`); rec.Code != http.StatusOK {
		t.Fatalf("note status = %d body = %s", rec.Code, rec.Body.String())
	}
	if rec := post("/api/findings", `{"category":"decision","summary":"use the UI","refs":[{"kind":"doc","value":"ui"}],"priority":true}`); rec.Code != http.StatusOK {
		t.Fatalf("finding status = %d body = %s", rec.Code, rec.Body.String())
	}

	req := httptest.NewRequest(http.MethodPut, "/api/docs/ui", strings.NewReader(`{"body":"# ui\n\nUse actions."}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	server.serveDoc(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("doc save status = %d body = %s", rec.Code, rec.Body.String())
	}

	req = httptest.NewRequest(http.MethodGet, "/api/docs/ui", nil)
	rec = httptest.NewRecorder()
	server.serveDoc(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("doc get status = %d body = %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "Use actions.") {
		t.Fatalf("doc body missing saved content: %s", rec.Body.String())
	}

	rt, err := Open(OpenOpts{Mutating: false})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	defer rt.Close()
	if len(rt.Events) != 5 {
		t.Fatalf("events len = %d, want 5", len(rt.Events))
	}
	if len(rt.State.Claims) != 1 {
		t.Fatalf("claims len = %d, want 1", len(rt.State.Claims))
	}
	if len(rt.State.Notes) != 1 || !rt.State.Notes[0].Priority {
		t.Fatalf("priority note not recorded: %+v", rt.State.Notes)
	}
	if len(rt.State.Findings) != 2 || !rt.State.Findings[0].Priority {
		t.Fatalf("findings not recorded: %+v", rt.State.Findings)
	}

	if rec := post("/api/claims/release-mine", `{"mode":"latest","result":"done"}`); rec.Code != http.StatusOK {
		t.Fatalf("release-mine status = %d body = %s", rec.Code, rec.Body.String())
	}
	rt2, err := Open(OpenOpts{Mutating: false})
	if err != nil {
		t.Fatalf("open runtime after release: %v", err)
	}
	defer rt2.Close()
	if len(rt2.State.Claims) != 0 {
		t.Fatalf("claims len after release = %d, want 0", len(rt2.State.Claims))
	}
}

func TestUICheckReportsConflicts(t *testing.T) {
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
		ID:    "01JX2Q3Y7W5B6N9P0R1S2T3U4V",
		Actor: "claude-20260527-a",
		Type:  event.TypeClaim,
		Scope: []string{"src/conflict.ts"},
		Data:  map[string]interface{}{"intent": "edit conflict"},
	})
	if closeErr := rt.Close(); closeErr != nil {
		t.Fatalf("close runtime: %v", closeErr)
	}
	if err != nil {
		t.Fatalf("append claim: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/check", strings.NewReader(`{"path":"src/conflict.ts"}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	uiServer{staleAfter: 90 * time.Minute}.serveCheck(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d body = %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"clear": false`) {
		t.Fatalf("expected blocked check response, got %s", rec.Body.String())
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
