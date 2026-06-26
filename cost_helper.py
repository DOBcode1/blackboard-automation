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
    path: Path = _DEFAULT_PATH,
) -> None:
    """
    Append one cost row to the JSONL ledger.

    Defensive: swallows all exceptions so a logging failure never raises into the caller.
    """
    try:
        row = {
            "event_id":      str(uuid.uuid4()),
            "request_id":    str(uuid.uuid4()),
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
