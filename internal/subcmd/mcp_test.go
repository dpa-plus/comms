package subcmd

import (
	"encoding/json"
	"strings"
	"testing"
)

// drive runs a batch of JSON-RPC frames through the server and returns the
// decoded responses in order.
func drive(t *testing.T, frames ...string) []map[string]interface{} {
	t.Helper()
	var out strings.Builder
	if err := serveMCP(strings.NewReader(strings.Join(frames, "\n")+"\n"), &out); err != nil {
		t.Fatalf("serveMCP: %v", err)
	}
	var got []map[string]interface{}
	for _, line := range strings.Split(strings.TrimSpace(out.String()), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		var m map[string]interface{}
		if err := json.Unmarshal([]byte(line), &m); err != nil {
			t.Fatalf("bad frame %q: %v", line, err)
		}
		got = append(got, m)
	}
	return got
}

func TestMCPHandshakeAndToolList(t *testing.T) {
	got := drive(t,
		`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}`,
		`{"jsonrpc":"2.0","id":2,"method":"tools/list"}`)
	if len(got) != 2 {
		t.Fatalf("expected 2 responses, got %d", len(got))
	}
	res := got[0]["result"].(map[string]interface{})
	if res["protocolVersion"] != mcpProtocolVersion {
		t.Errorf("protocolVersion = %v, want %s", res["protocolVersion"], mcpProtocolVersion)
	}
	tools := got[1]["result"].(map[string]interface{})["tools"].([]interface{})
	want := map[string]bool{"comms_check": false, "comms_claim": false, "comms_release": false,
		"comms_status": false, "comms_note": false, "comms_find": false}
	for _, tl := range tools {
		m := tl.(map[string]interface{})
		name, _ := m["name"].(string)
		if _, ok := want[name]; !ok {
			t.Errorf("unexpected tool %q", name)
			continue
		}
		want[name] = true
		// Every tool the model can call must describe its own arguments, or the
		// model has to guess and will guess wrong.
		if _, ok := m["inputSchema"].(map[string]interface{}); !ok {
			t.Errorf("tool %q has no inputSchema", name)
		}
		if d, _ := m["description"].(string); d == "" {
			t.Errorf("tool %q has no description", name)
		}
	}
	for name, seen := range want {
		if !seen {
			t.Errorf("tool %q missing", name)
		}
	}
}

// A notification carries no id and must never be answered. Some clients treat a
// reply to one as a fatal protocol violation and drop the connection.
func TestMCPNotificationIsNotAnswered(t *testing.T) {
	got := drive(t,
		`{"jsonrpc":"2.0","method":"notifications/initialized"}`,
		`{"jsonrpc":"2.0","id":7,"method":"ping"}`)
	if len(got) != 1 {
		t.Fatalf("expected exactly 1 response (the ping), got %d: %v", len(got), got)
	}
	if got[0]["id"].(float64) != 7 {
		t.Errorf("answered the wrong frame: %v", got[0])
	}
}

// An unparseable line must not kill the loop — the next valid frame still works.
func TestMCPSurvivesAGarbageFrame(t *testing.T) {
	got := drive(t,
		`{"jsonrpc":"2.0"`,
		`not json at all`,
		`{"jsonrpc":"2.0","id":9,"method":"ping"}`)
	if len(got) != 1 || got[0]["id"].(float64) != 9 {
		t.Fatalf("garbage frames should be skipped, got %v", got)
	}
}

func TestMCPUnknownMethodIsAProtocolError(t *testing.T) {
	got := drive(t, `{"jsonrpc":"2.0","id":3,"method":"nope/there"}`)
	if len(got) != 1 {
		t.Fatalf("expected 1 response, got %d", len(got))
	}
	if got[0]["error"] == nil {
		t.Fatalf("unknown method should be a protocol error, got %v", got[0])
	}
}
