from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest
from cardrag_core import (
    ArtifactRef,
    ContentOCRArtifactManifest,
    NativeOCRContract,
    OCRArtifactManifest,
    OCRInput,
    canonical_json_bytes,
    content_addressed_ocr_reuse_key,
    native_ocr_reuse_key,
    object_path,
    sha256_bytes,
    verify_ocr_bytes,
)

from cardrag_worker.content_cache import (
    ContentCacheValidationError,
    ContentOCRVariantStore,
    content_index_path,
)
from cardrag_worker.ocr import OCRCacheMissError, OCRResolver, OCRResult
from cardrag_worker.state import WorkerState


class FakeWebDAV:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.list_calls = 0
        self.put_order: list[str] = []

    async def list_children(self, path: PurePosixPath) -> tuple[PurePosixPath, ...]:
        self.list_calls += 1
        return tuple(
            sorted(PurePosixPath(name) for name in self.objects if PurePosixPath(name).parent == path)
        )

    async def get_bytes(self, path: str | PurePosixPath, *, max_bytes: int | None = None) -> bytes | None:
        body = self.objects.get(str(path))
        if body is not None and max_bytes is not None and len(body) > max_bytes:
            raise ValueError("object exceeds maximum")
        return body

    async def put_bytes(self, path: PurePosixPath, body: bytes, *, content_type: str) -> None:
        name = str(path)
        if name in self.objects and self.objects[name] != body:
            raise ValueError("immutable collision")
        self.objects[name] = body
        self.put_order.append(name)

    async def put_cas(self, body: bytes, *, media_type: str) -> tuple[str, str]:
        digest = sha256_bytes(body)
        path = str(object_path(digest))
        self.objects[path] = body
        self.put_order.append(path)
        return digest, path


def _provenance(provider: str = "codex-exec") -> NativeOCRContract:
    return NativeOCRContract(
        processor_version="worker/1",
        prompt_version="ocr/v1",
        prompt_sha256=sha256_bytes(b"prompt"),
        renderer_id="pdfium",
        render_scale_milli=6000,
        provider=provider,
        model="test-model",
        reasoning_effort="medium",
        segmentation_strategy_id="windowed",
        whole_document_max_pages=4,
        target_pages_per_call=2,
        context_pages_before=1,
        context_pages_after=1,
        output_policy="target-pages-only",
    )


def _variant(*, created_at: datetime, provider: str, text: str) -> tuple[ContentOCRArtifactManifest, bytes]:
    source = OCRInput(pdf_sha256=sha256_bytes(b"pdf"), pdf_size_bytes=3, page_count=1)
    body = f"## Page 1\n\n{text}\n".encode()
    verified = verify_ocr_bytes(body, expected_page_count=1)
    manifest = ContentOCRArtifactManifest.create(
        source=source,
        cache_epoch=0,
        output=ArtifactRef.for_cas(
            sha256=verified.sha256,
            size_bytes=verified.size_bytes,
            media_type="text/markdown; charset=utf-8",
        ),
        ocr_chars=verified.char_count,
        page_output_sha256=verified.page_sha256,
        created_at=created_at,
        provenance=_provenance(provider),
    )
    return manifest, body


@pytest.mark.asyncio
async def test_content_variants_publish_select_latest_and_freeze_run(tmp_path: Path) -> None:
    webdav = FakeWebDAV()
    store = ContentOCRVariantStore(webdav=webdav, state_root=tmp_path)  # type: ignore[arg-type]
    earlier, earlier_body = _variant(
        created_at=datetime(2026, 10, 6, tzinfo=UTC), provider="codex-exec", text="earlier text"
    )
    later, later_body = _variant(
        created_at=datetime(2026, 10, 6, tzinfo=UTC) + timedelta(hours=1),
        provider="opencode",
        text="later text",
    )
    await store.publish(earlier, earlier_body)
    assert webdav.put_order[-1] == str(content_index_path(earlier))
    await store.freeze("run1")
    assert webdav.list_calls == 1
    first = await store.lookup(run_id="run1", source=earlier.source, cache_epoch=0)
    assert first is not None and first.manifest.variant_id == earlier.variant_id

    await store.publish(later, later_body)
    frozen = await store.lookup(run_id="run1", source=earlier.source, cache_epoch=0)
    assert frozen is not None and frozen.manifest.variant_id == earlier.variant_id
    next_run = await store.lookup(run_id="run2", source=earlier.source, cache_epoch=0)
    assert next_run is not None and next_run.manifest.variant_id == later.variant_id
    assert next_run.verified.sha256 == sha256_bytes(later_body)
    assert webdav.list_calls == 2

    resumed = ContentOCRVariantStore(webdav=webdav, state_root=tmp_path)  # type: ignore[arg-type]
    resume_hit = await resumed.lookup(run_id="run1", source=earlier.source, cache_epoch=0)
    assert resume_hit is not None and resume_hit.manifest.variant_id == earlier.variant_id
    assert webdav.list_calls == 2


@pytest.mark.asyncio
async def test_content_variant_rejects_corrupt_latest_and_epoch_miss(tmp_path: Path) -> None:
    webdav = FakeWebDAV()
    store = ContentOCRVariantStore(webdav=webdav, state_root=tmp_path)  # type: ignore[arg-type]
    earlier, earlier_body = _variant(
        created_at=datetime(2026, 10, 6, tzinfo=UTC), provider="codex-exec", text="valid text"
    )
    later, later_body = _variant(
        created_at=datetime(2026, 10, 6, tzinfo=UTC) + timedelta(hours=1),
        provider="opencode",
        text="invalidated text",
    )
    await store.publish(earlier, earlier_body)
    await store.publish(later, later_body)
    webdav.objects[str(later.variant_root / "READY.json")] = b"{}"
    with pytest.raises(ContentCacheValidationError):
        await store.verify_all()
    hit = await store.lookup(run_id="run3", source=earlier.source, cache_epoch=0)
    assert hit is not None and hit.manifest.variant_id == earlier.variant_id
    assert await store.lookup(run_id="run3", source=earlier.source, cache_epoch=1) is None
    assert content_addressed_ocr_reuse_key(earlier.source, cache_epoch=0) == earlier.reuse_key

    index = content_index_path(earlier)
    webdav.objects[str(index)] = canonical_json_bytes({"tampered": True})
    new_run = await store.lookup(run_id="run4", source=earlier.source, cache_epoch=0)
    assert new_run is None


@pytest.mark.asyncio
async def test_document_selection_survives_resume_and_fails_closed_if_variant_changes(tmp_path: Path) -> None:
    webdav = FakeWebDAV()
    store = ContentOCRVariantStore(webdav=webdav, state_root=tmp_path)  # type: ignore[arg-type]
    earlier, earlier_body = _variant(
        created_at=datetime(2026, 10, 6, tzinfo=UTC), provider="codex-exec", text="first text"
    )
    later, later_body = _variant(
        created_at=datetime(2026, 10, 6, tzinfo=UTC) + timedelta(hours=1),
        provider="opencode",
        text="latest text",
    )
    await store.publish(earlier, earlier_body)
    await store.publish(later, later_body)
    first = await store.lookup(
        run_id="run-select", document_id="doc_test", source=earlier.source, cache_epoch=0
    )
    assert first is not None and first.manifest.variant_id == later.variant_id
    selection = tmp_path / "runs" / "run-select" / "content-ocr-selections" / "doc_test.json"
    assert selection.is_file()

    resumed = ContentOCRVariantStore(webdav=webdav, state_root=tmp_path)  # type: ignore[arg-type]
    again = await resumed.lookup(
        run_id="run-select", document_id="doc_test", source=earlier.source, cache_epoch=0
    )
    assert again is not None and again.manifest.variant_id == later.variant_id

    webdav.objects[str(later.variant_root / "READY.json")] = b"{}"
    with pytest.raises(ContentCacheValidationError, match="frozen content selection"):
        await resumed.lookup(
            run_id="run-select", document_id="doc_test", source=earlier.source, cache_epoch=0
        )


@pytest.mark.asyncio
async def test_transition_pin_preserves_each_documents_served_ocr(tmp_path: Path) -> None:
    class NoCallProvider:
        provider = "opencode"
        model = "alibaba-token-plan/qwen3.8-flash"
        reasoning_effort = "medium"

        async def recognize(self, *_args: object, **_kwargs: object) -> str:
            raise AssertionError("transition must use a verified content variant")

    webdav = FakeWebDAV()
    earlier, earlier_body = _variant(
        created_at=datetime(2026, 10, 6, tzinfo=UTC), provider="codex-exec", text="served old text"
    )
    later, later_body = _variant(
        created_at=datetime(2026, 10, 6, tzinfo=UTC) + timedelta(hours=1),
        provider="opencode",
        text="another document's text",
    )
    store = ContentOCRVariantStore(webdav=webdav, state_root=tmp_path)  # type: ignore[arg-type]
    await store.publish(earlier, earlier_body)
    await store.publish(later, later_body)
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
        old = await resolver.resolve(
            run_id="run-old",
            document_id="doc_old",
            pdf_path=pdf_path,
            pdf_sha256=earlier.source.pdf_sha256,
            pdf_size_bytes=3,
            page_count=1,
            output_dir=tmp_path / "runs" / "run-old" / "documents" / "doc_old" / "ocr",
            retained_ocr_identity=(earlier.output.sha256, earlier.output.size_bytes),
        )
        latest = await resolver.resolve(
            run_id="run-latest",
            document_id="doc_latest",
            pdf_path=pdf_path,
            pdf_sha256=earlier.source.pdf_sha256,
            pdf_size_bytes=3,
            page_count=1,
            output_dir=tmp_path / "runs" / "run-latest" / "documents" / "doc_latest" / "ocr",
        )
        with pytest.raises(OCRCacheMissError, match="retained OCR"):
            await resolver.resolve(
                run_id="run-missing",
                document_id="doc_missing",
                pdf_path=pdf_path,
                pdf_sha256=earlier.source.pdf_sha256,
                pdf_size_bytes=3,
                page_count=1,
                output_dir=tmp_path / "runs" / "run-missing" / "documents" / "doc_missing" / "ocr",
                retained_ocr_identity=("f" * 64, 1),
            )
    assert old.cache_variant_id == earlier.variant_id
    assert latest.cache_variant_id == later.variant_id


@pytest.mark.asyncio
async def test_resolver_reuses_other_provider_content_without_invocation(tmp_path: Path) -> None:
    class OtherProvider:
        provider = "opencode"
        model = "different-model"
        reasoning_effort = "medium"

        async def recognize(self, *_args: object, **_kwargs: object) -> str:
            raise AssertionError("content hit must avoid provider invocation")

    webdav = FakeWebDAV()
    manifest, body = _variant(
        created_at=datetime(2026, 10, 6, tzinfo=UTC),
        provider="codex-exec",
        text="verified OCR text from a different provider",
    )
    await ContentOCRVariantStore(webdav=webdav, state_root=tmp_path).publish(  # type: ignore[arg-type]
        manifest, body
    )
    pdf_path = tmp_path / "source.pdf"
    pdf_path.write_bytes(b"pdf")
    with WorkerState(tmp_path / "state.sqlite3") as state:
        resolver = OCRResolver(
            provider=OtherProvider(),  # type: ignore[arg-type]
            state=state,
            webdav=webdav,  # type: ignore[arg-type]
            cache_mode="read-only",
            require_cache_hit=True,
        )
        await resolver.freeze_content_snapshot("run1")
        result = await resolver.resolve(
            run_id="run1",
            document_id="doc_test",
            pdf_path=pdf_path,
            pdf_sha256=manifest.source.pdf_sha256,
            pdf_size_bytes=manifest.source.pdf_size_bytes,
            page_count=manifest.source.page_count,
            output_dir=tmp_path / "runs" / "run1" / "documents" / "doc_test" / "ocr",
        )
    assert result.cache_kind == "content"
    assert result.cache_variant_id == manifest.variant_id
    assert result.ocr_sha256 == manifest.output.sha256
    assert result.provider_called is False
    assert result.cache_reused is True


@pytest.mark.asyncio
async def test_native_local_result_publishes_content_variant_for_next_provider(tmp_path: Path) -> None:
    class Provider:
        provider = "codex-exec"
        model = "test-model"
        reasoning_effort = "medium"

        async def recognize(self, *_args: object, **_kwargs: object) -> str:
            raise AssertionError("this test commits a verified local result")

    webdav = FakeWebDAV()
    source = OCRInput(pdf_sha256=sha256_bytes(b"pdf"), pdf_size_bytes=3, page_count=1)
    body = b"## Page 1\n\nVerified OCR text published into content cache.\n"
    verified = verify_ocr_bytes(body, expected_page_count=1)
    output = ArtifactRef.for_cas(
        sha256=verified.sha256,
        size_bytes=verified.size_bytes,
        media_type="text/markdown; charset=utf-8",
    )
    with WorkerState(tmp_path / "state.sqlite3") as state:
        resolver = OCRResolver(
            provider=Provider(),  # type: ignore[arg-type]
            state=state,
            webdav=webdav,  # type: ignore[arg-type]
            cache_mode="read-write",
        )
        native_key = native_ocr_reuse_key(resolver.contract, source)
        native_manifest = OCRArtifactManifest(
            reuse_key=native_key,
            source=source,
            contract=resolver.contract,
            output=output,
            ocr_chars=verified.char_count,
            page_output_sha256=verified.page_sha256,
            created_at=datetime(2026, 10, 6, tzinfo=UTC),
        )
        local = OCRResult(
            pages=(verified.pages[0].partition("\n")[2].strip(),),
            ocr_bytes=body,
            ocr_text=verified.text,
            ocr_sha256=verified.sha256,
            size_bytes=verified.size_bytes,
            provenance="native",
            provider="codex-exec",
            model="test-model",
            reuse_key=native_key,
            provider_called=True,
        )
        committed = await resolver._commit_local_native(
            result=local,
            manifest=native_manifest,
            body=body,
            output_dir=tmp_path / "runs" / "run1" / "documents" / "doc_test" / "ocr",
            source_document_id="doc_test",
        )
        assert committed.cache_kind == "content"
        assert committed.cache_variant_id is not None
        assert committed.cache_reuse_key == content_addressed_ocr_reuse_key(source)
        assert (
            str(
                content_index_path(
                    ContentOCRArtifactManifest.create(
                        source=source,
                        cache_epoch=0,
                        output=output,
                        ocr_chars=verified.char_count,
                        page_output_sha256=verified.page_sha256,
                        created_at=native_manifest.created_at,
                        provenance=resolver.contract,
                    )
                )
            )
            in webdav.objects
        )


@pytest.mark.asyncio
async def test_provider_change_hits_content_after_first_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CountingProvider:
        reasoning_effort = "medium"

        def __init__(self, provider: str) -> None:
            self.provider = provider
            self.model = f"{provider}/test-model"
            self.calls = 0

        async def recognize(self, *_args: object, **_kwargs: object) -> str:
            self.calls += 1
            return "## Page 1\nThis card has a verified annual fee and discount condition."

    def render(_pdf_path: Path, output_dir: Path, *, scale: float) -> tuple[Path, ...]:
        output_dir.mkdir(parents=True, exist_ok=True)
        image = output_dir / "page-0001.png"
        image.write_bytes(b"fake-png")
        return (image,)

    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", render)
    webdav = FakeWebDAV()
    pdf_path = tmp_path / "source.pdf"
    pdf_path.write_bytes(b"pdf")
    provider_a = CountingProvider("codex-exec")
    provider_b = CountingProvider("opencode")
    with WorkerState(tmp_path / "state.sqlite3") as state:
        first_resolver = OCRResolver(
            provider=provider_a,  # type: ignore[arg-type]
            state=state,
            webdav=webdav,  # type: ignore[arg-type]
        )
        state.start_run(run_id="run1")
        await first_resolver.freeze_content_snapshot("run1")
        first = await first_resolver.resolve(
            run_id="run1",
            document_id="doc_test",
            pdf_path=pdf_path,
            pdf_sha256=sha256_bytes(b"pdf"),
            pdf_size_bytes=3,
            page_count=1,
            output_dir=tmp_path / "runs" / "run1" / "documents" / "doc_test" / "ocr",
        )
        assert first.cache_kind == "content"
        assert first.provider_called is True
        assert provider_a.calls == 1

        state.finish_run("run1", "succeeded")
        state.start_run(run_id="run2")
        second_resolver = OCRResolver(
            provider=provider_b,  # type: ignore[arg-type]
            state=state,
            webdav=webdav,  # type: ignore[arg-type]
        )
        await second_resolver.freeze_content_snapshot("run2")
        second = await second_resolver.resolve(
            run_id="run2",
            document_id="doc_test",
            pdf_path=pdf_path,
            pdf_sha256=sha256_bytes(b"pdf"),
            pdf_size_bytes=3,
            page_count=1,
            output_dir=tmp_path / "runs" / "run2" / "documents" / "doc_test" / "ocr",
        )
    assert provider_b.calls == 0
    assert second.cache_reused is True
    assert second.cache_variant_id == first.cache_variant_id
    assert second.ocr_sha256 == first.ocr_sha256


@pytest.mark.asyncio
async def test_reprocess_bypasses_cache_once_and_resume_reuses_request_variant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CountingProvider:
        provider = "opencode"
        model = "alibaba-token-plan/qwen3.8-flash"
        reasoning_effort = "medium"

        def __init__(self) -> None:
            self.calls = 0

        async def recognize(self, *_args: object, **_kwargs: object) -> str:
            self.calls += 1
            return "## Page 1\nA newly requested OCR result with different text."

    def render(_pdf_path: Path, output_dir: Path, *, scale: float) -> tuple[Path, ...]:
        output_dir.mkdir(parents=True, exist_ok=True)
        image = output_dir / "page-0001.png"
        image.write_bytes(b"fake-png")
        return (image,)

    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", render)
    webdav = FakeWebDAV()
    old, old_body = _variant(
        created_at=datetime(2026, 10, 6, tzinfo=UTC),
        provider="codex-exec",
        text="Previously served OCR text with enough characters for strict verification.",
    )
    await ContentOCRVariantStore(webdav=webdav, state_root=tmp_path).publish(  # type: ignore[arg-type]
        old, old_body
    )
    pdf_path = tmp_path / "source.pdf"
    pdf_path.write_bytes(b"pdf")
    provider = CountingProvider()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        resolver = OCRResolver(provider=provider, state=state, webdav=webdav)  # type: ignore[arg-type]
        state.start_run(run_id="reprocess1")
        first = await resolver.resolve(
            run_id="reprocess1",
            document_id="doc_test",
            pdf_path=pdf_path,
            pdf_sha256=old.source.pdf_sha256,
            pdf_size_bytes=3,
            page_count=1,
            output_dir=tmp_path / "runs" / "reprocess1" / "documents" / "doc_test" / "ocr",
            reprocess_request_id="ocr-request-test",
        )
        state.finish_run("reprocess1", "succeeded")
        state.start_run(run_id="reprocess2")
        resumed = await resolver.resolve(
            run_id="reprocess2",
            document_id="doc_test",
            pdf_path=pdf_path,
            pdf_sha256=old.source.pdf_sha256,
            pdf_size_bytes=3,
            page_count=1,
            output_dir=tmp_path / "runs" / "reprocess2" / "documents" / "doc_test" / "ocr",
            reprocess_request_id="ocr-request-test",
        )
    assert provider.calls == 1
    assert first.provider_called is True
    assert first.cache_variant_id != old.variant_id
    assert resumed.cache_variant_id == first.cache_variant_id
    assert resumed.provider_called is False


@pytest.mark.asyncio
async def test_restore_promotes_historical_text_without_changing_original(tmp_path: Path) -> None:
    webdav = FakeWebDAV()
    store = ContentOCRVariantStore(webdav=webdav, state_root=tmp_path)  # type: ignore[arg-type]
    old, old_body = _variant(
        created_at=datetime(2026, 10, 6, tzinfo=UTC), provider="codex-exec", text="old OCR text"
    )
    recent, recent_body = _variant(
        created_at=datetime(2026, 10, 7, tzinfo=UTC), provider="opencode", text="new OCR text"
    )
    await store.publish(old, old_body)
    await store.publish(recent, recent_body)
    assert (
        await store.has_verified_variant_after(
            run_id="before-restore",
            source=old.source,
            cache_epoch=0,
            cutoff=recent.created_at,
        )
        is False
    )
    restored = await store.restore_variant(source=old.source, cache_epoch=0, variant_id=old.variant_id)
    assert restored.restored_from == old.variant_id
    assert restored.output == old.output
    assert restored.created_at > recent.created_at
    assert (
        await store.has_verified_variant_after(
            run_id="after-restore",
            source=old.source,
            cache_epoch=0,
            cutoff=recent.created_at,
        )
        is True
    )
    assert webdav.objects[str(old.variant_root / "manifest.json")] == old.canonical_bytes()
    latest = await store.lookup(run_id="after-restore", source=old.source, cache_epoch=0)
    assert latest is not None and latest.manifest.variant_id == restored.variant_id
    assert latest.body == old_body
