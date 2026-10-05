"""LLM client factory: OpenRouter (default) or OpenAI, both through the OpenAI SDK."""
from __future__ import annotations

from openai import OpenAI

from .config import Settings, llm_api_key

OPENROUTER_URL = "https://openrouter.ai/api/v1"


def make_client(s: Settings) -> OpenAI:
    key = llm_api_key(s)
    if s.provider == "openai":
        return OpenAI(api_key=key, timeout=180)
    return OpenAI(base_url=OPENROUTER_URL, api_key=key, timeout=180)


def extra_body(s: Settings) -> dict | None:
    """OpenRouter reports the real cost of a call when asked to."""
    return {"usage": {"include": True}} if s.provider == "openrouter" else None
