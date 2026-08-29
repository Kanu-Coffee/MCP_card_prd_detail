from __future__ import annotations

import json
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest
from cardrag_core import (
    STABLE_POINTER_PATH,
    AdoptedOCRArtifactManifest,
    ArtifactRef,
    EmbeddingContract,
    GenerationCounts,
    GenerationDocument,
    GenerationManifest,
    GenerationPointer,
    GenerationReady,
    LegacyAdoptionReceiptV2,
    LegacyAdoptionValidationV2,
    NativeOCRContract,
    OCRArtifactManifest,
    OCRInput,
    OCRReady,
    WebDAVHTTPError,
    adopted_ocr_reuse_key,
    generation_database_path,
    generation_manifest_path,
    generation_ready_path,
    native_ocr_reuse_key,
    object_path,
    ocr_manifest_path,
    ocr_ready_path,
    sha256_bytes,
    verify_ocr_bytes,
)

from cardrag_worker.gc import GCError, GCPartialFailure, collect_garbage
from cardrag_worker.state import WorkerState

NOW = datetime(2026, 8, 25, tzinfo=UTC)


class FakeWebDAV:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.children: dict[str, tuple[PurePosixPath, ...]] = {}
        self.deleted: list[str] = []
        self.stable_reads = 0
        self.replace_stable_after_first_read = False
        self.replace_stable_after_reads: int | None = None
        self.delete_failures: dict[str, Exception] = {}

    async def get_bytes(self, path: str | PurePosixPath, *, max_bytes: int | None = None) -> bytes | None:
        key = str(path)
        if key == "v1/channels/stable.json":
            self.stable_reads += 1
            if (
                self.replace_stable_after_first_read
                and self.stable_reads > 1
                or self.replace_stable_after_reads is not None
                and self.stable_reads > self.replace_stable_after_reads
            ):
                return b'{"changed":true}'
        return self.objects.get(key)

    async def list_children(self, path: str | PurePosixPath) -> tuple[PurePosixPath, ...]:
        key = str(path)
        if key not in self.children:
            raise WebDAVHTTPError("PROPFIND", PurePosixPath(key), 404)
        return self.children[key]

    async def delete(self, path: str | PurePosixPath, *, missing_ok: bool = False) -> None:
        key = str(path)
        failure = self.delete_failures.get(key)
        if failure is not None:
            raise failure
        self.deleted.append(key)


def cache_control(*, cache_epoch: int, body: bytes) -> tuple[str, OCRArtifactManifest, OCRReady, ArtifactRef]:
    source = OCRInput(pdf_sha256=sha256_bytes(b"pdf"), pdf_size_bytes=3, page_count=1)
    contract = NativeOCRContract(
        schema_version="cardrag.ocr-contract.v1",
        processor_version="worker/1",
        cache_epoch=cache_epoch,
        prompt_version="prompt.v1",
        prompt_sha256=sha256_bytes(b"prompt"),
        renderer_id="renderer",
        render_scale_milli=3000,
        provider="codex-exec",
        model="gpt-5.4",
        reasoning_effort="high",
        chunk_pages=1,
    )
    verified = verify_ocr_bytes(body, expected_page_count=1)
    output = ArtifactRef.for_cas(
        sha256=verified.sha256,
        size_bytes=verified.size_bytes,
        media_type="text/markdown; charset=utf-8",
    )
    key = native_ocr_reuse_key(contract, source)
    manifest = OCRArtifactManifest(
        reuse_key=key,
        source=source,
        contract=contract,
        output=output,
        ocr_chars=verified.char_count,
        page_output_sha256=verified.page_sha256,
        created_at=NOW,
    )
    ready = OCRReady(
        reuse_key=key,
        manifest_sha256=sha256_bytes(manifest.canonical_bytes()),
        ocr_sha256=output.sha256,
    )
    return key, manifest, ready, output


def build_remote() -> tuple[FakeWebDAV, str, str, str]:
    webdav = FakeWebDAV()
    ocr_body = "## Page 1\n\n카드 혜택 조건과 제외 사항을 충분히 설명하는 본문입니다.\n".encode()
    active_key, active_manifest, active_ready, ocr = cache_control(cache_epoch=0, body=ocr_body)
    inactive_key, inactive_manifest, inactive_ready, _ = cache_control(cache_epoch=1, body=ocr_body)
    orphan_key = sha256_bytes(b"orphan-cache")
    pdf = ArtifactRef.for_cas(sha256=sha256_bytes(b"pdf"), size_bytes=3, media_type="application/pdf")
    generation_id = "g-current"
    database = ArtifactRef(
        sha256=sha256_bytes(b"db"),
        size_bytes=2,
        media_type="application/vnd.sqlite3",
        path=generation_database_path(generation_id).as_posix(),
    )
    manifest = GenerationManifest(
        generation_id=generation_id,
        created_at=NOW,
        serving_database=database,
        corpus_sha256="c" * 64,
        contract_sha256="d" * 64,
        embedding_contract=EmbeddingContract(provider="openrouter", model="embed", dimension=1536, count=1),
        issuer_codes=("kb",),
        counts=GenerationCounts(documents=1, pdf_objects=1, ocr_objects=1, chunks=1),
        documents=(
            GenerationDocument(
                document_id="doc_kb",
                issuer="kb",
                pdf=pdf,
                ocr=ocr,
                ocr_cache_kind="native",
                ocr_reuse_key=active_key,
                page_count=1,
            ),
        ),
    )
    manifest_body = manifest.canonical_bytes()
    ready = GenerationReady(
        generation_id=generation_id,
        manifest_sha256=sha256_bytes(manifest_body),
        serving_database_sha256=database.sha256,
        serving_database_size_bytes=database.size_bytes,
    )
    ready_body = ready.canonical_bytes()
    pointer = GenerationPointer(
        generation_id=generation_id,
        manifest_sha256=ready.manifest_sha256,
        ready_sha256=sha256_bytes(ready_body),
    )
    webdav.objects.update(
        {
            "v1/channels/stable.json": pointer.canonical_bytes(),
            generation_manifest_path(generation_id).as_posix(): manifest_body,
            generation_ready_path(generation_id).as_posix(): ready_body,
            ocr_manifest_path(active_key).as_posix(): active_manifest.canonical_bytes(),
            ocr_ready_path(active_key).as_posix(): active_ready.canonical_bytes(),
            ocr_manifest_path(inactive_key).as_posix(): inactive_manifest.canonical_bytes(),
            ocr_ready_path(inactive_key).as_posix(): inactive_ready.canonical_bytes(),
            ocr_manifest_path(orphan_key).as_posix(): b"{}",
        }
    )
    generations_root = PurePosixPath("v1/generations")
    current_root = generations_root / generation_id
    old_root = generations_root / "g-old"
    webdav.children[generations_root.as_posix()] = (current_root, old_root)
    webdav.children[current_root.as_posix()] = (
        current_root / "index.sqlite3",
        current_root / "manifest.json",
        current_root / "READY.json",
    )
    webdav.children[old_root.as_posix()] = (old_root / "manifest.json",)

    cas_root = PurePosixPath("v1/objects/sha256")
    references = (pdf, ocr)
    unused_sha = sha256_bytes(b"unused")
    all_shas = {reference.sha256 for reference in references} | {unused_sha}
    prefixes = tuple(sorted({cas_root / digest[:2] for digest in all_shas}, key=str))
    webdav.children[cas_root.as_posix()] = prefixes
    for prefix in prefixes:
        webdav.children[prefix.as_posix()] = tuple(
            object_path(digest) for digest in sorted(all_shas) if digest[:2] == prefix.name
        )

    native_root = PurePosixPath("v1/ocr-cache/native")
    keys = (active_key, inactive_key, orphan_key)
    cache_prefixes = tuple(sorted({native_root / key[:2] for key in keys}, key=str))
    webdav.children[native_root.as_posix()] = cache_prefixes
    for prefix in cache_prefixes:
        reuse_roots = tuple(prefix / key for key in sorted(keys) if key[:2] == prefix.name)
        webdav.children[prefix.as_posix()] = reuse_roots
        for reuse_root in reuse_roots:
            key = reuse_root.name
            children = [reuse_root / "manifest.json"]
            if key != orphan_key:
                children.append(reuse_root / "READY.json")
            webdav.children[reuse_root.as_posix()] = tuple(children)
    return webdav, active_key, inactive_key, unused_sha


def add_incoming_temp_leaves(webdav: FakeWebDAV) -> tuple[PurePosixPath, PurePosixPath]:
    root = PurePosixPath("v1", ".incoming")
    channels = root / "channels"
    publish = root / "publish"
    channel_leaf = channels / ("1" * 32 + ".tmp")
    publish_leaf = publish / ("2" * 32 + ".tmp")
    webdav.children[root.as_posix()] = (publish, channels)
    webdav.children[channels.as_posix()] = (channel_leaf,)
    webdav.children[publish.as_posix()] = (publish_leaf,)
    webdav.children[channel_leaf.as_posix()] = ()
    webdav.children[publish_leaf.as_posix()] = ()
    return channel_leaf, publish_leaf


@pytest.mark.asyncio
async def test_gc_marks_exact_cache_key_and_sweeps_generation_cache_then_cas(tmp_path: Path) -> None:
    webdav, active_key, inactive_key, unused_sha = build_remote()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        first = await collect_garbage(webdav=webdav, state=state, now=NOW)
        assert f"v1/ocr-cache/native/{active_key[:2]}/{active_key}" not in first.candidates
        assert f"v1/ocr-cache/native/{inactive_key[:2]}/{inactive_key}" in first.candidates
        assert object_path(unused_sha).as_posix() in first.candidates
        second = await collect_garbage(
            webdav=webdav,
            state=state,
            apply=True,
            now=NOW + timedelta(days=31),
        )
    assert second.deleted[0] == "v1/generations/g-old"
    cache_index = next(index for index, path in enumerate(second.deleted) if path.startswith("v1/ocr-cache"))
    object_index = next(index for index, path in enumerate(second.deleted) if path.startswith("v1/objects"))
    assert cache_index < object_index


@pytest.mark.asyncio
async def test_gc_incoming_temp_leaves_observe_grace_and_pointer_fence(tmp_path: Path) -> None:
    webdav, _, _, _ = build_remote()
    incoming = add_incoming_temp_leaves(webdav)
    with WorkerState(tmp_path / "state.sqlite3") as state:
        first = await collect_garbage(webdav=webdav, state=state, apply=True, now=NOW)
        assert set(path.as_posix() for path in incoming).issubset(first.candidates)
        assert not set(path.as_posix() for path in incoming).intersection(first.deleted)

        second = await collect_garbage(
            webdav=webdav,
            state=state,
            apply=True,
            now=NOW + timedelta(days=31),
        )

    assert second.deleted[:2] == tuple(path.as_posix() for path in incoming)
    assert webdav.deleted[:2] == list(second.deleted[:2])


@pytest.mark.asyncio
async def test_gc_incoming_path_ambiguity_fails_before_any_delete(tmp_path: Path) -> None:
    webdav, _, _, _ = build_remote()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        await collect_garbage(webdav=webdav, state=state, now=NOW)
        root = PurePosixPath("v1", ".incoming")
        publish = root / "publish"
        webdav.children[root.as_posix()] = (publish,)
        webdav.children[publish.as_posix()] = (publish / "not-a-publisher-uuid.tmp",)

        with pytest.raises(GCError, match="unexpected incoming temporary path"):
            await collect_garbage(
                webdav=webdav,
                state=state,
                apply=True,
                now=NOW + timedelta(days=31),
            )

    assert webdav.deleted == []


@pytest.mark.asyncio
async def test_gc_incoming_collection_shape_fails_closed(tmp_path: Path) -> None:
    webdav, _, _, _ = build_remote()
    leaf, _ = add_incoming_temp_leaves(webdav)
    webdav.children[leaf.as_posix()] = (leaf / "nested",)

    with (
        WorkerState(tmp_path / "state.sqlite3") as state,
        pytest.raises(GCError, match="not a leaf"),
    ):
        await collect_garbage(webdav=webdav, state=state, apply=True, now=NOW)

    assert webdav.deleted == []


@pytest.mark.asyncio
async def test_gc_ignores_known_nested_object_upload_namespace(tmp_path: Path) -> None:
    webdav, _, _, _ = build_remote()
    incoming = add_incoming_temp_leaves(webdav)
    root = PurePosixPath("v1", ".incoming")
    objects = root / "objects"
    webdav.children[root.as_posix()] = (*webdav.children[root.as_posix()], objects)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        result = await collect_garbage(webdav=webdav, state=state, now=NOW)

    assert set(path.as_posix() for path in incoming).issubset(result.candidates)
    assert objects.as_posix() not in result.candidates


@pytest.mark.asyncio
async def test_gc_partial_delete_failure_reports_only_known_safe_count(tmp_path: Path) -> None:
    webdav, _, _, _ = build_remote()
    incoming = add_incoming_temp_leaves(webdav)
    raw_sentinel = "RAW_DELETE_URL_TOKEN_SECRET"
    with WorkerState(tmp_path / "state.sqlite3") as state:
        await collect_garbage(webdav=webdav, state=state, now=NOW)
        webdav.delete_failures[incoming[1].as_posix()] = RuntimeError(raw_sentinel)

        with pytest.raises(GCPartialFailure) as captured:
            await collect_garbage(
                webdav=webdav,
                state=state,
                apply=True,
                now=NOW + timedelta(days=31),
            )

    error = captured.value
    assert error.deleted_count == 1
    assert error.reason_code == "remote_gc_partial_failure"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert raw_sentinel not in str(error)
    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert raw_sentinel not in rendered
    assert webdav.deleted == [incoming[0].as_posix()]


@pytest.mark.asyncio
async def test_gc_parses_and_marks_retained_v2_adopted_cache(tmp_path: Path) -> None:
    webdav, _, _, _ = build_remote()
    pointer = GenerationPointer.model_validate_json(webdav.objects[STABLE_POINTER_PATH.as_posix()])
    generation_path = generation_manifest_path(pointer.generation_id).as_posix()
    generation = GenerationManifest.model_validate_json(webdav.objects[generation_path])
    document = generation.documents[0]
    assert document.ocr is not None
    source = OCRInput(
        pdf_sha256=document.pdf.sha256,
        pdf_size_bytes=document.pdf.size_bytes,
        page_count=document.page_count,
    )
    document_id = document.document_id
    key = adopted_ocr_reuse_key(
        adoption_policy_version="cardrag.legacy-ocr-adoption.v2",
        source_document_id=document_id,
        pdf_sha256=source.pdf_sha256,
    )
    receipt = LegacyAdoptionReceiptV2(
        source_bundle_id="bundle-v2",
        source_bundle_sha256="e" * 64,
        source_database_id="legacy-data-kit",
        source_document_id=document_id,
        pdf_sha256=source.pdf_sha256,
        source_ocr_sha256=document.ocr.sha256,
        source_ocr_size_bytes=document.ocr.size_bytes,
        normalized_ocr_sha256=document.ocr.sha256,
        normalized_ocr_size_bytes=document.ocr.size_bytes,
        normalization_profile="exact",
        prefix_sha256=None,
        removed_bytes=0,
        validation=LegacyAdoptionValidationV2(
            source_hash_verified=True,
            normalized_hash_verified=True,
            transformation_verified=True,
            page_coverage_verified=True,
            utf8_verified=True,
            ledger_bound=True,
        ),
    )
    adopted = AdoptedOCRArtifactManifest(
        schema_version="cardrag.ocr-artifact.v2",
        validation_profile="cardrag.legacy-ocr-adoption.v2",
        reuse_key=key,
        source=source,
        receipt=receipt,
        output=document.ocr,
        ocr_chars=32,
        page_output_sha256=("f" * 64,),
        created_at=NOW,
    )
    adopted_ready = OCRReady(
        reuse_key=key,
        manifest_sha256=sha256_bytes(adopted.canonical_bytes()),
        ocr_sha256=document.ocr.sha256,
    )
    adopted_document = GenerationDocument(
        document_id=document.document_id,
        issuer=document.issuer,
        pdf=document.pdf,
        ocr=document.ocr,
        ocr_cache_kind="adopted",
        ocr_reuse_key=key,
        page_count=document.page_count,
    )
    updated_generation = GenerationManifest.model_validate(
        {**generation.model_dump(mode="python"), "documents": (adopted_document,)}
    )
    generation_body = updated_generation.canonical_bytes()
    database = updated_generation.serving_database
    generation_ready = GenerationReady(
        generation_id=updated_generation.generation_id,
        manifest_sha256=sha256_bytes(generation_body),
        serving_database_sha256=database.sha256,
        serving_database_size_bytes=database.size_bytes,
    )
    ready_body = generation_ready.canonical_bytes()
    webdav.objects[generation_path] = generation_body
    webdav.objects[generation_ready_path(pointer.generation_id).as_posix()] = ready_body
    webdav.objects[STABLE_POINTER_PATH.as_posix()] = GenerationPointer(
        generation_id=pointer.generation_id,
        manifest_sha256=generation_ready.manifest_sha256,
        ready_sha256=sha256_bytes(ready_body),
    ).canonical_bytes()
    webdav.objects[ocr_manifest_path(key, kind="adopted").as_posix()] = adopted.canonical_bytes()
    webdav.objects[ocr_ready_path(key, kind="adopted").as_posix()] = adopted_ready.canonical_bytes()
    adopted_root = PurePosixPath("v1/ocr-cache/adopted")
    prefix = adopted_root / key[:2]
    reuse_root = prefix / key
    webdav.children[adopted_root.as_posix()] = (prefix,)
    webdav.children[prefix.as_posix()] = (reuse_root,)
    webdav.children[reuse_root.as_posix()] = (
        reuse_root / "manifest.json",
        reuse_root / "READY.json",
    )

    with WorkerState(tmp_path / "state.sqlite3") as state:
        result = await collect_garbage(webdav=webdav, state=state, now=NOW)

    assert reuse_root.as_posix() not in result.candidates


@pytest.mark.asyncio
async def test_gc_pointer_fence_deletes_nothing(tmp_path: Path) -> None:
    webdav, _, _, _ = build_remote()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        await collect_garbage(webdav=webdav, state=state, now=NOW)
        webdav.stable_reads = 0
        webdav.replace_stable_after_first_read = True
        with pytest.raises(GCError, match="deleted 0"):
            await collect_garbage(
                webdav=webdav,
                state=state,
                apply=True,
                now=NOW + timedelta(days=31),
            )
    assert webdav.deleted == []


@pytest.mark.asyncio
async def test_gc_pointer_change_after_one_delete_raises_safe_partial_count(tmp_path: Path) -> None:
    webdav, _, _, _ = build_remote()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        await collect_garbage(webdav=webdav, state=state, now=NOW)
        webdav.stable_reads = 0
        webdav.replace_stable_after_reads = 3
        with pytest.raises(GCPartialFailure) as captured:
            await collect_garbage(
                webdav=webdav,
                state=state,
                apply=True,
                now=NOW + timedelta(days=31),
            )

    assert captured.value.deleted_count == 1
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert webdav.deleted == ["v1/generations/g-old"]


@pytest.mark.asyncio
async def test_gc_unreferenced_corrupt_committed_cache_observes_grace_before_delete(
    tmp_path: Path,
) -> None:
    webdav, _, _, _ = build_remote()
    broken_key = sha256_bytes(b"broken-committed")
    root = PurePosixPath("v1/ocr-cache/native", broken_key[:2], broken_key)
    prefix = root.parent
    webdav.children["v1/ocr-cache/native"] += (prefix,)
    webdav.children[prefix.as_posix()] = (root,)
    webdav.children[root.as_posix()] = (root / "READY.json",)
    webdav.objects[(root / "READY.json").as_posix()] = b"{}"
    with WorkerState(tmp_path / "state.sqlite3") as state:
        first = await collect_garbage(webdav=webdav, state=state, apply=True, now=NOW)
        assert root.as_posix() in first.candidates
        assert root.as_posix() not in first.deleted
        second = await collect_garbage(
            webdav=webdav,
            state=state,
            apply=True,
            now=NOW + timedelta(days=31),
        )
    assert root.as_posix() in second.deleted


@pytest.mark.asyncio
async def test_gc_retained_committed_cache_corruption_fails_closed(tmp_path: Path) -> None:
    webdav, active_key, _, _ = build_remote()
    active_manifest = ocr_manifest_path(active_key).as_posix()
    del webdav.objects[active_manifest]

    with (
        WorkerState(tmp_path / "state.sqlite3") as state,
        pytest.raises(GCError, match="retained native OCR cache.*has no manifest"),
    ):
        await collect_garbage(webdav=webdav, state=state, apply=True, now=NOW)
    assert webdav.deleted == []


@pytest.mark.asyncio
async def test_gc_rejects_noncanonical_stable_pointer_before_delete(tmp_path: Path) -> None:
    webdav, _, _, _ = build_remote()
    stable_path = STABLE_POINTER_PATH.as_posix()
    payload = json.loads(webdav.objects[stable_path])
    webdav.objects[stable_path] = json.dumps(payload, indent=2).encode()

    with (
        WorkerState(tmp_path / "state.sqlite3") as state,
        pytest.raises(GCError, match="stable pointer is not canonical"),
    ):
        await collect_garbage(webdav=webdav, state=state, apply=True, now=NOW)
    assert webdav.deleted == []


@pytest.mark.asyncio
async def test_gc_rejects_noncanonical_generation_controls_even_when_rebound(tmp_path: Path) -> None:
    webdav, _, _, _ = build_remote()
    stable_path = STABLE_POINTER_PATH.as_posix()
    pointer = GenerationPointer.model_validate_json(webdav.objects[stable_path])
    manifest_path = generation_manifest_path(pointer.generation_id).as_posix()
    ready_path = generation_ready_path(pointer.generation_id).as_posix()
    manifest_payload = json.loads(webdav.objects[manifest_path])
    noncanonical_manifest = json.dumps(manifest_payload, indent=2).encode()
    ready = GenerationReady.model_validate_json(webdav.objects[ready_path]).model_copy(
        update={"manifest_sha256": sha256_bytes(noncanonical_manifest)}
    )
    ready_body = ready.canonical_bytes()
    rebound_pointer = pointer.model_copy(
        update={
            "manifest_sha256": ready.manifest_sha256,
            "ready_sha256": sha256_bytes(ready_body),
        }
    )
    webdav.objects[manifest_path] = noncanonical_manifest
    webdav.objects[ready_path] = ready_body
    webdav.objects[stable_path] = rebound_pointer.canonical_bytes()

    with (
        WorkerState(tmp_path / "state.sqlite3") as state,
        pytest.raises(GCError, match="control JSON is not canonical"),
    ):
        await collect_garbage(webdav=webdav, state=state, apply=True, now=NOW)
    assert webdav.deleted == []


@pytest.mark.asyncio
async def test_gc_rejects_generation_predecessor_cycle_before_delete(tmp_path: Path) -> None:
    webdav, _, _, _ = build_remote()
    stable_path = STABLE_POINTER_PATH.as_posix()
    pointer = GenerationPointer.model_validate_json(webdav.objects[stable_path])
    current_id = pointer.generation_id
    current_manifest_path = generation_manifest_path(current_id).as_posix()
    current_ready_path = generation_ready_path(current_id).as_posix()
    original = GenerationManifest.model_validate_json(webdav.objects[current_manifest_path])
    second_id = "g-second"

    current = original.model_copy(update={"previous_generation_id": second_id})
    current_body = current.canonical_bytes()
    current_ready = GenerationReady(
        generation_id=current_id,
        manifest_sha256=sha256_bytes(current_body),
        serving_database_sha256=current.serving_database.sha256,
        serving_database_size_bytes=current.serving_database.size_bytes,
    )
    current_ready_body = current_ready.canonical_bytes()
    second_database = ArtifactRef(
        sha256=sha256_bytes(b"second-db"),
        size_bytes=len(b"second-db"),
        media_type="application/vnd.sqlite3",
        path=generation_database_path(second_id).as_posix(),
    )
    second = original.model_copy(
        update={
            "generation_id": second_id,
            "serving_database": second_database,
            "previous_generation_id": current_id,
        }
    )
    second_body = second.canonical_bytes()
    second_ready = GenerationReady(
        generation_id=second_id,
        manifest_sha256=sha256_bytes(second_body),
        serving_database_sha256=second_database.sha256,
        serving_database_size_bytes=second_database.size_bytes,
    )
    second_root = PurePosixPath("v1/generations", second_id)
    webdav.objects.update(
        {
            current_manifest_path: current_body,
            current_ready_path: current_ready_body,
            generation_manifest_path(second_id).as_posix(): second_body,
            generation_ready_path(second_id).as_posix(): second_ready.canonical_bytes(),
            stable_path: GenerationPointer(
                generation_id=current_id,
                manifest_sha256=current_ready.manifest_sha256,
                ready_sha256=sha256_bytes(current_ready_body),
            ).canonical_bytes(),
        }
    )
    webdav.children["v1/generations"] += (second_root,)
    webdav.children[second_root.as_posix()] = (
        second_root / "index.sqlite3",
        second_root / "manifest.json",
        second_root / "READY.json",
    )

    with (
        WorkerState(tmp_path / "state.sqlite3") as state,
        pytest.raises(GCError, match="predecessor chain contains a cycle"),
    ):
        await collect_garbage(
            webdav=webdav,
            state=state,
            apply=True,
            retain_generations=3,
            now=NOW,
        )
    assert webdav.deleted == []
