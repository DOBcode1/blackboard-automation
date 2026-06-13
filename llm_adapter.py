"""
Provider-agnostic wrapper for all LLM calls.
Reads ANTHROPIC_API_KEY from the environment; never hardcodes credentials.
"""

import os
import time
import anthropic
from logging_setup import get_logger

from dataclasses import dataclass, field
from typing import Generator

# ---------------------------------------------------------------------------
# Model constants — strings must match what is used elsewhere in this codebase
# ---------------------------------------------------------------------------
MODEL_FAST = "claude-haiku-4-5-20251001"   # fast/cheap tier (course router)
MODEL_MAIN = "claude-sonnet-4-6"            # main tier (query.py)

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384

# ---------------------------------------------------------------------------
# Structured logger
# ---------------------------------------------------------------------------
logger = get_logger("llm_adapter")

# ---------------------------------------------------------------------------
# Pricing table — (input_$/M, output_$/M)
# ---------------------------------------------------------------------------
_PRICING: dict[str, tuple[float, float]] = {
    MODEL_FAST: (1.0, 5.0),
    MODEL_MAIN: (3.0, 15.0),
}


def _compute_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    if model not in _PRICING:
        logger.warning("no pricing entry for model=%s — cost unknown", model)
        return None
    in_rate, out_rate = _PRICING[model]
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


def _log_call(model: str, usage: dict, duration_ms: float) -> None:
    try:
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        cost = _compute_cost(model, in_tok, out_tok)
        cost_str = f"${cost:.6f}" if cost is not None else "unknown"
        logger.info("model=%s in=%d out=%d cost=%s dur=%dms", model, in_tok, out_tok, cost_str, round(duration_ms))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Lazy client — created on first use so importing this module never requires
# ANTHROPIC_API_KEY to be set in the environment.
# ---------------------------------------------------------------------------
_client: anthropic.Anthropic | None = None
_embedding_model = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def _get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        from fastembed import TextEmbedding
        _embedding_model = TextEmbedding(model_name=EMBEDDING_MODEL)
    return _embedding_model

# ---------------------------------------------------------------------------
# Result type for non-streaming calls
# ---------------------------------------------------------------------------
@dataclass
class LLMResult:
    text: str
    usage: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
_RETRYABLE = (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError)
_MAX_RETRIES = 3


def _build_kwargs(model: str, messages: list, system: str | None, max_tokens: int) -> dict:
    """Assemble the kwargs dict for a messages.create call."""
    kwargs = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if system is not None:
        kwargs["system"] = system
    return kwargs


def _with_retry(fn, *args, **kwargs):
    """Call fn with exponential backoff on transient errors; re-raise auth errors immediately."""
    delay = 1.0
    for attempt in range(_MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except anthropic.AuthenticationError:
            raise
        except _RETRYABLE as exc:
            if attempt == _MAX_RETRIES - 1:
                raise
            time.sleep(delay)
            delay *= 2
    # unreachable, but keeps type checkers happy
    raise RuntimeError("Retry loop exited unexpectedly")


def _extract_usage(response) -> dict:
    """Pull token counts from a response object if available."""
    try:
        return {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
    except AttributeError:
        return {}


def _collect_text(response) -> str:
    """Concatenate all text content blocks from a response."""
    return "".join(block.text for block in response.content if hasattr(block, "text"))


def _stream_chunks(model: str, messages: list, system: str | None, max_tokens: int) -> Generator[str, None, None]:
    """Yield text chunks from a streaming messages call; capture usage if available."""
    kwargs = _build_kwargs(model, messages, system, max_tokens)
    t0 = time.perf_counter()
    with _with_retry(_get_client().messages.stream, **kwargs) as stream:
        for text in stream.text_stream:
            yield text
        dur = (time.perf_counter() - t0) * 1000
        try:
            final = stream.get_final_message()
            _log_call(model, _extract_usage(final), dur)
        except Exception as exc:
            logger.warning("get_final_message failed — stream usage not captured: %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def call_fast(
    messages: list,
    system: str | None = None,
    max_tokens: int = 1024,
    stream: bool = False,
) -> LLMResult | Generator[str, None, None]:
    """Route to the fast (Haiku) model. Returns LLMResult or a text-chunk generator."""
    if stream:
        return _stream_chunks(MODEL_FAST, messages, system, max_tokens)
    kwargs = _build_kwargs(MODEL_FAST, messages, system, max_tokens)
    t0 = time.perf_counter()
    response = _with_retry(_get_client().messages.create, **kwargs)
    dur = (time.perf_counter() - t0) * 1000
    result = LLMResult(text=_collect_text(response), usage=_extract_usage(response))
    _log_call(MODEL_FAST, result.usage, dur)
    return result


def call_main(
    messages: list,
    system: str | None = None,
    max_tokens: int = 4096,
    stream: bool = False,
) -> LLMResult | Generator[str, None, None]:
    """Route to the main (Sonnet) model. Returns LLMResult or a text-chunk generator."""
    if stream:
        return _stream_chunks(MODEL_MAIN, messages, system, max_tokens)
    kwargs = _build_kwargs(MODEL_MAIN, messages, system, max_tokens)
    t0 = time.perf_counter()
    response = _with_retry(_get_client().messages.create, **kwargs)
    dur = (time.perf_counter() - t0) * 1000
    result = LLMResult(text=_collect_text(response), usage=_extract_usage(response))
    _log_call(MODEL_MAIN, result.usage, dur)
    return result


def call_vision(
    messages: list,
    system: str | None = None,
    max_tokens: int = 4096,
) -> LLMResult:
    """Route to the main (Sonnet) model; messages may contain image content blocks."""
    kwargs = _build_kwargs(MODEL_MAIN, messages, system, max_tokens)
    t0 = time.perf_counter()
    response = _with_retry(_get_client().messages.create, **kwargs)
    dur = (time.perf_counter() - t0) * 1000
    result = LLMResult(text=_collect_text(response), usage=_extract_usage(response))
    _log_call(MODEL_MAIN, result.usage, dur)
    return result


def embed(texts: list[str]) -> list[list[float]]:
    """Return one embedding vector per input string.

    Uses BAAI/bge-small-en-v1.5 via fastembed — runs locally with no external API call.
    """
    if not texts:
        return []
    model = _get_embedding_model()
    return [vec.tolist() for vec in model.embed(texts)]
