"""Shared embedding input contract for Worker producers and MCP queries."""

from __future__ import annotations

from typing import Final, Literal

from .canonical import canonical_sha256

EMBEDDING_DIMENSION: Final = 1536
EMBEDDING_POLICY_VERSION: Final = "cardrag.embedding-input.v1"
DOCUMENT_EMBEDDING_PREFIX: Final = "Represent this Korean card-disclosure evidence for retrieval: "
QUERY_EMBEDDING_PREFIX: Final = "Retrieve Korean card-disclosure evidence that answers: "

# v5 deliberately lives beside the immutable v1-v4 input contract above.  The
# old constants remain the compatibility contract for 1,536D generations.
QWEN3_EMBEDDING_PROVIDER: Final = "openrouter"
QWEN3_EMBEDDING_PROVIDER_IDS: Final = ("deepinfra", "nebius")
QWEN3_EMBEDDING_PROVIDER_FALLBACK_POLICY: Final = "forbidden"
QWEN3_EMBEDDING_MODEL: Final = "qwen/qwen3-embedding-8b"
QWEN3_EMBEDDING_DIMENSION: Final = 4096
QWEN3_EMBEDDING_DTYPE: Final = "float32"
QWEN3_EMBEDDING_NORMALIZATION: Final = "l2"
QWEN3_DOCUMENT_INSTRUCTION: Final[None] = None
QWEN3_QUERY_POLICY: Final = "cardrag.qwen3-query.v1"
QWEN3_DOCUMENT_POLICY: Final = "cardrag.structure-views.v1"
QWEN3_TRUNCATION_POLICY: Final = "error"
QWEN3_QUERY_INSTRUCTION: Final = (
    "Instruct: Given a Korean financial product disclosure search query, retrieve relevant "
    "passages from Korean credit card product disclosure sections and sentence units that answer "
    "the query"
)

Qwen3EmbeddingProviderId = Literal["deepinfra", "nebius"]


def _validate_qwen3_provider(provider_id: str) -> None:
    if provider_id not in QWEN3_EMBEDDING_PROVIDER_IDS:
        raise ValueError("Qwen embedding provider must be deepinfra or nebius")


def _validate_maximum_tokens(maximum_tokens: int) -> None:
    if isinstance(maximum_tokens, bool) or not isinstance(maximum_tokens, int) or maximum_tokens <= 0:
        raise ValueError("maximum_tokens must be a positive integer")


def _qwen3_profile_digest(provider_id: str, maximum_tokens: int) -> str:
    _validate_qwen3_provider(provider_id)
    _validate_maximum_tokens(maximum_tokens)
    return canonical_sha256(
        {
            "dimension": QWEN3_EMBEDDING_DIMENSION,
            "document_instruction": QWEN3_DOCUMENT_INSTRUCTION,
            "document_policy": QWEN3_DOCUMENT_POLICY,
            "dtype": QWEN3_EMBEDDING_DTYPE,
            "maximum_tokens": maximum_tokens,
            "model": QWEN3_EMBEDDING_MODEL,
            "normalization": QWEN3_EMBEDDING_NORMALIZATION,
            "provider": QWEN3_EMBEDDING_PROVIDER,
            "provider_fallback": QWEN3_EMBEDDING_PROVIDER_FALLBACK_POLICY,
            "provider_id": provider_id,
            "query_policy": QWEN3_QUERY_POLICY,
            "schema_version": "cardrag.embedding-profile.v1",
            "truncation": QWEN3_TRUNCATION_POLICY,
        }
    )


def qwen3_embedding_profile_id(
    provider_id: Qwen3EmbeddingProviderId,
    *,
    maximum_tokens: int,
) -> str:
    """Return the immutable, upstream-provider-specific Qwen profile ID."""

    digest = _qwen3_profile_digest(provider_id, maximum_tokens)
    return f"cardrag.qwen3-embedding-8b.{provider_id}.{digest}"


def qwen3_embedding_cache_namespace(
    provider_id: Qwen3EmbeddingProviderId,
    *,
    maximum_tokens: int,
) -> str:
    """Return a portable cache namespace that cannot mix routed providers."""

    digest = _qwen3_profile_digest(provider_id, maximum_tokens)
    return f"cardrag-qwen3-embedding-8b-{provider_id}-{digest}"


def format_qwen3_query(query: str) -> str:
    """Apply the exact v5 Qwen query instruction without rewriting the query."""

    if not isinstance(query, str):
        raise TypeError("query must be a string")
    if not query.strip():
        raise ValueError("query must not be empty")
    return f"{QWEN3_QUERY_INSTRUCTION}\nQuery:{query}"


def format_qwen3_document(document: str) -> str:
    """Return v5 document input byte-for-byte; documents have no instruction."""

    if not isinstance(document, str):
        raise TypeError("document must be a string")
    if not document.strip():
        raise ValueError("document must not be empty")
    return document


__all__ = [
    "DOCUMENT_EMBEDDING_PREFIX",
    "EMBEDDING_DIMENSION",
    "EMBEDDING_POLICY_VERSION",
    "QUERY_EMBEDDING_PREFIX",
    "QWEN3_DOCUMENT_INSTRUCTION",
    "QWEN3_DOCUMENT_POLICY",
    "QWEN3_EMBEDDING_DIMENSION",
    "QWEN3_EMBEDDING_DTYPE",
    "QWEN3_EMBEDDING_MODEL",
    "QWEN3_EMBEDDING_NORMALIZATION",
    "QWEN3_EMBEDDING_PROVIDER",
    "QWEN3_EMBEDDING_PROVIDER_FALLBACK_POLICY",
    "QWEN3_EMBEDDING_PROVIDER_IDS",
    "QWEN3_QUERY_INSTRUCTION",
    "QWEN3_QUERY_POLICY",
    "QWEN3_TRUNCATION_POLICY",
    "Qwen3EmbeddingProviderId",
    "format_qwen3_document",
    "format_qwen3_query",
    "qwen3_embedding_cache_namespace",
    "qwen3_embedding_profile_id",
]
