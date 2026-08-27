package subcmd

// Two helpers that outlived the Go dashboard.
//
// They were written for it and are used by `claim`, `status` and `log`, which
// are staying. Kept here rather than left in ui.go, so that file is only ever
// the launcher and nobody has to wonder what else is hiding in it.

import (
	"fmt"
	"time"

	"github.com/dpa-plus/comms/internal/event"
)

func shortAge(d time.Duration) string {
	if d < time.Minute {
		return "now"
	}
	if d < time.Hour {
		return fmt.Sprintf("%dm", int(d.Minutes()))
	}
	if d < 24*time.Hour {
		return fmt.Sprintf("%dh", int(d.Hours()))
	}
	return fmt.Sprintf("%dd", int(d.Hours()/24))
}

func eventSummary(ev event.Event) string {
	switch ev.Type {
	case event.TypeHello:
		if dataBool(ev.Data, "comms_session_start") {
			if s := dataString(ev.Data, "comms_session_name"); s != "" {
				return "started comms session: " + s
			}
			if s := reasonOf(ev); s != "" {
				return "started comms session: " + s
			}
			return "started comms session"
		}
		if dataBool(ev.Data, "comms_session_join") {
			if s := dataString(ev.Data, "comms_session_name"); s != "" {
				return "joined comms session: " + s
			}
			return "joined comms session"
		}
	case event.TypeClaim:
		if s, _ := ev.Data["intent"].(string); s != "" {
			return s
		}
	case event.TypeRelease:
		if dataBool(ev.Data, "comms_session_end") {
			count := len(dataStringList(ev.Data, "refs"))
			return fmt.Sprintf("ended comms session; released %d claim%s", count, pluralS(count))
		}
		if dataBool(ev.Data, "session_retire") {
			target, _ := ev.Data["retired_actor"].(string)
			count := len(dataStringList(ev.Data, "refs"))
			return fmt.Sprintf("retired @%s from active sessions; released %d claim%s", target, count, pluralS(count))
		}
		if dataBool(ev.Data, "leader_transfer") {
			target, _ := ev.Data["leader_actor"].(string)
			return fmt.Sprintf("@%s became comms leader", target)
		}
		if s, _ := ev.Data["result"].(string); s != "" {
			return s
		}
		if s, _ := ev.Data["reason"].(string); s != "" {
			return s
		}
	case event.TypeFinding:
		cat, _ := ev.Data["category"].(string)
		sum, _ := ev.Data["summary"].(string)
		if dataBool(ev.Data, "priority") {
			sum = "PRIORITY: " + sum
		}
		if cat != "" {
			return cat + ": " + sum
		}
		return sum
	case event.TypeNote:
		if s, _ := ev.Data["body"].(string); s != "" {
			if dataBool(ev.Data, "priority") {
				return "PRIORITY: " + s
			}
			return s
		}
	}
	return ""
}

func dataBool(m map[string]interface{}, key string) bool {
	if v, ok := m[key]; ok {
		if b, ok := v.(bool); ok {
			return b
		}
	}
	return false
}

func dataString(m map[string]interface{}, key string) string {
	if v, ok := m[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

func dataStringList(m map[string]interface{}, key string) []string {
	if m == nil {
		return nil
	}
	v, ok := m[key]
	if !ok {
		return nil
	}
	if s, ok := v.(string); ok {
		return []string{s}
	}
	if arr, ok := v.([]string); ok {
		return append([]string(nil), arr...)
	}
	arr, ok := v.([]interface{})
	if !ok {
		return nil
	}
	out := make([]string, 0, len(arr))
	for _, x := range arr {
		if s, ok := x.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

func reasonOf(ev event.Event) string {
	if s, _ := ev.Data["reason"].(string); s != "" {
		return s
	}
	if s, _ := ev.Data["result"].(string); s != "" {
		return s
	}
	return ""
}
