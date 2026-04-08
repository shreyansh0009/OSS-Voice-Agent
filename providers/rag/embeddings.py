"""
Embeddings providers for RAG.

Two options:
  1. OllamaEmbeddings  — uses the same Ollama server already running for LLM
                         Model: nomic-embed-text (768-dim, fast, good quality)
  2. SentenceTransformerEmbeddings — runs locally, no Ollama needed
                         Model: all-MiniLM-L6-v2 (384-dim, very fast)

Env vars:
  EMBEDDING_PROVIDER   - "ollama" | "sentence_transformers"
  OLLAMA_EMBED_MODEL   - default: nomic-embed-text
  EMBED_MODEL_NAME     - default: all-MiniLM-L6-v2 (for sentence_transformers)
"""
from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from functools import partial

import httpx

logger = logging.getLogger(__name__)


class BaseEmbeddings(ABC):
    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts. Returns list of float vectors."""
        ...

    async def embed_one(self, text: str) -> list[float]:
        results = await self.embed([text])
        return results[0]


class OllamaEmbeddings(BaseEmbeddings):
    """
    Uses Ollama's /api/embed endpoint.
    Pull the model first: ollama pull nomic-embed-text
    """
    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "nomic-embed-text",
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        url = f"{self.base_url}/api/embed"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json={"model": self.model, "input": texts})
            response.raise_for_status()
            data = response.json()
            return data["embeddings"]

    @classmethod
    def from_env(cls) -> "OllamaEmbeddings":
        return cls(
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            model=os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
        )


class SentenceTransformerEmbeddings(BaseEmbeddings):
    """
    Uses sentence-transformers (runs fully locally, no Ollama needed).
    Install: pip install sentence-transformers
    """
    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        from sentence_transformers import SentenceTransformer
        logger.info(f"Loading SentenceTransformer model: {model_name}")
        self._model = SentenceTransformer(model_name)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        loop = asyncio.get_event_loop()
        vectors = await loop.run_in_executor(
            None, partial(self._model.encode, texts, convert_to_numpy=True)
        )
        return [v.tolist() for v in vectors]

    @classmethod
    def from_env(cls) -> "SentenceTransformerEmbeddings":
        return cls(model_name=os.getenv("EMBED_MODEL_NAME", "all-MiniLM-L6-v2"))


def get_embeddings() -> BaseEmbeddings:
    provider = os.getenv("EMBEDDING_PROVIDER", "sentence_transformers")
    if provider == "ollama":
        return OllamaEmbeddings.from_env()
    return SentenceTransformerEmbeddings.from_env()
