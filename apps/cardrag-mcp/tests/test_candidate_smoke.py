from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from cardrag_core import canonical_sha256
from cardrag_core.candidate_acceptance import (
    MCP_TOOLS,
    CandidateAcceptanceError,
    ToolSmokeResult,
)
from pydantic import ValidationError

from cardrag_mcp import candidate_smoke

GENERATION = "g-synthetic-smoke"
SHA256 = "a" * 64


def _examples() -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    document = {
        "document_id": "doc-1",
        "issuer": "kb",
        "product_code": "000001",
        "title": "Synthetic product guide",
        "pdf_sha256": SHA256,
        "pdf_size_bytes": 100,
        "page_count": 1,
    }
    contract = {
        "product_lineage_id": "lineage-1",
        "contract_revision_id": "revision-1",
        "document_id": "doc-1",
        "issuer": "kb",
        "product_code": "000001",
        "product_name": "Synthetic Card",
        "document_type": "guide",
        "source_id": "source-1",
        "source_version": "v1",
        "source_url": "https://example.com/guide.pdf",
        "effective_date": "2026-01-01",
        "temporal_status": "current",
        "pdf_sha256": SHA256,
        "page_count": 1,
    }
    evidence = {
        "evidence_id": "evidence-1",
        "document_id": "doc-1",
        "issuer": "kb",
        "product_code": "000001",
        "product_name": "Synthetic Card",
        "document_title": "Synthetic product guide",
        "page_start": 1,
        "page_end": 1,
        "section_type": "body",
        "text": "Synthetic benefit",
        "source_start": 0,
        "source_end": 17,
        "pdf_sha256": SHA256,
    }
    catalog_entry = {
        key: contract[key]
        for key in (
            "issuer",
            "product_code",
            "product_lineage_id",
            "product_name",
            "document_type",
            "temporal_status",
        )
    }
    catalog_entry.update(launch_date="2026-01-01", launch_date_status="confirmed")
    catalog = {"generation_id": GENERATION, "items": [catalog_entry], "total_count": 1}
    coverage = {
        "generation_id": GENERATION,
        "search_mode": "exact",
        "temporal_scope": "current",
        "expected_active_contracts": 1,
        "scored_contracts": 1,
        "expected_embedding_rows": 1,
        "scored_embedding_rows": 1,
        "exact_search_milliseconds": 1.0,
        "exact_blocks": 1,
        "lexical_additional_evidence_count": 0,
        "lexical_enabled": True,
        "lexical_status": "succeeded",
        "lexical_global_matched_evidence_count": 0,
        "lexical_global_additional_evidence_count": 0,
        "catalog_resolution_status": "unresolved",
        "catalog_candidate_count": 0,
        "response_node_count": 0,
        "response_character_count": 0,
    }
    return {
        "search_contracts": (
            {"query": "benefit"},
            {"generation_id": GENERATION, "bundles": [], "coverage": coverage},
        ),
        "get_contract_bundle": (
            {"contract_revision_id": "revision-1"},
            {"generation_id": GENERATION, "contract": contract, "scope": "full", "nodes": []},
        ),
        "list_product_revisions": (
            {"issuer": "kb", "product_lineage_id": "lineage-1"},
            {
                "generation_id": GENERATION,
                "issuer": "kb",
                "product_lineage_id": "lineage-1",
                "revisions": [contract],
            },
        ),
        "search_evidence": (
            {"query": "benefit"},
            {
                "generation_id": GENERATION,
                "items": [evidence],
                "retrieval_mode": "exact",
                "degraded": False,
            },
        ),
        "get_evidence": (
            {"evidence_id": "evidence-1"},
            {
                "generation_id": GENERATION,
                "evidence_id": "evidence-1",
                "document_id": "doc-1",
                "items": [evidence],
            },
        ),
        "get_product": (
            {"issuer": "KB국민카드", "product_code": "000001"},
            {
                "issuer": "kb",
                "product_code": "000001",
                "name": "Synthetic Card",
                "availability": "available",
                "document": document,
            },
        ),
        "get_source_pdf": (
            {"document_id": "doc-1"},
            {
                "document_id": "doc-1",
                "url": "https://example.com/sources/doc-1/pdf",
                "sha256": SHA256,
                "size_bytes": 100,
                "mime_type": "application/pdf",
                "range_supported": True,
            },
        ),
        "get_source_page": (
            {"document_id": "doc-1", "page": 1},
            {
                "document_id": "doc-1",
                "page": 1,
                "page_count": 1,
                "text": "Synthetic benefit",
                "text_sha256": SHA256,
                "pdf_sha256": SHA256,
            },
        ),
        "list_recent_products": (
            {"months": 3},
            {
                **catalog,
                "period_start": "2025-12-01",
                "period_end": "2026-03-01",
                "unknown_launch_date_count": 0,
            },
        ),
        "find_products": ({"keyword": "Synthetic"}, catalog),
        "find_cards_by_merchant": (
            {"merchant_name": "Synthetic Merchant"},
            {
                "generation_id": GENERATION,
                "merchant_query": "Synthetic Merchant",
                "items": [
                    {
                        "issuer": "kb",
                        "product_code": "000001",
                        "product_name": "Synthetic Card",
                        "matched_texts": ["Synthetic Merchant benefit"],
                    }
                ],
                "total_count": 1,
            },
        ),
        "get_product_summary": (
            {"issuer": "kb", "identifier": "000001"},
            {
                "generation_id": GENERATION,
                "issuer": "kb",
                "product_code": "000001",
                "product_name": "Synthetic Card",
                "launch_date": "2026-01-01",
                "launch_date_status": "confirmed",
                "evidence": [
                    {
                        "field": "launch_date",
                        "node_id": "node-1",
                        "pages": [1],
                        "excerpt": "Synthetic date",
                    }
                ],
            },
        ),
    }


def _call(
    tool: str,
    arguments: dict[str, Any] | None = None,
    response: dict[str, Any] | None = None,
) -> ToolSmokeResult:
    default_arguments, default_response = _examples()[tool]
    arguments = default_arguments if arguments is None else arguments
    response = default_response if response is None else response
    return ToolSmokeResult(
        tool=tool,
        passed=True,
        generation_id=GENERATION,
        request_arguments=arguments,
        request_sha256=canonical_sha256({"tool": tool, "arguments": arguments}),
        response=response,
        response_sha256=canonical_sha256(response),
    )


@pytest.mark.parametrize("tool", MCP_TOOLS)
def test_all_twelve_normalized_public_responses_validate(tool: str) -> None:
    candidate_smoke.validate_tool_response(_call(tool))


@pytest.mark.parametrize("availability", ["unsupported_drm", "ocr_failed"])
def test_product_availability_branches_validate(availability: str) -> None:
    response = _examples()["get_product"][1]
    response["availability"] = availability
    if availability == "unsupported_drm":
        response.pop("document")
        response.update(
            source_id="source-1",
            source_version="v1",
            source_url="https://example.com/guide.pdf",
            protected_magic="SCDSA004",
            protected_source_sha256=SHA256,
            protected_source_size_bytes=100,
        )
    else:
        response.update(reason_code="ocr_unavailable", reason="Synthetic OCR failure", attempts=1)
    candidate_smoke.validate_tool_response(_call("get_product", response=response))


@pytest.mark.parametrize("mode", ["catalog", "coverage"])
def test_catalog_and_coverage_branches_validate(mode: str) -> None:
    response = _examples()["find_products"][1]
    if mode == "coverage":
        response = {
            "generation_id": GENERATION,
            "schema_id": "cardrag.serving-db.v5",
            "launch_date_support": "supported",
            "supported_issuers": ["kb"],
            "loaded_issuers": ["kb"],
            "issuers": [
                {
                    "issuer": "kb",
                    "display_name": "KB국민카드",
                    "loaded": True,
                    "product_count": 1,
                    "available_product_count": 1,
                    "unsupported_drm_count": 0,
                    "ocr_failed_count": 0,
                }
            ],
            "product_count": 1,
        }
    candidate_smoke.validate_tool_response(_call("find_products", {"mode": mode}, response))


def test_summary_batch_accepts_nullable_items_and_validates_nested_items() -> None:
    response = {
        "generation_id": GENERATION,
        "items": [_examples()["get_product_summary"][1], None],
    }
    arguments = {
        "products": [
            {"issuer": "kb", "identifier": "000001"},
            {"issuer": "kb", "identifier": "missing"},
        ]
    }
    candidate_smoke.validate_tool_response(_call("get_product_summary", arguments, response))
    response["items"][0]["evidence"][0]["field"] = "private-invalid-field"
    with pytest.raises(CandidateAcceptanceError, match="^mcp_response_schema_invalid$"):
        candidate_smoke.validate_tool_response(_call("get_product_summary", arguments, response))


@pytest.mark.parametrize("tool", MCP_TOOLS)
@pytest.mark.parametrize("response", [{}, {"passed": True}])
def test_callback_itself_rejects_shape_free_success(tool: str, response: dict[str, Any]) -> None:
    # Core rejects these before the callback; bypass it to exercise this independent guard.
    call = _call(tool).model_copy(update={"response": response})
    with pytest.raises(CandidateAcceptanceError, match="^mcp_response_schema_invalid$"):
        candidate_smoke.validate_tool_response(call)


@pytest.mark.parametrize(
    ("tool", "path", "value"),
    [
        ("get_product", ("document", "pdf_sha256"), "private-invalid-value"),
        ("get_contract_bundle", ("contract", "page_count"), "1"),
        ("search_contracts", ("coverage", "scored_embedding_rows"), 0),
        ("search_contracts", ("coverage", "lexical_enabled"), 1),
        ("get_evidence", ("items", 0, "source_start"), -1),
        ("list_recent_products", ("items", 0, "launch_date"), "2026-02-30"),
        ("find_products", ("items", 0, "unrecognized_field"), "private-invalid-value"),
        ("find_cards_by_merchant", ("items", 0, "matched_texts"), []),
    ],
)
def test_nested_schema_errors_are_bounded_and_not_coerced(
    tool: str, path: tuple[str | int, ...], value: Any
) -> None:
    response = deepcopy(_examples()[tool][1])
    target = response
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    # All top-level core shape/hash bindings are valid; the API models find the defect.
    call = _call(tool, response=response)
    with pytest.raises(CandidateAcceptanceError) as caught:
        candidate_smoke.validate_tool_response(call)
    assert str(caught.value) == "mcp_response_schema_invalid"
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize(
    ("tool", "updates"),
    [
        ("search_contracts", {"limit": 101}),
        ("search_contracts", {"launch_start_date": "2026-01-01"}),
        ("search_contracts", {"as_of": "2026-01-01", "include_history": True}),
        ("search_contracts", {"product_lineage_ids": ["lineage-1", "lineage-1"]}),
        ("search_evidence", {"limit": 51}),
        ("search_evidence", {"query": "x" * 2001}),
        ("get_product", {"issuer": "unknown synthetic issuer"}),
    ],
)
def test_existing_request_models_and_issuer_validation_are_replayed(
    tool: str, updates: dict[str, Any]
) -> None:
    arguments = {**_examples()[tool][0], **updates}
    with pytest.raises(CandidateAcceptanceError, match="^mcp_response_schema_invalid$"):
        candidate_smoke.validate_tool_response(_call(tool, arguments=arguments))


def test_core_rejects_foreign_generation_even_with_recomputed_hashes() -> None:
    response = _examples()["search_contracts"][1]
    response["coverage"]["generation_id"] = "g-other-generation"
    with pytest.raises(ValidationError, match="different generation"):
        _call("search_contracts", response=response)


def test_response_issuer_must_match_the_normalized_request_issuer() -> None:
    response = _examples()["get_product"][1]
    response["issuer"] = "samsung"
    with pytest.raises(CandidateAcceptanceError, match="^mcp_response_schema_invalid$"):
        candidate_smoke.validate_tool_response(_call("get_product", response=response))


def test_cli_delegates_bound_evidence_reading_with_the_response_validator(monkeypatch) -> None:
    received = []

    def core_main(argv, *, mcp_response_validator):
        received.append((argv, mcp_response_validator))
        return 7

    monkeypatch.setattr(candidate_smoke.candidate_acceptance, "main", core_main)
    arguments = ["--help"]
    assert candidate_smoke.main(arguments) == 7
    assert received == [(arguments, candidate_smoke.validate_tool_response)]
