"""Replay normalized candidate MCP responses against the public API models.

This offline entry point delegates evidence reading and generation/hash binding
to cardrag-core. It never constructs a serving repository, provider, or server.
Only independently available request models are replayed here; required argument
and identity checks for every tool remain in the core receipt verifier.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from cardrag_core import candidate_acceptance, canonical_json_bytes
from cardrag_core.candidate_acceptance import CandidateAcceptanceError, ToolSmokeResult
from pydantic import BaseModel, TypeAdapter, ValidationError

from cardrag_mcp.issuer_input import IssuerInput, normalize_issuer, normalize_optional_issuer
from cardrag_mcp.models import (
    ContractBundle,
    ContractSearchPage,
    ContractSearchRequest,
    EvidencePage,
    MerchantSearchPage,
    OCRFailedProduct,
    Product,
    ProductCatalogPage,
    ProductCoverage,
    ProductRevisionList,
    ProductSummary,
    ProductSummaryBatch,
    ProductSummaryRequest,
    RecentProductCatalogPage,
    SearchPage,
    SearchRequest,
    SourcePage,
    SourcePdfDescriptor,
    UnsupportedProduct,
)

_RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "search_contracts": ContractSearchPage,
    "get_contract_bundle": ContractBundle,
    "list_product_revisions": ProductRevisionList,
    "search_evidence": SearchPage,
    "get_evidence": EvidencePage,
    "get_product": Product,
    "get_source_pdf": SourcePdfDescriptor,
    "get_source_page": SourcePage,
    "list_recent_products": RecentProductCatalogPage,
    "find_products": ProductCatalogPage,
    "find_cards_by_merchant": MerchantSearchPage,
    "get_product_summary": ProductSummary,
}
_PRODUCT_MODELS: dict[str, type[BaseModel]] = {
    "available": Product,
    "unsupported_drm": UnsupportedProduct,
    "ocr_failed": OCRFailedProduct,
}
_ISSUER: TypeAdapter[str | None] = TypeAdapter(IssuerInput | None)
_ISSUERS: TypeAdapter[list[str] | None] = TypeAdapter(list[IssuerInput] | None)


def _validate_available_request_models(call: ToolSmokeResult) -> None:
    arguments: dict[str, Any] = dict(call.request_arguments)
    if "issuer" in arguments:
        arguments["issuer"] = normalize_optional_issuer(
            _ISSUER.validate_python(arguments["issuer"])
        )
        if (
            arguments["issuer"] is not None
            and "issuer" in call.response
            and call.response["issuer"] != arguments["issuer"]
        ):
            raise ValueError("response issuer differs from the request")
    if "issuers" in arguments:
        issuers = _ISSUERS.validate_python(arguments["issuers"])
        if issuers is not None:
            for issuer in issuers:
                normalize_issuer(issuer)
    if call.tool == "search_contracts":
        for key in ("as_of", "launch_start_date", "launch_end_date"):
            if arguments.get(key) is not None:
                arguments[key] = date.fromisoformat(arguments[key])
        ContractSearchRequest.model_validate(arguments)
    elif call.tool == "search_evidence":
        filters = {
            key: arguments.pop(key)
            for key in ("issuer", "product_code", "section_type")
            if key in arguments
        }
        SearchRequest.model_validate({**arguments, "filters": filters})
    elif call.tool == "get_product_summary":
        products = arguments.get("products")
        if products is None:
            ProductSummaryRequest.model_validate(
                {"issuer": arguments.get("issuer"), "identifier": arguments.get("identifier")}
            )
        else:
            for product in products:
                request = ProductSummaryRequest.model_validate(product)
                normalize_optional_issuer(_ISSUER.validate_python(request.issuer))


def validate_tool_response(call: ToolSmokeResult) -> None:
    """Reject invalid public response schemas without exposing evidence values."""

    try:
        _validate_available_request_models(call)
        model = _RESPONSE_MODELS[call.tool]
        if call.tool == "get_product":
            availability = call.response.get("availability")
            if not isinstance(availability, str):
                raise ValueError("missing product availability")
            model = _PRODUCT_MODELS[availability]
        elif call.tool == "find_products" and call.request_arguments.get("mode") == "coverage":
            model = ProductCoverage
        elif (
            call.tool == "get_product_summary"
            and call.request_arguments.get("products") is not None
        ):
            model = ProductSummaryBatch
        model.model_validate_json(canonical_json_bytes(call.response), strict=True)
    except (ValidationError, ValueError, TypeError, KeyError):
        raise CandidateAcceptanceError("mcp_response_schema_invalid") from None


def main(argv: Sequence[str] | None = None) -> int:
    return candidate_acceptance.main(argv, mcp_response_validator=validate_tool_response)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
