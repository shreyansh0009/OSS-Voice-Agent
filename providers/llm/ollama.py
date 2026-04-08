"""
OllamaLLM: calls a locally-hosted Ollama server.

Setup:
  # On EC2 or local:
  curl https://ollama.ai/install.sh | sh
  ollama pull llama3.2        # or mistral, qwen2.5, etc.
  ollama serve

Env vars:
  OLLAMA_BASE_URL  - default: http://localhost:11434
  OLLAMA_MODEL     - default: llama3.2
  OLLAMA_TIMEOUT   - default: 30 (seconds)
"""
from __future__ import annotations

import logging

import httpx

from providers.llm.base import BaseLLM

logger = logging.getLogger(__name__)


class OllamaLLM(BaseLLM):
    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "llama3.2",
        timeout: int = 30,
        temperature: float = 0.7,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature

    async def chat(self, messages: list[dict]) -> str:
        url = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.temperature,
            },
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            return data["message"]["content"]

    @classmethod
    def from_env(cls) -> "OllamaLLM":
        import os
        return cls(
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            model=os.getenv("OLLAMA_MODEL", "llama3.2"),
            timeout=int(os.getenv("OLLAMA_TIMEOUT", "30")),
        )
