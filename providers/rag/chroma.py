"""
ChromaRetriever: RAG retrieval backed by ChromaDB (embedded, no server needed).

ChromaDB stores vectors + text chunks locally on disk.
Each agent gets its own collection (named by agent + knowledge base name).

Install: pip install chromadb

Env vars:
  CHROMA_PERSIST_DIR  - where ChromaDB stores data, default: ./chroma_db
"""
from __future__ import annotations

import asyncio
import logging
import os
from functools import partial

from providers.rag.base import BaseRetriever, RetrievedChunk
from providers.rag.embeddings import BaseEmbeddings

logger = logging.getLogger(__name__)


class ChromaRetriever(BaseRetriever):
    def __init__(
        self,
        collection_name: str,
        embeddings: BaseEmbeddings,
        persist_dir: str = "./chroma_db",
    ):
        import chromadb
        self._embeddings = embeddings
        self._collection_name = collection_name

        client = chromadb.PersistentClient(path=persist_dir)
        self._collection = client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(
            f"ChromaDB collection '{collection_name}' loaded "
            f"({self._collection.count()} chunks)"
        )

    async def retrieve(self, query: str, top_k: int = 3) -> list[RetrievedChunk]:
        if self._collection.count() == 0:
            logger.warning(f"Collection '{self._collection_name}' is empty — no RAG context")
            return []

        query_vector = await self._embeddings.embed_one(query)

        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None,
            partial(
                self._collection.query,
                query_embeddings=[query_vector],
                n_results=min(top_k, self._collection.count()),
                include=["documents", "metadatas", "distances"],
            ),
        )

        chunks = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            chunks.append(RetrievedChunk(
                text=doc,
                source=meta.get("source", "unknown"),
                score=1.0 - dist,   # cosine distance → similarity
            ))

        return chunks

    async def add_documents(self, chunks: list[dict]) -> None:
        """
        Add text chunks to the collection.
        chunks: [{"text": "...", "source": "filename.md", "id": "unique-id"}, ...]
        """
        texts = [c["text"] for c in chunks]
        vectors = await self._embeddings.embed(texts)

        self._collection.add(
            ids=[c["id"] for c in chunks],
            embeddings=vectors,
            documents=texts,
            metadatas=[{"source": c.get("source", "")} for c in chunks],
        )
        logger.info(f"Added {len(chunks)} chunks to '{self._collection_name}'")

    @classmethod
    def for_agent(
        cls,
        agent_name: str,
        kb_name: str,
        embeddings: BaseEmbeddings,
        persist_dir: str | None = None,
    ) -> "ChromaRetriever":
        """Convenience factory: creates a collection scoped to an agent + KB."""
        collection = f"{agent_name}_{kb_name}".replace("-", "_").replace(" ", "_")
        return cls(
            collection_name=collection,
            embeddings=embeddings,
            persist_dir=persist_dir or os.getenv("CHROMA_PERSIST_DIR", "./chroma_db"),
        )
