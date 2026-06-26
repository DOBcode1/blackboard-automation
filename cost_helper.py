"""
cost_helper.py — append-only JSONL cost ledger for LLM calls.

One row per LLM call, written via true file append (never read-modify-write).
Mirrors the pattern of chat_history_helper.py / overrides_helper.py.

Ledger lives at output/cost_ledger.jsonl (output/ is gitignored).
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_PATH = Path("output/cost_ledger.jsonl")

# ---------------------------------------------------------------------------
# Pricing table — per MILLION tokens (input, output)
# ---------------------------------------------------------------------------
PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.00,  5.00),
    "claude-sonnet-4-6":         (3.00, 15.00),
}


def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return cost in USD for a call. Returns 0.0 if model not in pricing table."""
    if model not in PRICING:
        return 0.0
    in_rate, out_rate = PRICING[model]
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


# ---------------------------------------------------------------------------
# Ledger I/O
# ---------------------------------------------------------------------------

def log_call(
    *,
    model: str,
    tier: str,
    input_tokens: int,
    output_tokens: int,
    operation: str | None = None,
    thread_id: str | None = None,
    request_id: str | None = None,
    path: Path = _DEFAULT_PATH,
) -> None:
    """
    Append one cost row to the JSONL ledger.

    Defensive: swallows all exceptions so a logging failure never raises into the caller.
    """
    try:
        row = {
            "event_id":      str(uuid.uuid4()),
            "request_id":    request_id if request_id is not None else str(uuid.uuid4()),
            "event_time":    datetime.now(timezone.utc).isoformat(),
            "user_id":       "local_dev",
            "school_id":     "fordham",
            "thread_id":     thread_id,
            "model":         model,
            "tier":          tier,
            "operation":     operation,
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
            "cost_usd":      compute_cost(model, input_tokens, output_tokens),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass


def read_all(path: Path = _DEFAULT_PATH) -> list[dict]:
    """
    Load all rows from the JSONL ledger. Returns [] if the file doesn't exist.
    Skips malformed lines rather than raising.
    """
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

def summarize_by_period(path: Path = _DEFAULT_PATH) -> dict:
    """
    Return total cost_usd + call_count for today (UTC), last 7 days, and all-time.
    Uses event_time field. Empty-safe.
    """
    from datetime import date, timedelta
    rows = read_all(path)
    today_utc = date.today()
    cutoff_7d = today_utc - timedelta(days=6)

    result = {
        "today":    {"cost_usd": 0.0, "call_count": 0},
        "last_7d":  {"cost_usd": 0.0, "call_count": 0},
        "all_time": {"cost_usd": 0.0, "call_count": 0},
    }
    for row in rows:
        cost = row.get("cost_usd") or 0.0
        result["all_time"]["cost_usd"]   += cost
        result["all_time"]["call_count"] += 1
        try:
            event_date = datetime.fromisoformat(row["event_time"]).date()
        except (KeyError, ValueError):
            continue
        if event_date == today_utc:
            result["today"]["cost_usd"]   += cost
            result["today"]["call_count"] += 1
        if event_date >= cutoff_7d:
            result["last_7d"]["cost_usd"]   += cost
            result["last_7d"]["call_count"] += 1
    return result


def summarize_by_operation(path: Path = _DEFAULT_PATH) -> list[dict]:
    """
    Group by operation (None -> "untagged").
    Per group: operation, call_count, total_input_tokens, total_output_tokens, total_cost_usd.
    Sorted by total_cost_usd desc. Empty-safe.
    """
    rows = read_all(path)
    groups: dict[str, dict] = {}
    for row in rows:
        key = row.get("operation") or "untagged"
        if key not in groups:
            groups[key] = {
                "operation":           key,
                "call_count":          0,
                "total_input_tokens":  0,
                "total_output_tokens": 0,
                "total_cost_usd":      0.0,
            }
        g = groups[key]
        g["call_count"]          += 1
        g["total_input_tokens"]  += row.get("input_tokens") or 0
        g["total_output_tokens"] += row.get("output_tokens") or 0
        g["total_cost_usd"]      += row.get("cost_usd") or 0.0
    return sorted(groups.values(), key=lambda g: g["total_cost_usd"], reverse=True)


def group_by_query(limit: int | None = None, path: Path = _DEFAULT_PATH) -> list[dict]:
    """
    Group by request_id (None -> "unknown").
    Per group: request_id, earliest event_time, operations (deduped list),
    total_input_tokens, total_output_tokens, total_cost_usd, call_count.
    Sorted by event_time desc. Limit applied after sorting. Empty-safe.
    """
    rows = read_all(path)
    groups: dict[str, dict] = {}
    for row in rows:
        key = row.get("request_id") or "unknown"
        if key not in groups:
            groups[key] = {
                "request_id":          key,
                "earliest_event_time": row.get("event_time", ""),
                "operations":          [],
                "total_input_tokens":  0,
                "total_output_tokens": 0,
                "total_cost_usd":      0.0,
                "call_count":          0,
            }
        g = groups[key]
        et = row.get("event_time", "")
        if et and et < g["earliest_event_time"]:
            g["earliest_event_time"] = et
        op = row.get("operation") or "untagged"
        if op not in g["operations"]:
            g["operations"].append(op)
        g["total_input_tokens"]  += row.get("input_tokens") or 0
        g["total_output_tokens"] += row.get("output_tokens") or 0
        g["total_cost_usd"]      += row.get("cost_usd") or 0.0
        g["call_count"]          += 1
    result = sorted(groups.values(), key=lambda g: g["earliest_event_time"], reverse=True)
    if limit is not None:
        result = result[:limit]
    return result


def most_expensive_calls(limit: int = 10, path: Path = _DEFAULT_PATH) -> list[dict]:
    """
    Top-N raw rows by cost_usd desc. Empty-safe.
    """
    rows = read_all(path)
    return sorted(rows, key=lambda r: r.get("cost_usd") or 0.0, reverse=True)[:limit]
