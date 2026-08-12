from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from cardrag.acquisition import PDFValidationError
from cardrag.acquisition.download import DownloadSecurityError
from cardrag.domain import Issuer
from cardrag.issuers.base import IssuerMarkupChanged, SourceRecord, UnsupportedCategory
from cardrag.jobs import ClaimedJob
from cardrag.pipeline.runtime import (
    PERMANENT_PIPELINE_ERRORS,
    OfflinePipeline,
    allow_daily_ocr_fallback,
    authoritative_is_latest,
    http_retry_policy,
    validate_discovery_volume,
)


def _status_error(status: int, *, retry_after: str | None = None) -> httpx.HTTPStatusError:
    headers = {"Retry-After": retry_after} if retry_after is not None else None
    request = httpx.Request("GET", "https://issuer.example/document.pdf")
    response = httpx.Response(status, request=request, headers=headers)
    return httpx.HTTPStatusError("fixture", request=request, response=response)


def test_http_retry_policy_honors_retry_after_and_issuer_failure_domains() -> None:
    assert http_retry_policy(_status_error(429, retry_after="120"), Issuer.WOORI) == (
        True,
        5.0,
        120.0,
    )
    assert http_retry_policy(_status_error(503), Issuer.SHINHAN) == (True, 10.0, 0.0)
    assert http_retry_policy(_status_error(404), Issuer.KB) == (False, 5.0, 0.0)


def test_daily_ocr_fallback_waits_for_durable_primary_retry_budget() -> None:
    assert not allow_daily_ocr_fallback(
        bulk=False, attempt_no=1, max_attempts=3, has_api_key=True
    )
    assert allow_daily_ocr_fallback(
        bulk=False, attempt_no=3, max_attempts=3, has_api_key=True
    )
    assert not allow_daily_ocr_fallback(
        bulk=True, attempt_no=3, max_attempts=3, has_api_key=True
    )
    assert not allow_daily_ocr_fallback(
        bulk=False, attempt_no=3, max_attempts=3, has_api_key=False
    )


def test_schema_category_pdf_and_download_security_failures_are_permanent() -> None:
    errors = (
        IssuerMarkupChanged("fixture"),
        UnsupportedCategory("fixture"),
        PDFValidationError("fixture"),
        DownloadSecurityError("fixture"),
    )
    assert all(isinstance(error, PERMANENT_PIPELINE_ERRORS) for error in errors)


def test_history_date_never_overrides_authoritative_current_marker() -> None:
    common = {
        "issuer": "woori",
        "product_code": "fixture",
        "product_name": "합성 카드",
        "source_url": "https://pc.wooricard.com/current.pdf",
        "source_post_id": "fixture",
        "file_name": "fixture.pdf",
        "category": "credit",
        "discovered_at": datetime(2026, 8, 12, tzinfo=UTC),
    }
    current = SourceRecord(
        **common,
        effective_date=date(2025, 1, 1),
        source_version="listing-current",
        is_current=True,
    )
    history_with_later_date = SourceRecord(
        **common,
        effective_date=date(2026, 8, 1),
        source_version="history-later",
        is_current=False,
    )

    assert authoritative_is_latest(current)
    assert not authoritative_is_latest(history_with_later_date)


def test_discovery_volume_rejects_major_drop_before_catalog_mutation() -> None:
    validate_discovery_volume(
        issuer=Issuer.WOORI,
        observed=80,
        absolute_minimum=20,
        previous_observed=100,
        minimum_previous_ratio=0.6,
    )
    with pytest.raises(IssuerMarkupChanged, match="anomaly threshold is 60"):
        validate_discovery_volume(
            issuer=Issuer.WOORI,
            observed=1,
            absolute_minimum=20,
            previous_observed=100,
            minimum_previous_ratio=0.6,
        )


def test_ocr_workspace_is_generation_job_attempt_and_fence_scoped(tmp_path: Path) -> None:
    pipeline = object.__new__(OfflinePipeline)
    pipeline.settings = SimpleNamespace(build_root=tmp_path)
    common = {
        "issuer": "woori",
        "stage": "ocr",
        "document_id": "doc-stable",
        "payload": {"generation_id": "generation-a"},
        "attempt_no": 1,
        "max_attempts": 3,
        "lease_until": datetime.now(UTC) + timedelta(seconds=30),
        "lease_owner": "worker",
        "generation_id": "generation-a",
    }
    first = ClaimedJob(id=uuid.uuid4(), fencing_token=1, **common)
    reclaimed = ClaimedJob(id=first.id, fencing_token=2, **common)

    def root(claim: ClaimedJob) -> Path:
        return (
            pipeline.settings.build_root
            / "ocr"
            / str(claim.generation_id or claim.payload.get("generation_id") or "no-generation")
            / str(claim.document_id)
            / f"{claim.id}-{claim.attempt_no}-{claim.fencing_token}"
        )

    assert root(first) != root(reclaimed)
    assert root(first).is_relative_to(tmp_path / "ocr" / "generation-a" / "doc-stable")
