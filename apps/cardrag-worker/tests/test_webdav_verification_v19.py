from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit
from xml.sax.saxutils import escape

import httpx
import pytest
from cardrag_core import (
    EMBEDDING_VIEW_TYPES,
    ArtifactRef,
    EmbeddingContract,
    EmbeddingProfile,
    EmbeddingVectorSidecar,
    EmbeddingViewCount,
    GenerationCounts,
    GenerationDocument,
    GenerationManifest,
    GenerationPointer,
    GenerationReady,
    IssuerOCRCounts,
    IssuerParserProfile,
    StructureContract,
    StructureMajorClassCounts,
    StructureNodeCounts,
    StructureRevisionCounts,
    StructureSourceCoverage,
    WebDAVIntegrityError,
    WebDAVSettings,
    canonical_sha256,
    generation_database_path,
    generation_manifest_path,
    generation_ready_path,
    generation_vectors_path,
    object_path,
    sha256_bytes,
)
from cardrag_core import WebDAVClient as CoreWebDAVClient
from cardrag_core import (
    WebDAVError as CoreWebDAVError,
)
from cardrag_core.webdav import WebDAVReadbackIntegrityError
from pydantic import SecretStr

from cardrag_worker.async_utils import to_thread_fenced
from cardrag_worker.settings import PublicationResumeSettings, WebDAVVerificationSettings, WorkerSettings
from cardrag_worker.state import WorkerState
from cardrag_worker.webdav import WebDAVBundlePublisher, WebDAVClient
from cardrag_worker.webdav_verification import VerificationPolicy


class DAV:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.collections: set[str] = {""}
        self.requests: list[tuple[str, str]] = []
        self.etag: str | None = '"opaque-version"'
        self.corrupt_moves = 0
        self.collision = False
        self.timeout_final = False

    def seed(self, path: str, body: bytes) -> None:
        self.files[path] = body
        self.collections.update(str(p) for p in PurePosixPath(path).parents if str(p) != ".")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path).removeprefix("/dav/")
        self.requests.append((request.method, path))
        if request.method in {"GET", "HEAD"}:
            if request.method == "GET" and self.timeout_final and path.startswith("v1/generations/"):
                raise httpx.ReadTimeout("injected read failure", request=request)
            if path not in self.files:
                return httpx.Response(200 if path in self.collections and request.method == "HEAD" else 404)
            body = self.files[path]
            headers = {"Content-Length": str(len(body))}
            if self.etag is not None:
                headers["ETag"] = self.etag
            return httpx.Response(200, content=body if request.method == "GET" else b"", headers=headers)
        if request.method == "MKCOL":
            status = 405 if path in self.collections else 201
            self.collections.add(path)
            return httpx.Response(status)
        if request.method == "PUT":
            if path in self.files and request.headers.get("If-None-Match") == "*":
                return httpx.Response(412)
            self.seed(path, request.read())
            return httpx.Response(201)
        if request.method == "MOVE":
            destination = unquote(urlsplit(request.headers["Destination"]).path).removeprefix("/dav/")
            if path not in self.files:
                return httpx.Response(404)
            if destination in self.files and request.headers.get("Overwrite") == "F":
                return httpx.Response(412)
            if self.collision and destination.startswith("v1/generations/"):
                self.seed(destination, b"foreign bad bytes")
                return httpx.Response(412)
            body = self.files.pop(path)
            if self.corrupt_moves and destination.startswith("v1/generations/"):
                self.corrupt_moves -= 1
                body = bytes([body[0] ^ 1]) + body[1:]
            self.seed(destination, body)
            return httpx.Response(201)
        if request.method == "DELETE":
            self.files.pop(path, None)
            return httpx.Response(204)
        if request.method == "PROPFIND":
            if path not in self.files and path not in self.collections:
                return httpx.Response(404)
            children = [path] + sorted(
                p for p in self.files.keys() | self.collections if str(PurePosixPath(p).parent) == path
            )
            return httpx.Response(
                207,
                content=(
                    '<d:multistatus xmlns:d="DAV:">'
                    + "".join(f"<d:response><d:href>/dav/{escape(p)}</d:href></d:response>" for p in children)
                    + "</d:multistatus>"
                ).encode(),
            )
        return httpx.Response(405)


def new_client(backend: DAV) -> WebDAVClient:
    core = CoreWebDAVClient(
        WebDAVSettings(
            base_url="https://dav.invalid/dav",
            username="worker",
            password=SecretStr("test-only"),
        ),
        transport=httpx.MockTransport(backend),
    )
    return WebDAVClient(core, stable_publication_approved=True)


@pytest.fixture
def dav(tmp_path: Path) -> Iterator[tuple[DAV, WebDAVClient, WorkerState]]:
    backend = DAV()
    client = new_client(backend)
    with WorkerState(tmp_path / "worker.sqlite3") as state:
        client.configure_verification(state, WebDAVVerificationSettings())
        yield backend, client, state
    client.core.close()


def seed_generation(backend: DAV, generation_id: str = "g-original") -> GenerationManifest:
    def ref(body: bytes, media: str, path: str | None = None) -> ArtifactRef:
        digest = sha256_bytes(body)
        result = ArtifactRef(
            sha256=digest, size_bytes=len(body), media_type=media, path=path or object_path(digest).as_posix()
        )
        backend.seed(result.path, body)
        return result

    database = ref(
        b"SQLite format 3\x00sealed",
        "application/vnd.sqlite3",
        generation_database_path(generation_id).as_posix(),
    )
    pdf = ref(b"%PDF-sealed", "application/pdf")
    ocr = ref(b"sealed text", "text/markdown; charset=utf-8")
    manifest = GenerationManifest(
        generation_id=generation_id,
        created_at=datetime(2026, 9, 6, tzinfo=UTC),
        serving_database=database,
        corpus_sha256="a" * 64,
        contract_sha256="b" * 64,
        embedding_contract=EmbeddingContract(provider="test", model="test", dimension=1536, count=0),
        issuer_codes=("kb",),
        counts=GenerationCounts(documents=1, pdf_objects=1, ocr_objects=1, chunks=0),
        documents=(GenerationDocument(document_id="doc-kb", issuer="kb", pdf=pdf, ocr=ocr, page_count=1),),
    )
    ready = GenerationReady(
        generation_id=generation_id,
        manifest_sha256=manifest.manifest_sha256,
        serving_database_sha256=database.sha256,
        serving_database_size_bytes=database.size_bytes,
    )
    pointer = GenerationPointer(
        generation_id=generation_id,
        manifest_sha256=manifest.manifest_sha256,
        ready_sha256=sha256_bytes(ready.canonical_bytes()),
    )
    backend.seed(generation_manifest_path(generation_id).as_posix(), manifest.canonical_bytes())
    backend.seed(generation_ready_path(generation_id).as_posix(), ready.canonical_bytes())
    backend.seed("v1/channels/stable.json", pointer.canonical_bytes())
    return manifest


def policy_for(client: WebDAVClient, run: str = "run1") -> VerificationPolicy:
    client.begin_verification_run(run)
    assert client.verification is not None
    return client.verification


async def test_restart_reuses_receipts_no_change_and_same_run_cas(
    dav: tuple[DAV, WebDAVClient, WorkerState],
) -> None:
    backend, client, state = dav
    manifest = seed_generation(backend)
    policy = policy_for(client)
    assert await client.validated_current_generation() is not None
    assert not policy.due()
    verified_at = policy.get("object", manifest.documents[0].pdf.path)["verified_at"]  # type: ignore[index]
    client.core.close()
    second = new_client(backend)
    try:
        second.configure_verification(state, WebDAVVerificationSettings())
        next_policy = policy_for(second, "run2")
        backend.requests.clear()
        assert await second.validated_current_generation() is not None
        await second.verification_gate()
        await second.put_cas(b"%PDF-sealed", media_type="application/pdf")
        assert not [
            p
            for method, p in backend.requests
            if method == "GET"
            and p
            in {
                manifest.serving_database.path,
                manifest.documents[0].pdf.path,
                manifest.documents[0].ocr.path,  # type: ignore[union-attr]
            }
        ]
        assert next_policy.get("object", manifest.documents[0].pdf.path)["verified_at"] == verified_at  # type: ignore[index]
        second.finish_verification_run()
        second.finish_verification_run()
        assert next_policy.audit["runs"] == 1
    finally:
        second.core.close()


async def test_fourteenth_run_due_and_failed_run_does_not_reset(
    dav: tuple[DAV, WebDAVClient, WorkerState],
) -> None:
    backend, client, _state = dav
    seed_generation(backend)
    policy_for(client, "baseline")
    await client.validated_current_generation()
    for number in range(1, 14):
        policy = policy_for(client, f"normal-{number}")
        assert not policy.due()
        client.finish_verification_run()
    policy = policy_for(client, "fourteenth")
    assert policy.due() == ["runs"]
    # A failed/restarted run neither counts twice nor clears its deadline.
    assert policy_for(client, "fourteenth").due() == ["runs"]
    await client.validated_current_generation()
    client.finish_verification_run()
    assert client.verification.audit["runs"] == 0  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("offset", "byte_delta", "expected"),
    [
        (7 * 86400 - 1, -1, []),
        (7 * 86400, -1, ["age"]),
        (0, 0, ["bytes"]),
        (0, 1, ["bytes"]),
        (7 * 86400, 0, ["age", "bytes"]),
    ],
)
async def test_age_and_byte_or_boundaries(
    dav: tuple[DAV, WebDAVClient, WorkerState], offset: int, byte_delta: int, expected: list[str]
) -> None:
    _backend, client, _state = dav
    policy = policy_for(client)
    policy.clock = lambda: 1_000_000
    policy.complete_audit(generation_id="g", manifest_sha256="a" * 64)
    policy.clock = lambda: 1_000_000 + offset
    policy.audit["new_bytes"] = 10 * 1024**3 + byte_delta
    assert policy.due() == expected


@pytest.mark.parametrize("etag", [None, 'W/"weak"', '"opaque"'])
async def test_unchanged_etag_reuse_and_due_detects_same_size_corruption(
    dav: tuple[DAV, WebDAVClient, WorkerState], etag: str | None
) -> None:
    backend, client, _state = dav
    backend.etag = etag
    manifest = seed_generation(backend)
    policy_for(client)
    await client.validated_current_generation()
    path = manifest.documents[0].pdf.path
    backend.files[path] = b"!" + backend.files[path][1:]
    policy = policy_for(client, "run2")
    assert await client.validated_current_generation() is not None
    policy.audit["new_bytes"] = 10 * 1024**3
    with pytest.raises(ExceptionGroup):
        await client.verification_gate()
    assert policy.audit["pending"] is True
    assert policy.get("object", path) is None
    assert "bytes" in policy_for(client, "run3").due()


@pytest.mark.parametrize("change", ["etag", "size", "missing"])
async def test_changed_remote_metadata_never_reuses_receipt(
    dav: tuple[DAV, WebDAVClient, WorkerState], change: str
) -> None:
    backend, client, _state = dav
    manifest = seed_generation(backend)
    policy_for(client)
    await client.validated_current_generation()
    path = manifest.serving_database.path
    if change == "etag":
        backend.etag = '"changed"'
        backend.files[path] = b"!" + backend.files[path][1:]
    elif change == "size":
        backend.files[path] += b"longer"
    else:
        del backend.files[path]
    policy_for(client, "run2")
    with pytest.raises(ExceptionGroup):
        await client.validated_current_generation()


async def test_new_cas_count_once_including_pending_restart(
    dav: tuple[DAV, WebDAVClient, WorkerState],
) -> None:
    backend, client, _state = dav
    policy = policy_for(client)
    policy.complete_audit(generation_id="g", manifest_sha256="a" * 64)
    body = b"a new cas object"
    await asyncio.gather(*(client.put_cas(body) for _ in range(16)))
    assert policy.audit["new_bytes"] == len(body)
    path = object_path(sha256_bytes(body))
    # Receipt may have committed immediately before the process lost power.
    policy.put("cas", str(path), {"pending": True})
    policy.audit["new_bytes"] = 0
    policy.put("audit", "stable", policy.audit)
    policy = policy_for(client, "run2")
    await client.put_cas(body)
    assert policy.audit["new_bytes"] == len(body)
    await client.put_cas(body)
    assert policy.audit["new_bytes"] == len(body)
    assert backend.files[str(path)] == body


@pytest.mark.parametrize(
    ("member", "mode", "reads"),
    [
        ("index.sqlite3", "final", 1),
        ("vectors.f32", "final", 1),
        ("index.sqlite3", "double", 2),
    ],
)
async def test_generation_readback_counts_and_cas_stays_double(
    dav: tuple[DAV, WebDAVClient, WorkerState], tmp_path: Path, member: str, mode: str, reads: int
) -> None:
    backend, client, state = dav
    settings = WebDAVVerificationSettings(generation_readback=mode)  # type: ignore[arg-type]
    client.configure_verification(state, settings)
    policy = policy_for(client)
    body = b"sealed generation bytes"
    source = tmp_path / member
    source.write_bytes(body)
    ref = ArtifactRef(
        sha256=sha256_bytes(body),
        size_bytes=len(body),
        media_type="application/octet-stream",
        path=f"v1/generations/g-new/{member}",
    )
    client.bind_publication_seal("a" * 64, "g-new")
    assert await client.publish_generation_file(ref, source) == ref
    assert client.core.performance_snapshot()["verification_get_payload_bytes"] == len(body) * reads
    assert policy.audit["new_bytes"] == 0
    before = client.core.performance_snapshot()["verification_get_payload_bytes"]
    await client.put_cas(b"new pdf")
    assert client.core.performance_snapshot()["verification_get_payload_bytes"] - before == 2 * len(
        b"new pdf"
    )
    assert backend.files[ref.path] == body


@pytest.mark.parametrize("failures", [1, 2])
async def test_generation_corruption_quarantine_retry_budget_survives_restart(
    dav: tuple[DAV, WebDAVClient, WorkerState], tmp_path: Path, failures: int
) -> None:
    backend, client, _state = dav
    policy = policy_for(client)
    body = b"sealed generation"
    source = tmp_path / "index.sqlite3"
    source.write_bytes(body)
    ref = ArtifactRef(
        sha256=sha256_bytes(body),
        size_bytes=len(body),
        media_type="application/vnd.sqlite3",
        path="v1/generations/g-new/index.sqlite3",
    )
    client.bind_publication_seal("a" * 64, "g-new")
    backend.corrupt_moves = failures
    if failures == 1:
        assert await client.publish_generation_file(ref, source) == ref
        assert backend.files[ref.path] == body
    else:
        with pytest.raises(WebDAVReadbackIntegrityError):
            await client.publish_generation_file(ref, source)
        assert ref.path not in backend.files
    assert len([p for p in backend.files if p.startswith("v1/.incoming/publish/")]) == failures
    assert policy.journal(PurePosixPath(ref.path))["attempts"] == 2
    policy_for(client, "restart")
    client.bind_publication_seal("a" * 64, "g-new")
    if failures == 2:
        before = len(backend.requests)
        with pytest.raises(RuntimeError, match="retry budget"):
            await client.publish_generation_file(ref, source)
        assert len(backend.requests) == before


@pytest.mark.parametrize("occupied", ["existing", "collision", "sealed", "timeout"])
async def test_unowned_sealed_or_transport_failure_is_not_quarantined(
    dav: tuple[DAV, WebDAVClient, WorkerState], tmp_path: Path, occupied: str
) -> None:
    backend, client, _state = dav
    policy = policy_for(client)
    body = b"sealed generation"
    source = tmp_path / "index.sqlite3"
    source.write_bytes(body)
    ref = ArtifactRef(
        sha256=sha256_bytes(body),
        size_bytes=len(body),
        media_type="application/vnd.sqlite3",
        path="v1/generations/g-new/index.sqlite3",
    )
    client.bind_publication_seal("a" * 64, "g-new")
    if occupied == "existing":
        backend.seed(ref.path, b"bad")
    elif occupied == "collision":
        backend.collision = True
    elif occupied == "sealed":
        backend.seed("v1/generations/g-new/READY.json", b"control exists")
        backend.corrupt_moves = 1
    else:
        backend.timeout_final = True
    with pytest.raises(CoreWebDAVError):
        await client.publish_generation_file(ref, source)
    assert not policy.snapshot().get("generation_quarantined")
    assert not any(method == "MOVE" and path == ref.path for method, path in backend.requests)


async def test_gate_failure_blocks_manifest_ready_pointer(
    dav: tuple[DAV, WebDAVClient, WorkerState], tmp_path: Path
) -> None:
    backend, client, _state = dav
    original = seed_generation(backend)
    policy_for(client)
    await client.validated_current_generation()
    policy = policy_for(client, "new-run")
    old_pointer = backend.files["v1/channels/stable.json"]
    database = tmp_path / "index.sqlite3"
    database.write_bytes(b"new database")
    reference = ArtifactRef(
        sha256=sha256_bytes(database.read_bytes()),
        size_bytes=database.stat().st_size,
        media_type="application/vnd.sqlite3",
        path="v1/generations/g-next/index.sqlite3",
    )
    manifest = original.model_copy(update={"generation_id": "g-next", "serving_database": reference})
    client.bind_publication_seal(canonical_sha256(manifest.model_dump(mode="json")), "g-next")
    backend.files[original.documents[0].pdf.path] = b"damaged"
    policy.audit["new_bytes"] = 10 * 1024**3
    with pytest.raises(ExceptionGroup):
        await WebDAVBundlePublisher(client).publish(
            generation_id="g-next", database=database, manifest=manifest.model_dump(mode="json")
        )
    assert backend.files["v1/channels/stable.json"] == old_pointer
    assert "v1/generations/g-next/manifest.json" not in backend.files
    assert "v1/generations/g-next/READY.json" not in backend.files


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CARDRAG_WEBDAV_VERIFICATION_MODE", "off"),
        ("CARDRAG_WEBDAV_GENERATION_READBACK_MODE", "none"),
        ("CARDRAG_WEBDAV_FULL_VERIFY_EVERY_RUNS", "0"),
        ("CARDRAG_WEBDAV_FULL_VERIFY_MAX_AGE_DAYS", "-1"),
        ("CARDRAG_WEBDAV_FULL_VERIFY_NEW_CAS_GIB", "1.5"),
        ("CARDRAG_WEBDAV_FORCE_FULL_VERIFY", "maybe"),
    ],
)
def test_invalid_policy_settings_rejected(name: str, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(name, value)
    for settings_type in (WorkerSettings, PublicationResumeSettings):
        with pytest.raises(ValueError, match=name):
            settings_type.from_env()


async def test_scope_and_strict_mode_force_fresh_checks(dav: tuple[DAV, WebDAVClient, WorkerState]) -> None:
    backend, client, state = dav
    manifest = seed_generation(backend)
    initial = policy_for(client)
    await client.validated_current_generation()
    client.configure_verification(state, WebDAVVerificationSettings(mode="strict"))
    strict = policy_for(client, "strict-run")
    assert "strict" in strict.due()
    backend.requests.clear()
    await client.validated_current_generation()
    assert ("GET", manifest.serving_database.path) in backend.requests
    client.configure_verification(state, WebDAVVerificationSettings(every_runs=15))
    changed = policy_for(client, "changed-policy")
    assert changed.scope != initial.scope
    assert "baseline" in changed.due()


async def test_terminal_run_counter_recovers_commit_window(
    dav: tuple[DAV, WebDAVClient, WorkerState],
) -> None:
    backend, client, state = dav
    seed_generation(backend)
    policy_for(client, "baseline")
    await client.validated_current_generation()
    run_id = state.start_run()
    policy_for(client, run_id)
    state.finish_run(run_id, "no_change")
    # Crash after the terminal row commits, before the policy counter commits.
    recovered = policy_for(client, "next-run")
    assert recovered.audit["runs"] == 1
    recovered = policy_for(client, "next-run")
    assert recovered.audit["runs"] == 1


@pytest.mark.parametrize("exhausted", [False, True])
async def test_quarantine_move_reconciles_after_restart(
    dav: tuple[DAV, WebDAVClient, WorkerState], tmp_path: Path, exhausted: bool
) -> None:
    backend, client, _state = dav
    policy = policy_for(client)
    client.bind_publication_seal("a" * 64, "g-new")
    source = tmp_path / "index.sqlite3"
    source.write_bytes(b"verified retry")
    path = PurePosixPath("v1/generations/g-new/index.sqlite3")
    quarantine = "v1/.incoming/publish/" + "1" * 32 + ".tmp"
    # Remote MOVE committed just before journal update/process termination.
    backend.seed(quarantine, b"damaged")
    policy.save_journal(
        path,
        {
            "phase": "quarantining",
            "created": True,
            "quarantine": quarantine,
            "attempts": 2 if exhausted else 1,
            "exhausted": exhausted,
        },
    )
    policy = policy_for(client, "restart")
    client.bind_publication_seal("a" * 64, "g-new")
    ref = ArtifactRef(
        sha256=sha256_bytes(source.read_bytes()),
        size_bytes=source.stat().st_size,
        media_type="application/vnd.sqlite3",
        path=str(path),
    )
    if exhausted:
        with pytest.raises(RuntimeError, match="retry budget"):
            await client.publish_generation_file(ref, source)
        assert str(path) not in backend.files
    else:
        assert await client.publish_generation_file(ref, source) == ref
    assert policy.journal(path)["phase"] == ("quarantined" if exhausted else "verified")
    assert backend.files[quarantine] == b"damaged"


async def test_journal_retry_budget_survives_policy_setting_change(
    dav: tuple[DAV, WebDAVClient, WorkerState], tmp_path: Path
) -> None:
    _backend, client, state = dav
    policy = policy_for(client)
    client.bind_publication_seal("a" * 64, "g-new")
    path = PurePosixPath("v1/generations/g-new/index.sqlite3")
    policy.save_journal(path, {"phase": "quarantined", "attempts": 2, "exhausted": True})
    client.configure_verification(state, WebDAVVerificationSettings(every_runs=15))
    policy_for(client, "restart")
    client.bind_publication_seal("a" * 64, "g-new")
    source = tmp_path / "index.sqlite3"
    source.write_bytes(b"data")
    ref = ArtifactRef(
        sha256=sha256_bytes(b"data"), size_bytes=4, media_type="application/vnd.sqlite3", path=str(path)
    )
    with pytest.raises(RuntimeError, match="retry budget"):
        await client.publish_generation_file(ref, source)


async def test_local_source_mismatch_never_quarantines_owned_remote(
    dav: tuple[DAV, WebDAVClient, WorkerState], tmp_path: Path
) -> None:
    backend, client, _state = dav
    policy = policy_for(client)
    client.bind_publication_seal("a" * 64, "g-new")
    path = PurePosixPath("v1/generations/g-new/index.sqlite3")
    backend.seed(str(path), b"good")
    policy.save_journal(path, {"phase": "moved", "created": True, "attempts": 1})
    source = tmp_path / "index.sqlite3"
    source.write_bytes(b"locally modified")
    ref = ArtifactRef(
        sha256=sha256_bytes(b"good"), size_bytes=4, media_type="application/vnd.sqlite3", path=str(path)
    )
    with pytest.raises(WebDAVIntegrityError, match="source SHA-256"):
        await client.publish_generation_file(ref, source)
    assert backend.files[str(path)] == b"good"
    assert policy.journal(path)["phase"] == "moved"


async def test_retained_object_receipt_age_is_bounded(dav: tuple[DAV, WebDAVClient, WorkerState]) -> None:
    backend, client, _state = dav
    policy = policy_for(client)
    body = b"historical cas"
    path = object_path(sha256_bytes(body))
    backend.seed(str(path), body)
    policy.clock = lambda: 1_000_000
    await to_thread_fenced(policy.verify_sync, path, sha256_bytes(body), len(body), force=True, reason="test")
    policy.clock = lambda: 1_000_000 + 8 * 86400
    policy.complete_audit(generation_id="another-generation", manifest_sha256="a" * 64)
    policy.memo.clear()
    backend.requests.clear()
    await to_thread_fenced(
        policy.verify_sync, path, sha256_bytes(body), len(body), force=False, reason="test"
    )
    assert ("GET", str(path)) in backend.requests


async def test_pointer_change_during_audit_does_not_commit_success(
    dav: tuple[DAV, WebDAVClient, WorkerState], monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, client, _state = dav
    seed_generation(backend)
    policy = policy_for(client)
    original = client._verify_manifest_members

    async def change_after_members(manifest: GenerationManifest, *, force: bool, reason: str) -> None:
        await original(manifest, force=force, reason=reason)
        backend.files["v1/channels/stable.json"] += b" "

    monkeypatch.setattr(client, "_verify_manifest_members", change_after_members)
    with pytest.raises(RuntimeError, match="pointer changed"):
        await client.validated_current_generation()
    assert policy.audit["at"] is None
    assert policy.audit["pending"]


async def test_cancelled_upload_drains_thread_and_preserves_recovery_journal(
    dav: tuple[DAV, WebDAVClient, WorkerState], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    backend, client, _state = dav
    policy = policy_for(client)
    client.bind_publication_seal("a" * 64, "g-new")
    source = tmp_path / "index.sqlite3"
    source.write_bytes(b"good")
    path = PurePosixPath("v1/generations/g-new/index.sqlite3")
    ref = ArtifactRef(
        sha256=sha256_bytes(b"good"), size_bytes=4, media_type="application/vnd.sqlite3", path=str(path)
    )
    entered, release = threading.Event(), threading.Event()
    original = client.core.verify_with_metadata

    def blocked(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(client.core, "verify_with_metadata", blocked)
    task = asyncio.create_task(client.publish_generation_file(ref, source))
    try:
        for _ in range(1000):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert entered.is_set()
        assert policy.journal(path)["phase"] == "moved"
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(client.core, "verify_with_metadata", original)
    policy_for(client, "restart")
    client.bind_publication_seal("a" * 64, "g-new")
    assert await client.publish_generation_file(ref, source) == ref
    assert backend.files[str(path)] == b"good"


def _v5_manifest(*, rows: int = 6) -> GenerationManifest:
    generation_id = "gen-v5-contract"
    pdf = ArtifactRef.for_cas(
        sha256=sha256_bytes(b"v5-pdf"),
        size_bytes=6,
        media_type="application/pdf",
    )
    ocr = ArtifactRef.for_cas(
        sha256=sha256_bytes(b"v5-ocr"),
        size_bytes=6,
        media_type="text/markdown; charset=utf-8",
    )
    profile = EmbeddingProfile.qwen3(provider_id="deepinfra", maximum_tokens=8192)
    source_hash = sha256_bytes(b"all non-whitespace OCR characters")
    view_counts = tuple(
        EmbeddingViewCount(view_type=view_type, count=rows if index == 0 else 0)
        for index, view_type in enumerate(EMBEDDING_VIEW_TYPES)
    )
    return GenerationManifest(
        schema_version="cardrag.generation.v5",
        generation_id=generation_id,
        created_at=datetime(2026, 9, 6, tzinfo=UTC),
        serving_schema="cardrag.serving-db.v5",
        serving_database=ArtifactRef(
            sha256=sha256_bytes(b"v5-sqlite"),
            size_bytes=9,
            media_type="application/vnd.sqlite3",
            path=generation_database_path(generation_id).as_posix(),
        ),
        corpus_sha256=sha256_bytes(b"v5-corpus"),
        contract_sha256=sha256_bytes(b"v5-contract"),
        embedding_contract=EmbeddingContract(
            provider="openrouter",
            model="qwen/qwen3-embedding-8b",
            dimension=4096,
            count=rows,
        ),
        issuer_codes=("kb",),
        counts=GenerationCounts(documents=1, pdf_objects=1, ocr_objects=1, chunks=rows),
        documents=(
            GenerationDocument(
                document_id="doc_v5",
                issuer="kb",
                pdf=pdf,
                ocr=ocr,
                page_count=1,
                availability="available",
            ),
        ),
        issuer_ocr_counts=(IssuerOCRCounts(issuer="kb", acquired=1, succeeded=1, failed=0),),
        structure_contract=StructureContract(
            schema_version="cardrag.structure.v2",
            parser_profiles=(
                IssuerParserProfile(
                    issuer="kb",
                    profile_id="cardrag.parser.kb.v1",
                    profile_sha256=sha256_bytes(b"kb-parser-policy"),
                ),
            ),
            node_counts=StructureNodeCounts(
                total=4,
                root=1,
                major_section=1,
                item=1,
                paragraph=1,
                list_item=0,
                table=0,
                table_row=0,
                footnote=0,
                boilerplate=0,
                unclassified=0,
            ),
            major_class_counts=StructureMajorClassCounts(
                total=1,
                benefit=1,
                notice=0,
                mixed=0,
                unknown=0,
            ),
            source_coverage=StructureSourceCoverage(
                source_non_whitespace_characters=31,
                covered_non_whitespace_characters=31,
                source_non_whitespace_sha256=source_hash,
                covered_non_whitespace_sha256=source_hash,
            ),
            revision_counts=StructureRevisionCounts(
                total=1,
                current=1,
                superseded=0,
                ambiguous=0,
            ),
            cross_contract_parent_count=0,
            cross_contract_link_count=0,
            lineages_with_multiple_current_revisions=0,
        ),
        embedding_profiles=(profile,),
        primary_embedding_profile_id=profile.profile_id,
        embedding_view_counts=view_counts,
        vector_sidecar=EmbeddingVectorSidecar(
            artifact=ArtifactRef(
                sha256=sha256_bytes(bytes(rows * 4096 * 4)),
                size_bytes=rows * 4096 * 4,
                media_type="application/octet-stream",
                path=generation_vectors_path(generation_id).as_posix(),
            ),
            profile_id=profile.profile_id,
            row_count=rows,
            dimension=4096,
            dtype="float32",
            byte_order="little-endian",
            layout="row-major",
            normalization="l2",
        ),
        parser_policy_sha256=sha256_bytes(b"parser-policy"),
        embedding_policy_sha256=sha256_bytes(b"embedding-policy"),
        retrieval_policy_sha256=sha256_bytes(b"retrieval-policy"),
    )


async def test_complete_v5_publication_preserves_single_read_and_next_run_reuse(
    dav: tuple[DAV, WebDAVClient, WorkerState], tmp_path: Path
) -> None:
    backend, client, _state = dav
    manifest = _v5_manifest(rows=1)
    policy = policy_for(client)
    for body in (b"v5-pdf", b"v5-ocr"):
        await client.put_cas(body)
    database, vectors = tmp_path / "index.sqlite3", tmp_path / "vectors.f32"
    database.write_bytes(b"v5-sqlite")
    vectors.write_bytes(bytes(4096 * 4))
    client.bind_publication_seal(canonical_sha256(manifest.model_dump(mode="json")), manifest.generation_id)
    await WebDAVBundlePublisher(client).publish(
        generation_id=manifest.generation_id,
        database=database,
        vectors=vectors,
        manifest=manifest.model_dump(mode="json"),
    )
    assert backend.requests.count(("GET", manifest.serving_database.path)) == 1
    assert backend.requests.count(("GET", manifest.vector_sidecar.artifact.path)) == 1  # type: ignore[union-attr]
    assert not policy.due()
    ready_move = next(
        i
        for i, (method, path) in enumerate(backend.requests)
        if method == "HEAD" and path == f"v1/generations/{manifest.generation_id}/READY.json"
    )
    assert backend.requests.index(("GET", manifest.serving_database.path)) < ready_move
    policy_for(client, "next-run")
    backend.requests.clear()
    current = await client.validated_current_generation()
    assert current is not None and current.generation_id == manifest.generation_id
    await client.verification_gate()
    assert not any(
        method == "GET"
        and (path.startswith("v1/objects/") or path.endswith(("index.sqlite3", "vectors.f32")))
        for method, path in backend.requests
    )


async def test_no_change_gate_refuses_pointer_removal(dav: tuple[DAV, WebDAVClient, WorkerState]) -> None:
    backend, client, _state = dav
    seed_generation(backend)
    policy = policy_for(client)
    await client.validated_current_generation()
    del backend.files["v1/channels/stable.json"]
    with pytest.raises(RuntimeError, match="pointer changed"):
        await client.verification_gate()
    assert policy.audit["pending"] is True
