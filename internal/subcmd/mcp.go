package subcmd

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strings"
	"time"

	"github.com/dpa-plus/comms/internal/event"
	"github.com/dpa-plus/comms/internal/overlap"
	"github.com/dpa-plus/comms/internal/state"
	"github.com/spf13/cobra"
)

// `comms mcp`: the coordination protocol as tools the model already has.
//
// docs/DESIGN.md lists "MCP server" under what we cut, because mcp-agent-mail's
// version came wrapped in severity ladders, threaded inboxes and registration
// tokens, "ceremony that agents kept skipping". That judgement was about the
// ceremony and it was right. It was wrong about the transport, and the evidence
// is in the log: across 4,356 real claims exactly one carried a task, because
// the skill that describes tasks forbids auto-triggering and only loads when a
// human types its name. A CLI an agent must be told to run loses to a tool
// sitting in its tool list every turn.
//
// So this is the same protocol with none of the ceremony: six verbs that map
// one-to-one onto commands that already exist, no registration, no inbox, no
// severity. Anything an agent must learn before its first call is ceremony and
// does not belong here.
//
// It speaks JSON-RPC 2.0 over stdio with no SDK. comms has four direct
// dependencies and the subset of MCP a tool server needs: initialize,
// tools/list, tools/call: is small, stable, and cheaper to own than a
// dependency is to carry.

const mcpProtocolVersion = "2025-06-18"

// NewMCPCmd builds `comms mcp`.
func NewMCPCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "mcp",
		Short: "Serve comms as MCP tools over stdio",
		Long: `Serve the coordination verbs as MCP tools on stdin/stdout.

Point an MCP-capable agent at this and claiming, checking and releasing become
tools in its list every turn, rather than commands somebody has to remember to
run. The tools mirror the CLI exactly and write the same events to the same log,
so a session using the tools and a session using the CLI coordinate with each
other without knowing which is which.

Every tool takes an "actor" argument. One server process can therefore act for
several agents over one connection, which COMMS_ACTOR alone cannot express since
it is per process. COMMS_ACTOR still works as the default when the argument is
omitted.`,
		Args:         cobra.NoArgs,
		SilenceUsage: true,
		RunE:         func(cmd *cobra.Command, _ []string) error { return serveMCP(cmd.InOrStdin(), cmd.OutOrStdout()) },
	}
}

type rpcRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type rpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Result  interface{}     `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

// serveMCP runs the stdio loop until stdin closes.
func serveMCP(in io.Reader, out io.Writer) error {
	rd := bufio.NewScanner(in)
	rd.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
	enc := json.NewEncoder(out)
	for rd.Scan() {
		line := strings.TrimSpace(rd.Text())
		if line == "" {
			continue
		}
		var req rpcRequest
		if err := json.Unmarshal([]byte(line), &req); err != nil {
			continue // a frame we cannot parse has no id to answer on
		}
		// A notification (no id) gets no reply, ever: answering one is a
		// protocol violation that some clients treat as fatal.
		if len(req.ID) == 0 {
			continue
		}
		resp := rpcResponse{JSONRPC: "2.0", ID: req.ID}
		result, rerr := dispatchMCP(req)
		if rerr != nil {
			resp.Error = rerr
		} else {
			resp.Result = result
		}
		if err := enc.Encode(resp); err != nil {
			return err
		}
	}
	return rd.Err()
}

func dispatchMCP(req rpcRequest) (interface{}, *rpcError) {
	switch req.Method {
	case "initialize":
		return map[string]interface{}{
			"protocolVersion": mcpProtocolVersion,
			"capabilities":    map[string]interface{}{"tools": map[string]interface{}{}},
			"serverInfo":      map[string]interface{}{"name": "comms", "version": "1"},
		}, nil
	case "ping":
		return map[string]interface{}{}, nil
	case "tools/list":
		return map[string]interface{}{"tools": mcpTools()}, nil
	case "tools/call":
		return callMCPTool(req.Params)
	default:
		return nil, &rpcError{Code: -32601, Message: "unknown method " + req.Method}
	}
}

// obj is a tiny helper so the schemas below read as data rather than as code.
type obj = map[string]interface{}

func strProp(desc string) obj { return obj{"type": "string", "description": desc} }

func mcpTools() []obj {
	actorProp := strProp("the calling agent's actor name, e.g. claude-dev. Defaults to COMMS_ACTOR.")
	return []obj{
		{
			"name":        "comms_check",
			"description": "Is anyone else editing this path? Call before editing a file. Returns clear, or who holds it and why.",
			"inputSchema": obj{"type": "object", "required": []string{"path"}, "properties": obj{
				"actor": actorProp,
				"path":  strProp("repo-relative path, optionally #L10-40 or #symbolName"),
			}},
		},
		{
			"name":        "comms_claim",
			"description": "Take an exclusive hold on a path before editing it. Refused if someone else holds an overlapping scope, and the refusal is recorded.",
			"inputSchema": obj{"type": "object", "required": []string{"path", "intent"}, "properties": obj{
				"actor":  actorProp,
				"path":   strProp("repo-relative path, optionally #L10-40 or #symbolName"),
				"intent": strProp("what you are about to do to it, one line"),
				"task":   strProp("slug of the task this claim carries out, if any"),
			}},
		},
		{
			"name":        "comms_release",
			"description": "Release every claim you hold, with a note of what came of it.",
			"inputSchema": obj{"type": "object", "required": []string{"result"}, "properties": obj{
				"actor":  actorProp,
				"result": strProp("what happened, e.g. 'merged as #321'"),
			}},
		},
		{
			"name":        "comms_status",
			"description": "Who is active, what is claimed, what has gone stale, and how many collisions have been prevented.",
			"inputSchema": obj{"type": "object", "properties": obj{"actor": actorProp}},
		},
		{
			"name":        "comms_note",
			"description": "Leave a short FYI for the other agents on this repo.",
			"inputSchema": obj{"type": "object", "required": []string{"body"}, "properties": obj{
				"actor": actorProp,
				"body":  strProp("the note"),
			}},
		},
		{
			"name":        "comms_find",
			"description": "Record something worth keeping: a decision and its reason, or a trap the next agent should not fall into. Anchored to the file you hold, so it resurfaces when somebody claims that file.",
			"inputSchema": obj{"type": "object", "required": []string{"category", "summary"}, "properties": obj{
				"actor":    actorProp,
				"category": obj{"type": "string", "enum": []string{"bug", "fix", "ship", "decision", "gotcha"}, "description": "bug=open problem, fix=resolved, ship=released, decision=architectural choice, gotcha=persistent trap"},
				"summary":  strProp("one line, specific enough to act on"),
				"ref":      strProp("optional anchor, e.g. path:src/auth.ts"),
			}},
		},
	}
}

type toolCall struct {
	Name      string `json:"name"`
	Arguments obj    `json:"arguments"`
}

// text wraps a plain string in the content shape MCP expects. isError marks a
// tool-level failure, which the model sees and can act on: as opposed to a
// protocol error, which it cannot.
func text(s string, isError bool) obj {
	return obj{
		"content": []obj{{"type": "text", "text": s}},
		"isError": isError,
	}
}

func argStr(a obj, k string) string {
	if v, ok := a[k].(string); ok {
		return strings.TrimSpace(v)
	}
	return ""
}

func callMCPTool(params json.RawMessage) (interface{}, *rpcError) {
	var call toolCall
	if err := json.Unmarshal(params, &call); err != nil {
		return nil, &rpcError{Code: -32602, Message: "bad params: " + err.Error()}
	}
	a := call.Arguments
	if a == nil {
		a = obj{}
	}
	actorName := argStr(a, "actor")

	switch call.Name {
	case "comms_check":
		return mcpCheck(actorName, argStr(a, "path"))
	case "comms_claim":
		return mcpClaim(actorName, argStr(a, "path"), argStr(a, "intent"), argStr(a, "task"))
	case "comms_release":
		return mcpRelease(actorName, argStr(a, "result"))
	case "comms_status":
		return mcpStatus(actorName)
	case "comms_note":
		return mcpNote(actorName, argStr(a, "body"))
	case "comms_find":
		return mcpFind(actorName, argStr(a, "category"), argStr(a, "summary"), argStr(a, "ref"))
	default:
		return nil, &rpcError{Code: -32602, Message: "unknown tool " + call.Name}
	}
}

// openMCP resolves a runtime for one tool call. Tool-level failures come back as
// text the model can read, never as a process exit: a server that exits on a
// conflict takes the whole session down with it.
func openMCP(actorName string, mutating bool) (*Runtime, *rpcError) {
	rt, err := Open(OpenOpts{Mutating: mutating, Actor: actorName})
	if err != nil {
		return nil, &rpcError{Code: -32603, Message: err.Error()}
	}
	return rt, nil
}

func mcpCheck(actorName, path string) (interface{}, *rpcError) {
	if path == "" {
		return text("path is required", true), nil
	}
	scope, err := overlap.Parse(path)
	if err != nil {
		return text(err.Error(), true), nil
	}
	rt, rerr := openMCP(actorName, false)
	if rerr != nil {
		return nil, rerr
	}
	defer func() { _ = rt.Close() }()

	conflicts := rt.State.ConflictsFor(scope, rt.Actor)
	if len(conflicts) == 0 {
		return text("clear: nobody else holds "+scope.String(), false), nil
	}
	h := conflicts[0]
	return text(fmt.Sprintf(
		"BLOCKED: %s is held by @%s (intent: %q, since %s). Do not edit it. Pick another file, or ask them in a note.",
		scope.String(), h.Actor, h.Intent, h.TS.Format(time.RFC3339)), true), nil
}

func mcpClaim(actorName, path, intent, task string) (interface{}, *rpcError) {
	if path == "" || intent == "" {
		return text("path and intent are both required", true), nil
	}
	scope, err := overlap.Parse(path)
	if err != nil {
		return text(err.Error(), true), nil
	}
	rt, rerr := openMCP(actorName, true)
	if rerr != nil {
		return nil, rerr
	}
	defer func() { _ = rt.Close() }()

	if conflicts := rt.State.ConflictsFor(scope, rt.Actor); len(conflicts) > 0 {
		h := conflicts[0]
		// Same evidence trail the CLI writes: a refusal that is not recorded
		// cannot be counted, and counting them is the only proof comms works.
		recordBlocked(rt, scope.String(), intent, conflicts)
		return text(fmt.Sprintf(
			"REFUSED: %s is already held by @%s (intent: %q). Nothing was claimed.",
			scope.String(), h.Actor, h.Intent), true), nil
	}
	data := obj{"intent": intent}
	if task != "" {
		data["task"] = task
	}
	stampActiveCommsSession(rt, data)
	now := time.Now().UTC()
	ev := event.Event{TS: now, ID: event.NewID(now), Actor: rt.Actor,
		Type: event.TypeClaim, Scope: []string{scope.String()}, Data: data}
	if err := rt.Append(ev); err != nil {
		return nil, &rpcError{Code: -32603, Message: err.Error()}
	}
	out := fmt.Sprintf("claimed %s as @%s (id %s)", scope.String(), rt.Actor, ev.ID)
	if prior := findingsOnScopes(rt.State, []overlap.Scope{scope}, 3); len(prior) > 0 {
		out += "\n\nprior context on this path:"
		for _, f := range prior {
			out += fmt.Sprintf("\n  [%s] %s (@%s)", f.Category, f.Summary, f.Actor)
		}
	}
	return text(out, false), nil
}

func mcpRelease(actorName, result string) (interface{}, *rpcError) {
	if result == "" {
		return text("result is required: say what came of the work", true), nil
	}
	rt, rerr := openMCP(actorName, true)
	if rerr != nil {
		return nil, rerr
	}
	defer func() { _ = rt.Close() }()

	held := rt.State.ActiveClaimsByActor(rt.Actor)
	if len(held) == 0 {
		return text("you hold no claims", false), nil
	}
	scopes := make([]string, 0, len(held))
	refs := make([]string, 0, len(held))
	for _, c := range held {
		scopes = append(scopes, c.Scope.String())
		refs = append(refs, c.ID)
	}
	data := obj{"result": result, "refs": refs}
	stampActiveCommsSession(rt, data)
	now := time.Now().UTC()
	if err := rt.Append(event.Event{TS: now, ID: event.NewID(now), Actor: rt.Actor,
		Type: event.TypeRelease, Scope: scopes, Data: data}); err != nil {
		return nil, &rpcError{Code: -32603, Message: err.Error()}
	}
	return text(fmt.Sprintf("released %d claim(s): %s", len(scopes), strings.Join(scopes, ", ")), false), nil
}

func mcpStatus(actorName string) (interface{}, *rpcError) {
	rt, rerr := openMCP(actorName, false)
	if rerr != nil {
		return nil, rerr
	}
	defer func() { _ = rt.Close() }()

	var b strings.Builder
	fmt.Fprintf(&b, "%d active claim(s), %d agent(s) seen\n", len(rt.State.Claims), len(rt.State.Sessions))
	claims := make([]*state.Claim, 0, len(rt.State.Claims))
	for _, c := range rt.State.Claims {
		claims = append(claims, c)
	}
	sort.Slice(claims, func(i, j int) bool { return claims[i].TS.Before(claims[j].TS) })
	for _, c := range claims {
		fmt.Fprintf(&b, "  %s: @%s (%s)\n", c.Scope.String(), c.Actor, c.Intent)
	}
	if n := len(rt.State.Blocked); n > 0 {
		fmt.Fprintf(&b, "collisions prevented: %d\n", n)
	}
	return text(strings.TrimRight(b.String(), "\n"), false), nil
}

func mcpNote(actorName, body string) (interface{}, *rpcError) {
	if body == "" {
		return text("body is required", true), nil
	}
	if err := rejectControlText("note", body, 280); err != nil {
		return text(err.Error(), true), nil
	}
	rt, rerr := openMCP(actorName, true)
	if rerr != nil {
		return nil, rerr
	}
	defer func() { _ = rt.Close() }()

	data := obj{"body": body}
	stampActiveCommsSession(rt, data)
	now := time.Now().UTC()
	if err := rt.Append(event.Event{TS: now, ID: event.NewID(now), Actor: rt.Actor,
		Type: event.TypeNote, Data: data}); err != nil {
		return nil, &rpcError{Code: -32603, Message: err.Error()}
	}
	return text("noted", false), nil
}

func mcpFind(actorName, category, summary, ref string) (interface{}, *rpcError) {
	if _, ok := findCategories[category]; !ok {
		return text("category must be one of bug, fix, ship, decision, gotcha", true), nil
	}
	if err := rejectControlText("finding summary", summary, 280); err != nil {
		return text(err.Error(), true), nil
	}
	rt, rerr := openMCP(actorName, true)
	if rerr != nil {
		return nil, rerr
	}
	defer func() { _ = rt.Close() }()

	var refs []kindValue
	if ref != "" {
		parsed, err := parseRefs([]string{ref})
		if err != nil {
			return text(err.Error(), true), nil
		}
		refs = parsed
	}
	// Same anchoring rule as the CLI: an unanchored finding is never read again.
	if !hasPathRef(refs) {
		for _, c := range rt.State.ActiveClaimsByActor(rt.Actor) {
			refs = append(refs, kindValue{kind: "path", value: c.Scope.Path})
		}
	}
	refsForJSON := make([]map[string]string, len(refs))
	for i, r := range refs {
		refsForJSON[i] = map[string]string{"kind": r.kind, "value": r.value}
	}
	data := obj{"category": category, "summary": summary, "refs": refsForJSON}
	stampActiveCommsSession(rt, data)
	now := time.Now().UTC()
	if err := rt.Append(event.Event{TS: now, ID: event.NewID(now), Actor: rt.Actor,
		Type: event.TypeFinding, Data: data}); err != nil {
		return nil, &rpcError{Code: -32603, Message: err.Error()}
	}
	return text(fmt.Sprintf("recorded [%s] %s", category, summary), false), nil
}
