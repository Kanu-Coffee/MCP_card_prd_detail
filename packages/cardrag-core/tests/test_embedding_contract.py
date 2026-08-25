from __future__ import annotations

from cardrag_core import (
    DOCUMENT_EMBEDDING_PREFIX,
    EMBEDDING_DIMENSION,
    EMBEDDING_POLICY_VERSION,
    QUERY_EMBEDDING_PREFIX,
)


def test_embedding_input_contract_is_exact_and_shared() -> None:
    assert EMBEDDING_DIMENSION == 1536
    assert EMBEDDING_POLICY_VERSION == "cardrag.embedding-input.v1"
    assert DOCUMENT_EMBEDDING_PREFIX == ("Represent this Korean card-disclosure evidence for retrieval: ")
    assert QUERY_EMBEDDING_PREFIX == ("Retrieve Korean card-disclosure evidence that answers: ")
    assert DOCUMENT_EMBEDDING_PREFIX != QUERY_EMBEDDING_PREFIX
    assert DOCUMENT_EMBEDDING_PREFIX.endswith(" ")
    assert QUERY_EMBEDDING_PREFIX.endswith(" ")
