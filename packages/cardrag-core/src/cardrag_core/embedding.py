"""Shared embedding input contract for Worker producers and MCP queries."""

from __future__ import annotations

from typing import Final

EMBEDDING_DIMENSION: Final = 1536
EMBEDDING_POLICY_VERSION: Final = "cardrag.embedding-input.v1"
DOCUMENT_EMBEDDING_PREFIX: Final = "Represent this Korean card-disclosure evidence for retrieval: "
QUERY_EMBEDDING_PREFIX: Final = "Retrieve Korean card-disclosure evidence that answers: "

__all__ = [
    "DOCUMENT_EMBEDDING_PREFIX",
    "EMBEDDING_DIMENSION",
    "EMBEDDING_POLICY_VERSION",
    "QUERY_EMBEDDING_PREFIX",
]
