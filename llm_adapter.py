"""
Provider-agnostic wrapper for all LLM calls.
Reads ANTHROPIC_API_KEY from the environment; never hardcodes credentials.
"""

import os
import time
import anthropic

from dataclasses import dataclass, field
from typing import Generator

# ---------------------------------------------------------------------------
# Model constants — strings must match what is used elsewhere in this codebase
# ---------------------------------------------------------------------------
MODEL_FAST = "claude-haiku-4-5-20251001"   # fast/cheap tier (course router)
MODEL_MAIN = "claude-sonnet-4-6"            # main tier (query.py)

# ---------------------------------------------------------------------------
# Module-level client (created once; fails fast if key is absent)
# ---------------------------------------------------------------------------
_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

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
    with _with_retry(_client.messages.stream, **kwargs) as stream:
        for text in stream.text_stream:
            yield text
        # Usage is captured after the stream closes; ignore failures silently
        try:
            _ = stream.get_final_message()
        except Exception:
            pass


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
    response = _with_retry(_client.messages.create, **kwargs)
    return LLMResult(text=_collect_text(response), usage=_extract_usage(response))


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
    response = _with_retry(_client.messages.create, **kwargs)
    return LLMResult(text=_collect_text(response), usage=_extract_usage(response))


def call_vision(
    messages: list,
    system: str | None = None,
    max_tokens: int = 4096,
) -> LLMResult:
    """Route to the main (Sonnet) model; messages may contain image content blocks."""
    kwargs = _build_kwargs(MODEL_MAIN, messages, system, max_tokens)
    response = _with_retry(_client.messages.create, **kwargs)
    return LLMResult(text=_collect_text(response), usage=_extract_usage(response))


def embed(texts: list[str]) -> list[list[float]]:
    """Embedding backend is pending — not yet implemented."""
    raise NotImplementedError("embed() is not yet implemented; embedding backend is pending")
