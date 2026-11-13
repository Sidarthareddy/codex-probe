"""Extract token usage from a logged response body.

Backends report how many tokens a call used. The two wire formats use
different field names, so this module converts both into one shape:

    {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"}

  * input tokens     = everything sent to the model (instructions, history, tools)
  * cached input     = the part of the input the backend had seen before and
                       could reuse (billed at a lower rate). It is a subset
                       of input tokens, not an extra amount.
  * output tokens    = what the model generated
  * reasoning tokens = hidden "thinking" tokens, a subset of output tokens

This only reads the logged body. It never estimates: if the backend sent no
usage, the result is None.
"""

from __future__ import annotations

from typing import Optional


def extract_usage(response_body) -> Optional[dict]:
    if not isinstance(response_body, dict):
        return None
    usage = response_body.get("usage")
    if not isinstance(usage, dict):
        return None

    if "input_tokens" in usage or "output_tokens" in usage:
        # Responses API field names.
        input_details = usage.get("input_tokens_details") or {}
        output_details = usage.get("output_tokens_details") or {}
        return {
            "input_tokens": usage.get("input_tokens", 0),
            "cached_input_tokens": input_details.get("cached_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "reasoning_tokens": output_details.get("reasoning_tokens", 0),
        }

    if "prompt_tokens" in usage or "completion_tokens" in usage:
        # Chat Completions field names.
        input_details = usage.get("prompt_tokens_details") or {}
        output_details = usage.get("completion_tokens_details") or {}
        return {
            "input_tokens": usage.get("prompt_tokens", 0),
            "cached_input_tokens": input_details.get("cached_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "reasoning_tokens": output_details.get("reasoning_tokens", 0),
        }

    return None
