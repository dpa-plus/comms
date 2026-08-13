package tests

import (
	"encoding/json"
	"os/exec"
	"regexp"
	"strings"
	"testing"
)

var claimIDPattern = regexp.MustCompile(`(?m)^  ID: ([0-9A-Z]+)$`)

type releaseStatus struct {
	Claims []struct {
		ID    string `json:"id"`
		Scope string `json:"scope"`
	} `json:"claims"`
}

func openClaim(t *testing.T, bin, repo, home, actor, scope string) string {
	t.Helper()
	cmd := exec.Command(bin, "claim", scope, "--intent", "test claim")
	cmd.Dir = repo
	cmd.Env = childEnv(home, actor)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("claim %s: %v: %s", scope, err, out)
	}
	match := claimIDPattern.FindSubmatch(out)
	if len(match) != 2 {
		t.Fatalf("claim output missing full ID: %s", out)
	}
	return string(match[1])
}

func activeReleaseClaims(t *testing.T, bin, repo, home, actor string) map[string]string {
	t.Helper()
	cmd := exec.Command(bin, "status", "--json")
	cmd.Dir = repo
	cmd.Env = childEnv(home, actor)
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("status: %v", err)
	}
	var status releaseStatus
	if err := json.Unmarshal(out, &status); err != nil {
		t.Fatalf("parse status JSON: %v\n%s", err, out)
	}
	claims := make(map[string]string, len(status.Claims))
	for _, claim := range status.Claims {
		claims[claim.ID] = claim.Scope
	}
	return claims
}

func TestReleaseSeveralSelectedClaimsAtomically(t *testing.T) {
	bin := buildCommsBinary(t)
	repo := setupTestRepo(t)
	home := t.TempDir()
	actor := "current-agent"

	first := openClaim(t, bin, repo, home, actor, "src/first.txt")
	second := openClaim(t, bin, repo, home, actor, "src/second.txt")
	third := openClaim(t, bin, repo, home, actor, "src/keep.txt")

	release := exec.Command(bin, "release", first[:10], second[:10], "--result", "selected work committed")
	release.Dir = repo
	release.Env = childEnv(home, actor)
	out, err := release.CombinedOutput()
	if err != nil {
		t.Fatalf("release selected claims: %v: %s", err, out)
	}
	text := string(out)
	if !strings.Contains(text, "src/first.txt") || !strings.Contains(text, "src/second.txt") {
		t.Fatalf("release output must name both selected scopes: %s", text)
	}
	if strings.Contains(text, "src/keep.txt") {
		t.Fatalf("release output must not include the unselected claim: %s", text)
	}

	claims := activeReleaseClaims(t, bin, repo, home, actor)
	if _, ok := claims[first]; ok {
		t.Fatalf("first selected claim remains active")
	}
	if _, ok := claims[second]; ok {
		t.Fatalf("second selected claim remains active")
	}
	if got := claims[third]; got != "src/keep.txt" {
		t.Fatalf("unselected claim = %q, want src/keep.txt", got)
	}
}

func TestReleaseSeveralIDsIsAllOrNothingWhenOneIsInvalid(t *testing.T) {
	bin := buildCommsBinary(t)
	repo := setupTestRepo(t)
	home := t.TempDir()
	actor := "current-agent"

	first := openClaim(t, bin, repo, home, actor, "src/first.txt")
	second := openClaim(t, bin, repo, home, actor, "src/second.txt")

	release := exec.Command(bin, "release", first[:10], "NOT-A-CLAIM", second[:10])
	release.Dir = repo
	release.Env = childEnv(home, actor)
	out, err := release.CombinedOutput()
	if err == nil {
		t.Fatalf("release with invalid ID should fail: %s", out)
	}
	if !strings.Contains(string(out), `no active claim matches "NOT-A-CLAIM"`) {
		t.Fatalf("multi-release must resolve every supplied ID before writing; got: %s", out)
	}

	claims := activeReleaseClaims(t, bin, repo, home, actor)
	if claims[first] != "src/first.txt" || claims[second] != "src/second.txt" {
		t.Fatalf("invalid multi-release changed active claims: %+v", claims)
	}
}

func TestReleaseSeveralIDsRejectsTheSameClaimTwice(t *testing.T) {
	bin := buildCommsBinary(t)
	repo := setupTestRepo(t)
	home := t.TempDir()
	actor := "current-agent"

	claimID := openClaim(t, bin, repo, home, actor, "src/one.txt")
	release := exec.Command(bin, "release", claimID, claimID[:10])
	release.Dir = repo
	release.Env = childEnv(home, actor)
	out, err := release.CombinedOutput()
	if err == nil {
		t.Fatalf("duplicate selected claim should fail: %s", out)
	}
	if !strings.Contains(string(out), "selects claim") || !strings.Contains(string(out), "more than once") {
		t.Fatalf("duplicate selection needs a clear error: %s", out)
	}
	if got := activeReleaseClaims(t, bin, repo, home, actor)[claimID]; got != "src/one.txt" {
		t.Fatalf("duplicate selection released the claim; active scope = %q", got)
	}
}
