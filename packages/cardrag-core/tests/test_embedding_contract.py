from __future__ import annotations

from cardrag_core import (
    DOCUMENT_EMBEDDING_PREFIX,
    EMBEDDING_DIMENSION,
    EMBEDDING_POLICY_VERSION,
    QUERY_EMBEDDING_PREFIX,
    QWEN3_DOCUMENT_INSTRUCTION,
    QWEN3_DOCUMENT_POLICY,
    QWEN3_EMBEDDING_DIMENSION,
    QWEN3_EMBEDDING_DTYPE,
    QWEN3_EMBEDDING_MODEL,
    QWEN3_EMBEDDING_NORMALIZATION,
    QWEN3_QUERY_INSTRUCTION,
    QWEN3_QUERY_POLICY,
    QWEN3_TRUNCATION_POLICY,
    format_qwen3_document,
    format_qwen3_query,
)


def test_embedding_input_contract_is_exact_and_shared() -> None:
    assert EMBEDDING_DIMENSION == 1536
    assert EMBEDDING_POLICY_VERSION == "cardrag.embedding-input.v1"
    assert DOCUMENT_EMBEDDING_PREFIX == ("Represent this Korean card-disclosure evidence for retrieval: ")
    assert QUERY_EMBEDDING_PREFIX == ("Retrieve Korean card-disclosure evidence that answers: ")
    assert DOCUMENT_EMBEDDING_PREFIX != QUERY_EMBEDDING_PREFIX
    assert DOCUMENT_EMBEDDING_PREFIX.endswith(" ")
    assert QUERY_EMBEDDING_PREFIX.endswith(" ")


def test_qwen3_embedding_contract_and_formatters_are_exact() -> None:
    query = "전월 실적 제외 대상은 무엇인가요?"
    document = "  원문 제목\n\n원문 세부설명  "

    assert QWEN3_EMBEDDING_MODEL == "qwen/qwen3-embedding-8b"
    assert QWEN3_EMBEDDING_DIMENSION == 4096
    assert QWEN3_EMBEDDING_DTYPE == "float32"
    assert QWEN3_EMBEDDING_NORMALIZATION == "l2"
    assert QWEN3_DOCUMENT_INSTRUCTION is None
    assert QWEN3_QUERY_POLICY == "cardrag.qwen3-query.v1"
    assert QWEN3_DOCUMENT_POLICY == "cardrag.structure-views.v1"
    assert QWEN3_TRUNCATION_POLICY == "error"
    assert QWEN3_QUERY_INSTRUCTION == (
        "Instruct: Given a Korean financial product disclosure search query, retrieve relevant "
        "passages from Korean credit card product disclosure sections and sentence units that answer "
        "the query"
    )
    assert format_qwen3_query(query) == f"{QWEN3_QUERY_INSTRUCTION}\nQuery:{query}"
    assert format_qwen3_document(document) == document
