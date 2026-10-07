"""Exercise content variants through the real WebDAV facade on an isolated transport."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.sax.saxutils import escape

import httpx
import pytest
from cardrag_core import (
    ArtifactRef,
    ContentOCRArtifactManifest,
    NativeOCRContract,
    OCRInput,
    WebDAVSettings,
    object_path,
    sha256_bytes,
    verify_ocr_bytes,
)
from cardrag_core import (
    WebDAVClient as CoreWebDAVClient,
)
from pydantic import SecretStr
from test_gc import build_remote
from typer.testing import CliRunner

from cardrag_worker.cli import app
from cardrag_worker.content_cache import ContentOCRVariantStore
from cardrag_worker.content_migration import apply_content_migration, plan_content_migration
from cardrag_worker.ocr import OCRResolver
from cardrag_worker.state import WorkerState
from cardrag_worker.webdav import WebDAVClient


class IsolatedWebDAV:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.collections = {""}
        self.methods: list[str] = []

    @staticmethod
    def path(url: httpx.URL | str) -> str:
        return unquote(urlsplit(str(url)).path.removeprefix("/dav/")).rstrip("/")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        path = self.path(request.url)
        self.methods.append(method)
        parent = path.rpartition("/")[0]
        if method == "HEAD":
            if path in self.files:
                return httpx.Response(200, headers={"Content-Length": str(len(self.files[path]))})
            return httpx.Response(200 if path in self.collections else 404)
        if method == "GET":
            if path not in self.files:
                return httpx.Response(404)
            return httpx.Response(200, content=self.files[path])
        if method == "PROPFIND":
            if path not in self.collections and path not in self.files:
                return httpx.Response(404)
            children = [
                name
                for name in self.files.keys() | self.collections
                if name == path or name.rpartition("/")[0] == path
            ]
            body = (
                "<d:multistatus xmlns:d='DAV:'>"
                + "".join(
                    f"<d:response><d:href>/dav/{escape(name)}</d:href></d:response>"
                    for name in sorted(children)
                )
                + "</d:multistatus>"
            )
            return httpx.Response(207, content=body.encode())
        if method == "MKCOL":
            if path in self.collections:
                return httpx.Response(405)
            if parent not in self.collections:
                return httpx.Response(409)
            self.collections.add(path)
            return httpx.Response(201)
        if method == "PUT":
            if parent not in self.collections:
                return httpx.Response(409)
            if request.headers.get("If-None-Match") == "*" and path in self.files:
                return httpx.Response(412)
            self.files[path] = request.read()
            return httpx.Response(201)
        if method == "MOVE":
            destination = self.path(request.headers["Destination"])
            if path not in self.files:
                return httpx.Response(404)
            if request.headers.get("Overwrite") == "F" and destination in self.files:
                return httpx.Response(412)
            if destination.rpartition("/")[0] not in self.collections:
                return httpx.Response(409)
            self.files[destination] = self.files.pop(path)
            return httpx.Response(201)
        if method == "DELETE":
            if path not in self.files:
                return httpx.Response(404)
            del self.files[path]
            return httpx.Response(204)
        return httpx.Response(405)


def _manifest(at: datetime, text: str) -> tuple[ContentOCRArtifactManifest, bytes]:
    body = f"## Page 1\n\n{text}\n".encode()
    verified = verify_ocr_bytes(body, expected_page_count=1)
    source = OCRInput(pdf_sha256=sha256_bytes(b"pdf"), pdf_size_bytes=3, page_count=1)
    contract = NativeOCRContract(
        processor_version="worker/1",
        prompt_version="ocr/v1",
        prompt_sha256=sha256_bytes(b"prompt"),
        renderer_id="pdfium",
        render_scale_milli=6000,
        provider="codex-exec",
        model="test-model",
        reasoning_effort="medium",
        segmentation_strategy_id="windowed",
        whole_document_max_pages=4,
        target_pages_per_call=2,
        context_pages_before=1,
        context_pages_after=1,
        output_policy="target-pages-only",
    )
    return ContentOCRArtifactManifest.create(
        source=source,
        cache_epoch=0,
        output=ArtifactRef.for_cas(
            sha256=verified.sha256,
            size_bytes=verified.size_bytes,
            media_type="text/markdown; charset=utf-8",
        ),
        ocr_chars=verified.char_count,
        page_output_sha256=verified.page_sha256,
        created_at=at,
        provenance=contract,
    ), body


@pytest.mark.asyncio
async def test_isolated_webdav_variant_publish_snapshot_and_restore(tmp_path: Path) -> None:
    backend = IsolatedWebDAV()
    settings = WebDAVSettings(
        environment="test",
        base_url="http://127.0.0.1/dav",
        username="test",
        password=SecretStr("test"),
        allow_insecure_http=True,
    )
    core = CoreWebDAVClient(settings, transport=httpx.MockTransport(backend))
    client = WebDAVClient(core)
    try:
        store = ContentOCRVariantStore(webdav=client, state_root=tmp_path)
        old, old_body = _manifest(datetime(2026, 10, 6, tzinfo=UTC), "old result")
        new, new_body = _manifest(datetime(2026, 10, 6, tzinfo=UTC) + timedelta(hours=1), "new result")
        await store.publish(old, old_body)
        await store.freeze("frozen-run")
        await store.publish(new, new_body)
        frozen = await store.lookup(run_id="frozen-run", source=old.source, cache_epoch=0)
        assert frozen is not None and frozen.manifest.variant_id == old.variant_id
        latest = await store.lookup(run_id="next-run", source=old.source, cache_epoch=0)
        assert latest is not None and latest.manifest.variant_id == new.variant_id
        restored = await store.restore_variant(source=old.source, cache_epoch=0, variant_id=old.variant_id)
        after = await store.lookup(run_id="after-restore", source=old.source, cache_epoch=0)
        assert after is not None and after.manifest.variant_id == restored.variant_id
        assert after.body == old_body
        assert backend.files[str(old.variant_root / "manifest.json")] == old.canonical_bytes()
        assert {"PROPFIND", "PUT", "GET", "HEAD"} <= set(backend.methods)
    finally:
        core.close()


def test_migration_apply_requires_explicit_stable_generation() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["ocr-cache", "migrate", "--apply"])
    assert result.exit_code != 0
    assert "--confirm-stable-generation" in result.output


@pytest.mark.asyncio
async def test_isolated_webdav_migration_retries_and_opencode_cache_hit(tmp_path: Path) -> None:
    source, _active, _inactive, _unused = build_remote()
    ocr_body = "## Page 1\n\n카드 혜택 조건과 제외 사항을 충분히 설명하는 본문입니다.\n".encode()
    backend = IsolatedWebDAV()
    backend.files = {
        key: value for key, value in source.objects.items() if not key.startswith("v1/ocr-cache/native/")
    }
    digest = sha256_bytes(ocr_body)
    backend.files[object_path(digest).as_posix()] = ocr_body
    original_files = dict(backend.files)
    for path in backend.files:
        current = path.rpartition("/")[0]
        while current:
            backend.collections.add(current)
            current = current.rpartition("/")[0]
    settings = WebDAVSettings(
        environment="test",
        base_url="http://127.0.0.1/dav",
        username="test",
        password=SecretStr("test"),
        allow_insecure_http=True,
    )
    core = CoreWebDAVClient(settings, transport=httpx.MockTransport(backend))
    client = WebDAVClient(core)
    try:
        plan = await plan_content_migration(client)
        assert plan.generation_only_variant_count == 1
        assert plan.legacy_variant_count == 0
        assert await apply_content_migration(client, plan, state_root=tmp_path) == 1
        assert await apply_content_migration(client, plan, state_root=tmp_path) == 1
        verified = await ContentOCRVariantStore(webdav=client, state_root=tmp_path).verify_all()
        assert len(verified) == 1
        assert verified[0].variant_id == plan.stable_document_variant_ids["doc_kb"]

        class NoCallProvider:
            provider = "opencode"
            model = "alibaba-token-plan/qwen3.8-flash"
            reasoning_effort = "medium"

            async def recognize(self, *_args: object, **_kwargs: object) -> str:
                raise AssertionError("migrated content must avoid provider calls")

        pdf_path = tmp_path / "source.pdf"
        pdf_path.write_bytes(b"pdf")
        with WorkerState(tmp_path / "state.sqlite3") as state:
            resolver = OCRResolver(
                provider=NoCallProvider(),  # type: ignore[arg-type]
                state=state,
                webdav=client,
                cache_mode="read-only",
                require_cache_hit=True,
            )
            result = await resolver.resolve(
                run_id="isolated-migration",
                document_id="doc_kb",
                pdf_path=pdf_path,
                pdf_sha256=sha256_bytes(b"pdf"),
                pdf_size_bytes=3,
                page_count=1,
                output_dir=tmp_path / "runs" / "isolated-migration" / "documents" / "doc_kb" / "ocr",
            )
        assert result.cache_variant_id == plan.stable_document_variant_ids["doc_kb"]
        assert result.provider_called is False
        assert all(backend.files.get(path) == body for path, body in original_files.items())
    finally:
        core.close()
