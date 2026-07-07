"""RAG backend: the stable boundary the RAG tools sit behind.

The harness is designed as an MCP host — the faithful shape is to spawn the
RAG server as an MCP stdio child and call its 7 tools over the wire. For this
pass we use the in-process backend: import ``obsidian_rag``'s tool handlers and
drive them directly, building the FAISS index at startup exactly as the RAG
server's lifespan does (``build_index``), then calling ``search`` /
``note_context`` with a lightweight shim context.

Both backends implement the same ``RagBackend`` protocol, so swapping to the
MCP stdio child is a config change (which backend to construct), not a rewrite
of the tools. The RAG handlers return ``dict`` and never raise — failures come
back as ``{"error", ..., "suggestion"}`` (CONTRACTS.md §2). The vault is
read-only; we never call ``reindex`` from the voice path.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("sonar.rag")


class RagBackend(Protocol):
    """The two RAG tools the harness wires in this pass (CONTRACTS.md §2)."""

    def search(
        self,
        query: str,
        vault_name: str | None = None,
        tags: list[str] | None = None,
        folder: str | None = None,
    ) -> dict[str, Any]: ...

    def note_context(
        self, path: str, vault_name: str | None = None
    ) -> dict[str, Any]: ...


class _ShimCtx:
    """Stand-in for FastMCP's ``Context``.

    The RAG handlers only ever touch ``ctx.lifespan_context``; a shim exposing
    that one attribute lets us call them in-process without a FastMCP server.
    Swapping to the MCP stdio child replaces this whole class with a client
    call — the tools above the backend don't change.
    """

    def __init__(self, lifespan_context: dict[str, Any]) -> None:
        self.lifespan_context = lifespan_context


class InProcessRagBackend:
    """Builds a vault index once and calls the RAG handlers directly."""

    def __init__(self, vault_indexes: dict, config: Any) -> None:
        self._lifespan = {
            "vault_indexes": vault_indexes,
            "config": config,
            "index_lock": threading.Lock(),
        }
        # Imported lazily so importing the harness never hard-requires the RAG
        # stack; resolved once here at construction.
        from obsidian_rag import tools as _rag_tools

        self._rag_tools = _rag_tools

    @classmethod
    def build(
        cls,
        vault_path: Path | str,
        vault_name: str = "sonar",
        ollama_url: str = "http://127.0.0.1:11434",
        embedding_model: str = "nomic-embed-text",
    ) -> "InProcessRagBackend":
        """Index ``vault_path`` (blocking) and return a ready backend.

        Mirrors the RAG server's lifespan: build the FAISS index, then hold the
        in-memory ``vault_indexes`` map the handlers read. Embeddings run
        through Ollama, so this raises if Ollama is unreachable — the caller
        starts ``ollama serve`` first.
        """
        from obsidian_rag.indexer import build_index
        from obsidian_rag.models import (
            AppConfig,
            EmbeddingConfig,
            VaultConfig,
        )

        vault_cfg = VaultConfig(name=vault_name, path=str(vault_path))
        config = AppConfig(
            vaults=[vault_cfg],
            embedding=EmbeddingConfig(model=embedding_model, ollama_url=ollama_url),
        )
        # watch_enabled defaults True in RAG config, but we never start a
        # watcher here — the index is a static snapshot for the spike.
        log.info("indexing vault %r at %s", vault_name, vault_path)
        index, metadata, file_hashes = build_index(config, vault_cfg)
        chunk_count = index.ntotal if index is not None else 0
        log.info("vault %r indexed: %d chunks", vault_name, chunk_count)
        vault_indexes = {
            vault_name: {
                "index": index,
                "metadata": metadata,
                "file_hashes": file_hashes,
                "vault_config": vault_cfg,
            }
        }
        return cls(vault_indexes, config)

    @property
    def chunk_count(self) -> int:
        total = 0
        for v in self._lifespan["vault_indexes"].values():
            idx = v.get("index")
            total += idx.ntotal if idx is not None else 0
        return total

    def search(
        self,
        query: str,
        vault_name: str | None = None,
        tags: list[str] | None = None,
        folder: str | None = None,
    ) -> dict[str, Any]:
        ctx = _ShimCtx(self._lifespan)
        return self._rag_tools.search(
            query, vault_name=vault_name, tags=tags, folder=folder, ctx=ctx
        )

    def note_context(
        self, path: str, vault_name: str | None = None
    ) -> dict[str, Any]:
        ctx = _ShimCtx(self._lifespan)
        return self._rag_tools.note_context(path, vault_name=vault_name, ctx=ctx)
