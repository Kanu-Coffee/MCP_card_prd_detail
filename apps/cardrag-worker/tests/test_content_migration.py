from __future__ import annotations

from pathlib import Path

import pytest
from cardrag_core import object_path, sha256_bytes
from test_gc import build_remote

from cardrag_worker.content_cache import content_index_path
from cardrag_worker.content_inventory import ContentInventoryError
from cardrag_worker.content_migration import apply_content_migration, plan_content_migration
from cardrag_worker.ocr import OCRResolver
from cardrag_worker.state import WorkerState


@pytest.mark.asyncio
async def test_migration_plan_preserves_stable_document_without_ocr_call() -> None:
    webdav, _active_key, _inactive_key, _unused = build_remote()
    body = "## Page 1\n\n카드 혜택 조건과 제외 사항을 충분히 설명하는 본문입니다.\n".encode()
    webdav.objects[object_path(sha256_bytes(body)).as_posix()] = body

    with pytest.raises(ContentInventoryError, match="every legacy OCR"):
        await plan_content_migration(webdav)  # type: ignore[arg-type]

    # Remove the deliberately incomplete orphan fixture from the remote listing.
    native_root = "v1/ocr-cache/native"
    for prefix in webdav.children[native_root]:
        webdav.children[prefix.as_posix()] = tuple(
            root
            for root in webdav.children[prefix.as_posix()]
            if root.name in webdav.objects or (root / "READY.json").as_posix() in webdav.objects
        )
    plan = await plan_content_migration(webdav)  # type: ignore[arg-type]
    assert plan.legacy_variant_count == 2
    assert plan.generation_only_variant_count == 0
    assert len(plan.stable_document_variant_ids) == 1
    assert all(variant.migrated_from is not None for variant in plan.variants)
    assert plan.summary()["migration_applied"] is False


@pytest.mark.asyncio
async def test_migration_plan_imports_generation_only_cas() -> None:
    webdav, _active_key, _inactive_key, _unused = build_remote()
    body = "## Page 1\n\n카드 혜택 조건과 제외 사항을 충분히 설명하는 본문입니다.\n".encode()
    webdav.objects[object_path(sha256_bytes(body)).as_posix()] = body
    webdav.children["v1/ocr-cache/native"] = ()

    plan = await plan_content_migration(webdav)  # type: ignore[arg-type]

    assert plan.legacy_variant_count == 0
    assert plan.generation_only_variant_count == 1
    variant = plan.variants[0]
    assert variant.provenance.provider == "generation-only"
    assert variant.output.sha256 == sha256_bytes(body)
    assert plan.stable_document_variant_ids["doc_kb"] == variant.variant_id


@pytest.mark.asyncio
async def test_migration_apply_is_append_only_and_retryable(tmp_path: Path) -> None:
    webdav, _active_key, _inactive_key, _unused = build_remote()
    body = "## Page 1\n\n카드 혜택 조건과 제외 사항을 충분히 설명하는 본문입니다.\n".encode()
    webdav.objects[object_path(sha256_bytes(body)).as_posix()] = body
    webdav.children["v1/ocr-cache/native"] = ()
    plan = await plan_content_migration(webdav)  # type: ignore[arg-type]

    assert await apply_content_migration(webdav, plan, state_root=tmp_path) == 1  # type: ignore[arg-type]
    assert await apply_content_migration(webdav, plan, state_root=tmp_path) == 1  # type: ignore[arg-type]
    variant = plan.variants[0]
    assert str(content_index_path(variant)) in webdav.objects
    assert variant.output.path in webdav.objects
    assert webdav.deleted == []

    class NoCallProvider:
        provider = "opencode"
        model = "alibaba-token-plan/qwen3.8-flash"
        reasoning_effort = "medium"

        async def recognize(self, *_args: object, **_kwargs: object) -> str:
            raise AssertionError("migrated content must avoid OCR provider calls")

    index = content_index_path(variant)
    webdav.children[index.parent.as_posix()] = (index,)
    pdf_path = tmp_path / "source.pdf"
    pdf_path.write_bytes(b"pdf")
    with WorkerState(tmp_path / "state.sqlite3") as state:
        resolver = OCRResolver(
            provider=NoCallProvider(),  # type: ignore[arg-type]
            state=state,
            webdav=webdav,  # type: ignore[arg-type]
            cache_mode="read-only",
            require_cache_hit=True,
        )
        result = await resolver.resolve(
            run_id="rehearsal",
            document_id="doc_kb",
            pdf_path=pdf_path,
            pdf_sha256=sha256_bytes(b"pdf"),
            pdf_size_bytes=3,
            page_count=1,
            output_dir=tmp_path / "runs" / "rehearsal" / "documents" / "doc_kb" / "ocr",
            retained_ocr_identity=(variant.output.sha256, variant.output.size_bytes),
        )
    assert result.cache_variant_id == variant.variant_id
    assert result.provider_called is False
