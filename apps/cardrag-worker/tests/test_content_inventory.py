from __future__ import annotations

import pytest
from cardrag_core import object_path, sha256_bytes
from test_gc import build_remote

from cardrag_worker.content_inventory import inventory_content_migration


@pytest.mark.asyncio
async def test_m0_inventory_counts_valid_legacy_and_served_text() -> None:
    webdav, active_key, _inactive_key, _unused = build_remote()
    body = "## Page 1\n\n카드 혜택 조건과 제외 사항을 충분히 설명하는 본문입니다.\n".encode()
    webdav.objects[object_path(sha256_bytes(body)).as_posix()] = body

    result = await inventory_content_migration(webdav)  # type: ignore[arg-type]

    assert result.legacy_found == 3
    assert result.legacy_valid == 2
    assert result.by_kind == {"native": 2}
    assert result.multi_variant_pdfs == 1
    assert result.stable_ocr_documents == 1
    assert result.stable_generation_only == 0
    assert result.stable_latest_would_change_text == 0
    assert result.stable_unverified == 0
    assert len(result.exclusions) == 1
    assert result.exclusions[0].reuse_key != active_key
    assert result.summary()["read_only"] is True


@pytest.mark.asyncio
async def test_m0_inventory_detects_generation_only_ocr() -> None:
    webdav, active_key, inactive_key, _unused = build_remote()
    body = "## Page 1\n\n카드 혜택 조건과 제외 사항을 충분히 설명하는 본문입니다.\n".encode()
    webdav.objects[object_path(sha256_bytes(body)).as_posix()] = body
    webdav.objects.pop(f"v1/ocr-cache/native/{active_key[:2]}/{active_key}/READY.json")
    webdav.objects.pop(f"v1/ocr-cache/native/{inactive_key[:2]}/{inactive_key}/READY.json")

    result = await inventory_content_migration(webdav)  # type: ignore[arg-type]

    assert result.legacy_valid == 0
    assert result.stable_generation_only == 1
    assert result.stable_unverified == 0
