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
	if got := actionByIDForTest(snap.Actions, "start_comms_session"); got.Enabled {
		t.Fatalf("start should be disabled while session active: %+v", got)
	}
	if got := actionByIDForTest(snap.Actions, "end_comms_session"); !got.Enabled {
		t.Fatalf("end should be enabled while session active: %+v", got)
	}
}

func TestUIStartCommsSessionRejectsWhenActive(t *testing.T) {
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

	if rec.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409, body = %s", rec.Code, rec.Body.String())
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

	current, archived := buildCommsSessionViews([]event.Event{secondHello, firstEnd, firstHello})

	if current == nil || current.EventCount != 1 || current.Events[0].Actor != "codex-1" {
		t.Fatalf("bad current session: %+v", current)
	}
	if len(archived) != 1 || archived[0].EventCount != 2 {
		t.Fatalf("bad archived sessions: %+v", archived)
	}
	if archived[0].Events[0].Type != event.TypeRelease || archived[0].Events[1].Type != event.TypeHello {
		t.Fatalf("archive events should be newest first and scoped to first session: %+v", archived[0].Events)
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
