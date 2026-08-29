"""Deterministic document-level aggregation for v5 exact row scores."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence

from cardrag_mcp.models import DocumentAggregationPolicy, ViewType

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DocumentAggregationError(ValueError):
    """A row set cannot satisfy the selected document aggregation contract."""


def aggregate_document_view_scores(
    scores: Sequence[tuple[ViewType, float]],
    policy: DocumentAggregationPolicy,
) -> float:
    """Aggregate every scored row for one contract using the sealed formula.

    ``max_child`` and ``top3_mean`` intentionally exclude the CONTRACT lane.
    The input is the raw exact-row score stream, not node maxima.
    """

    if not scores or any(not math.isfinite(score) for _view_type, score in scores):
        raise DocumentAggregationError("document aggregation requires finite row scores")
    child_scores = [score for view_type, score in scores if view_type != "CONTRACT"]
    if not child_scores:
        raise DocumentAggregationError(
            "document aggregation requires at least one non-CONTRACT row"
        )
    maximum_child = max(child_scores)
    if policy == "max_child":
        return maximum_child
    if policy == "top3_mean":
        highest = sorted(child_scores, reverse=True)[:3]
        return math.fsum(highest) / len(highest)
    if policy != "contract_plus_child":
        raise DocumentAggregationError("unknown document aggregation policy")
    contract_scores = [score for view_type, score in scores if view_type == "CONTRACT"]
    if len(contract_scores) != 1:
        raise DocumentAggregationError("contract_plus_child requires exactly one CONTRACT row")
    return math.fsum((0.5 * contract_scores[0], 0.5 * maximum_child))


def exhaustive_profile_id(
    *,
    policy: DocumentAggregationPolicy,
    sealed_profile_sha256: str | None,
) -> str:
    """Bind resumable exhaustive state to the selected aggregation profile."""

    if sealed_profile_sha256 is None:
        if policy != "max_child":
            raise DocumentAggregationError("unsealed exhaustive scoring must use max_child")
        identity = "candidate-default"
    else:
        if _SHA256.fullmatch(sealed_profile_sha256) is None:
            raise DocumentAggregationError("sealed aggregation profile SHA-256 is invalid")
        identity = sealed_profile_sha256
    return f"cardrag.exhaustive.exact-document-aggregation.v2.{policy}.{identity}"


__all__ = [
    "DocumentAggregationError",
    "aggregate_document_view_scores",
    "exhaustive_profile_id",
]
