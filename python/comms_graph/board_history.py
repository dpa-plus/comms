"""Paged, human-readable history; the event log remains the source of truth."""

from .log import WRITTEN_TYPES


def text(data, key):
    value = data.get(key) if isinstance(data, dict) else None
    return "" if value is None or isinstance(value, (dict, list)) else str(value)


def history_page(events, *, actor="", before="", limit=60):
    claims = {e.id: e for e in events if e.type == "claim"}
    end = next((i for i, e in enumerate(events) if e.id == before), None) if before else len(events)
    if end is None:
        raise ValueError("The history position is no longer available. Reload history.")
    rows = []
    for ev in events[:end]:
        if ev.type not in WRITTEN_TYPES:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        refs = data.get("refs", [])
        if isinstance(refs, str):
            refs = [refs]
        referenced = [claims[r] for r in refs if isinstance(r, str) and r in claims] if isinstance(refs, (list, tuple)) else []
        owners = list(dict.fromkeys(c.actor for c in referenced))
        original_actor = text(data, "original_actor") or text(data, "freed_from") or (owners[0] if owners else "")
        if actor and actor != ev.actor and actor not in owners and actor != original_actor:
            continue
        scopes = list(ev.scope or [])
        if ev.type == "release":
            scopes = list(dict.fromkeys(s for c in referenced for s in (c.scope or [])))
        row = {"id": ev.id, "type": ev.type, "actor": ev.actor, "original_actor": original_actor,
               "original_actors": list(dict.fromkeys(filter(None, owners + [original_actor]))),
               "scopes": scopes, "scope": scopes[0] if scopes else "",
               "ts": ev.ts.isoformat().replace("+00:00", "Z"),
               **{key: text(data, key) for key in ("intent", "reason", "result", "steals")},
               "body": text(data, "body")}
        previous = rows[-1] if rows else None
        # A multi-file claim is one visible action.
        if (previous and ev.type == previous["type"] == "claim" and ev.actor == previous["actor"]
                and row["intent"] == previous["intent"]
                and not row["steals"] and not previous["steals"]
                and abs(ev.ts.timestamp() - previous["_ts"]) <= 1):
            previous["scopes"].extend(scopes)
        else:
            row["_ts"] = ev.ts.timestamp()
            rows.append(row)
    limit = max(1, min(100, limit))
    page = list(reversed(rows[-limit:]))
    more = len(rows) > limit
    for row in page:
        row.pop("_ts", None)
    return {"events": page, "next_before": page[-1]["id"] if more and page else ""}
