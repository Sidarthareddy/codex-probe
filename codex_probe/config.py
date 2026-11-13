"""Configuration for CodexProbe.

The config says where the proxy listens, which real backend it forwards to,
and where logs go. Swapping backends (OpenAI vs local Qwen) is done by
changing the "backend" block only. No code changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Optional, Union

from pydantic import BaseModel


class BackendConfig(BaseModel):
    """The real LLM server that the proxy forwards requests to."""

    # A label that shows up in the logs, e.g. "openai" or "ollama_qwen".
    name: str
    # Root of the backend's OpenAI compatible API, e.g. "https://api.openai.com/v1".
    base_url: str
    # Which API format the backend speaks:
    #   "responses" = the Responses API (POST /v1/responses), what Codex sends
    #   "chat"      = the Chat Completions API (POST /v1/chat/completions)
    wire_api: Literal["responses", "chat"] = "responses"
    # Name of the environment variable that holds the API key for this backend.
    # null means the backend needs no key (a local Ollama server, for example).
    api_key_env: Optional[str] = None


class ProbeConfig(BaseModel):
    """Top level config for one ProxyRecorder."""

    listen_host: str = "127.0.0.1"
    # Port 0 means "let the operating system pick a free port" (used by tests).
    listen_port: int = 8135
    backend: BackendConfig
    log_dir: str = "logs"
    pricing_file: str = "pricing.json"
    seed: int = 0


def load_config(config: Union[ProbeConfig, dict, str, Path]) -> ProbeConfig:
    """Accept a ProbeConfig, a plain dict, or a path to a JSON file.

    Pydantic validates the shape, so a typo in the config fails loudly here
    instead of causing a confusing error later.
    """
    if isinstance(config, ProbeConfig):
        return config
    if isinstance(config, (str, Path)):
        config = json.loads(Path(config).read_text())
    return ProbeConfig.model_validate(config)
