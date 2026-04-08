"""
OpenAILLM: streaming chat via the OpenAI API.

Compatible models (as of 2025):
  gpt-4o-mini   — fastest + cheapest, ~200-400ms first token  (recommended for voice)
  gpt-4o        — best quality,        ~300-600ms first token
  gpt-4.1-mini  — latest mini,         ~200-400ms first token
  gpt-4.1       — latest flagship,     ~300-700ms first token

Env vars:
  OPENAI_API_KEY    - required
  OPENAI_MODEL      - default: gpt-4o-mini
  OPENAI_TIMEOUT    - default: 15
  LLM_TEMPERATURE   - default: 0.7
  OPENAI_MAX_TOKENS - default: 300
"""
from __future__ import annotations

import json
import logging
import os
from typing import AsyncIterator

import httpx

from providers.llm.base import BaseLLM

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.openai.com/v1"


class OpenAILLM(BaseLLM):
    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        temperature: float = 0.7,
        timeout: int = 15,
        max_tokens: int = 300,
    ):
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _get_max_tokens(self, messages: list[dict]) -> int:
        """Use more tokens for non-English (Indian scripts are ~2x token-heavy)."""
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        if "LANGUAGE LOCK" in system and "English" not in system.split("CURRENT LANGUAGE:")[-1][:30]:
            return min(self.max_tokens * 2, 400)
        return self.max_tokens

    async def chat(self, messages: list[dict]) -> str:
        """Non-streaming chat."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{_BASE_URL}/chat/completions",
                headers=self._headers,
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": self.temperature,
                    "max_tokens": self._get_max_tokens(messages),
                    "stream": False,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    async def stream_chat(self, messages: list[dict]) -> AsyncIterator[str]:
        """True streaming — yields tokens as they arrive."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                f"{_BASE_URL}/chat/completions",
                headers=self._headers,
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": self.temperature,
                    "max_tokens": self._get_max_tokens(messages),
                    "stream": True,
                },
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0]["delta"].get("content", "")
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError):
                        continue

    @classmethod
    def from_env(cls) -> "OpenAILLM":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable is required")
        return cls(
            api_key=api_key,
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.7")),
            timeout=int(os.getenv("OPENAI_TIMEOUT", "15")),
            max_tokens=int(os.getenv("OPENAI_MAX_TOKENS", "300")),
        )
