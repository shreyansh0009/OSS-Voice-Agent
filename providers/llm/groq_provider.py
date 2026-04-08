"""
GroqLLM: ultra-fast hosted inference via Groq.

Groq's LPU hardware delivers 300–500 tokens/sec — 10–20x faster than CPU Ollama.
Uses the OpenAI-compatible REST API (no extra SDK needed beyond httpx).

Recommended models (by use case):
  llama-3.3-70b-versatile  — best quality,  ~280 tok/s
  llama-3.1-8b-instant     — fastest,       ~750 tok/s  (use for simple agents)
  mixtral-8x7b-32768       — long context,  ~500 tok/s

Env vars:
  GROQ_API_KEY    - required
  GROQ_MODEL      - default: llama-3.3-70b-versatile
  GROQ_TIMEOUT    - default: 15
  LLM_TEMPERATURE - default: 0.7
"""
from __future__ import annotations

import json
import logging
import os
from typing import AsyncIterator

import httpx

from providers.llm.base import BaseLLM

logger = logging.getLogger(__name__)

GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class GroqLLM(BaseLLM):
    def __init__(
        self,
        api_key: str,
        model: str = "llama-3.3-70b-versatile",
        temperature: float = 0.7,
        timeout: int = 15,
        max_tokens: int = 400,  # base for English; 700 for Hindi/non-English
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
        """
        Hindi/Indian scripts use ~2x tokens per word vs English.
        Use 700 for non-English to avoid truncation without burning free-tier quota.
        English stays at base (400).
        """
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        if "LANGUAGE LOCK" in system and "English" not in system.split("CURRENT LANGUAGE:")[-1][:30]:
            return min(self.max_tokens * 2, 400)  # non-English: 2x base, capped at 400
        return self.max_tokens  # English: base

    async def chat(self, messages: list[dict]) -> str:
        """Non-streaming chat with automatic retry on 429 rate-limit."""
        import asyncio
        max_retries = 4
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(
                        f"{GROQ_BASE_URL}/chat/completions",
                        headers=self._headers,
                        json={
                            "model": self.model,
                            "messages": messages,
                            "temperature": self.temperature,
                            "max_tokens": self._get_max_tokens(messages),
                            "stream": False,
                        },
                    )
                    if resp.status_code == 429:
                        # Read retry-after header if present, else use backoff
                        retry_after = float(resp.headers.get("retry-after", 2 ** attempt))
                        retry_after = min(retry_after, 10)  # cap at 10s
                        logger.warning(f"Groq 429 rate limit — waiting {retry_after:.1f}s (attempt {attempt+1}/{max_retries})")
                        await asyncio.sleep(retry_after)
                        continue
                    resp.raise_for_status()
                    return resp.json()["choices"][0]["message"]["content"]
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < max_retries - 1:
                    wait = 2 ** attempt
                    logger.warning(f"Groq 429 (exception) — retrying in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                raise
        raise RuntimeError(f"Groq rate limit: failed after {max_retries} attempts")

    async def stream_chat(self, messages: list[dict]) -> AsyncIterator[str]:
        """
        True streaming — yields text tokens as they arrive from Groq.
        Falls back to non-streaming on 429 (retries handled in chat()).
        """
        import asyncio
        max_retries = 3
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    async with client.stream(
                        "POST",
                        f"{GROQ_BASE_URL}/chat/completions",
                        headers=self._headers,
                        json={
                            "model": self.model,
                            "messages": messages,
                            "temperature": self.temperature,
                            "max_tokens": self._get_max_tokens(messages),
                            "stream": True,
                        },
                    ) as resp:
                        if resp.status_code == 429:
                            wait = min(2 ** attempt, 10)
                            logger.warning(f"Groq 429 on stream — waiting {wait}s")
                            await asyncio.sleep(wait)
                            continue
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
                        return  # completed successfully
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise

    @classmethod
    def from_env(cls) -> "GroqLLM":
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY environment variable is required")
        return cls(
            api_key=api_key,
            model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.7")),
            timeout=int(os.getenv("GROQ_TIMEOUT", "15")),
            max_tokens=int(os.getenv("GROQ_MAX_TOKENS", "500")),
        )