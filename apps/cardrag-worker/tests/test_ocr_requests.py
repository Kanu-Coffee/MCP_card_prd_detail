from __future__ import annotations

from pathlib import Path

import pytest
from cardrag_core import GenerationManifest, generation_manifest_path
from test_gc import build_remote

from cardrag_worker.ocr_requests import (
    complete_reprocess_request,
    load_next_reprocess_request,
    plan_reprocess_requests,
    queue_reprocess_requests,
    select_run_reprocess_request,
)


def _stable_manifest() -> GenerationManifest:
    webdav, _, _, _ = build_remote()
    return GenerationManifest.model_validate_json(
        webdav.objects[generation_manifest_path("g-current").as_posix()]
    )


def test_reprocess_preview_requires_explicit_scope_and_preserves_pdf_identity() -> None:
    stable = _stable_manifest()
    with pytest.raises(ValueError, match="select document IDs"):
        plan_reprocess_requests(stable)
    with pytest.raises(ValueError, match="confirm_all"):
        plan_reprocess_requests(stable, all_documents=True)

    requests = plan_reprocess_requests(stable, document_ids=("doc_kb",))
    assert len(requests) == 1
    target = requests[0].targets[0]
    assert target.document_id == "doc_kb"
    assert target.pdf_sha256 == stable.documents[0].pdf.sha256
    assert target.page_count == 1


def test_reprocess_request_queue_is_immutable_and_completion_is_idempotent(tmp_path: Path) -> None:
    stable = _stable_manifest()
    requests = plan_reprocess_requests(stable, all_documents=True, confirm_all=True, max_documents=1)
    queue_reprocess_requests(tmp_path, requests)
    loaded = load_next_reprocess_request(tmp_path)
    assert loaded == requests[0]
    with pytest.raises(FileExistsError):
        queue_reprocess_requests(tmp_path, requests)
    complete_reprocess_request(tmp_path, requests[0], run_id="run-test", generation_id="g-result")
    complete_reprocess_request(tmp_path, requests[0], run_id="run-test", generation_id="g-result")
    assert load_next_reprocess_request(tmp_path) is None


def test_resumed_run_keeps_original_request_when_queue_changes(tmp_path: Path) -> None:
    stable = _stable_manifest()
    first = plan_reprocess_requests(stable, document_ids=("doc_kb",))[0]
    queue_reprocess_requests(tmp_path, (first,))
    assert select_run_reprocess_request(tmp_path, "run-original") == first
    complete_reprocess_request(tmp_path, first, run_id="run-other", generation_id="g-other")
    second = plan_reprocess_requests(stable, document_ids=("doc_kb",))[0]
    queue_reprocess_requests(tmp_path, (second,))
    assert select_run_reprocess_request(tmp_path, "run-original") == first
    assert select_run_reprocess_request(tmp_path, "run-next") == second


def test_invalid_completion_receipt_cannot_silently_skip_reprocessing(tmp_path: Path) -> None:
    request = plan_reprocess_requests(_stable_manifest(), document_ids=("doc_kb",))[0]
    queue_reprocess_requests(tmp_path, (request,))
    completed = tmp_path / "ocr-requests" / "completed"
    completed.mkdir()
    (completed / f"{request.request_id}.json").write_bytes(b"{}")
    with pytest.raises(ValueError, match="completion receipt contract"):
        load_next_reprocess_request(tmp_path)
