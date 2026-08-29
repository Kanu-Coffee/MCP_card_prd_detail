from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cardrag_core import (
    ArtifactRef,
    GenerationPaths,
    canonical_json_bytes,
    canonical_sha256,
    channel_pointer_path,
    generation_database_path,
    generation_manifest_path,
    generation_ready_path,
    issuer_code,
    object_path,
    ocr_manifest_path,
    ocr_ready_path,
    sha256_bytes,
    validate_relative_path,
)


@dataclass(frozen=True)
class _Payload:
    name: str
    at: datetime


def test_canonical_json_is_stable_for_models_and_dataclasses() -> None:
    left = {"b": 2, "a": ["한글", True]}
    right = {"a": ["한글", True], "b": 2}
    assert canonical_json_bytes(left) == b'{"a":["\xed\x95\x9c\xea\xb8\x80",true],"b":2}'
    assert canonical_sha256(left) == canonical_sha256(right)
    payload = _Payload("x", datetime(2026, 8, 25, 1, 2, 3, tzinfo=UTC))
    assert b"2026-08-25T01:02:03.000000Z" in canonical_json_bytes(payload)
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json_bytes({"bad": float("nan")})


def test_open_issuer_code_contract_accepts_future_safe_adapter() -> None:
    assert issuer_code("woori") == "woori"
    assert issuer_code("lotte") == "lotte"
    assert issuer_code("new_card2") == "new_card2"
    for invalid in ("KB", "../kb", "a", "kb/card", "kb card"):
        with pytest.raises(ValueError, match="issuer code"):
            issuer_code(invalid)


def test_paths_match_v1_contract_and_reject_traversal() -> None:
    digest = sha256_bytes(b"artifact")
    assert object_path(digest).as_posix() == f"v1/objects/sha256/{digest[:2]}/{digest}"
    assert ocr_manifest_path(digest).as_posix() == (
        f"v1/ocr-cache/native/{digest[:2]}/{digest}/manifest.json"
    )
    assert ocr_ready_path(digest, kind="adopted").as_posix() == (
        f"v1/ocr-cache/adopted/{digest[:2]}/{digest}/READY.json"
    )
    assert generation_manifest_path("gen-20260825").as_posix() == (
        "v1/generations/gen-20260825/manifest.json"
    )
    assert generation_ready_path("gen-20260825").as_posix() == ("v1/generations/gen-20260825/READY.json")
    assert generation_database_path("gen-20260825").as_posix() == (
        "v1/generations/gen-20260825/index.sqlite3"
    )
    paths = GenerationPaths.for_generation("gen-20260825")
    assert paths.serving_database == generation_database_path(paths.generation_id)
    assert channel_pointer_path().as_posix() == "v1/channels/stable.json"
    assert channel_pointer_path("candidate-v1.0.9").as_posix() == ("v1/channels/candidate-v1.0.9.json")
    with pytest.raises(ValueError, match="channel"):
        channel_pointer_path("../candidate")
    for unsafe in ("../secret", "/absolute", "v1/%2e%2e/file", "v1\\file", "v1//file"):
        with pytest.raises(ValueError):
            validate_relative_path(unsafe)


def test_artifact_reference_binds_cas_path_to_digest() -> None:
    digest = sha256_bytes(b"pdf")
    reference = ArtifactRef.for_cas(
        sha256=digest,
        size_bytes=3,
        media_type="application/pdf",
    )
    assert reference.path == object_path(digest).as_posix()
    assert reference.mime_type == "application/pdf"
    with pytest.raises(ValidationError, match="does not match"):
        ArtifactRef(
            sha256=digest,
            size_bytes=3,
            media_type="application/pdf",
            path=object_path(sha256_bytes(b"other")).as_posix(),
        )
