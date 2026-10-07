"""Behavior tests for the lightweight public release-qualification evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cardrag_core.canonical import canonical_json_bytes
from cardrag_core.release_qualification import (
    NOT_PERFORMED,
    QUALIFICATION_SCHEMA,
    ReleaseQualification,
    ReleaseQualificationError,
    main,
    verify_release_qualification,
)

SOURCE_COMMIT = "c" * 40
WORKER = "sha256:" + "a" * 64
MCP = "sha256:" + "b" * 64
REPOSITORY = "Kanu-Coffee/MCP_card_prd_detail"
OPERATIONAL_RUN = "03fbc4f18a3c450bb017e2fd6f6442c4"
OPERATIONAL_GENERATION = "g-03fbc4f18a3c450bb017e2fd-36bae25dd8cd"
OPERATIONAL_SOURCE = "1ba366db8029c6947c0509c2d480ed6182cf996b"


def _qualification() -> ReleaseQualification:
    return ReleaseQualification(
        schema_version=QUALIFICATION_SCHEMA,
        release_version="1.0.32",
        source_commit=SOURCE_COMMIT,
        worker_image_digest=WORKER,
        mcp_image_digest=MCP,
        ci={
            "commit": SOURCE_COMMIT,
            "conclusion": "success",
            "run_url": "https://github.com/Kanu-Coffee/MCP_card_prd_detail/actions/runs/37273732727",
        },
        operational_reference={
            "worker_run_id": OPERATIONAL_RUN,
            "generation_id": OPERATIONAL_GENERATION,
            "image_source_commit": OPERATIONAL_SOURCE,
            "interpretation": "reference_only_not_candidate_runtime",
        },
        not_performed=NOT_PERFORMED,
    )


def _write(tmp_path: Path, payload: bytes | None = None) -> Path:
    path = tmp_path / "release-qualification.json"
    path.write_bytes(payload if payload is not None else canonical_json_bytes(_qualification()) + b"\n")
    return path


def _verify(path: Path, **overrides: str) -> None:
    kwargs: dict[str, str] = {
        "expected_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "expected_source_commit": SOURCE_COMMIT,
        "expected_worker_image_digest": WORKER,
        "expected_mcp_image_digest": MCP,
        "expected_repository": REPOSITORY,
    }
    kwargs.update(overrides)
    validation = verify_release_qualification(path, **kwargs)  # type: ignore[arg-type]
    assert validation.status == "qualified"


@pytest.mark.parametrize("version", ["1.0.32", "1.0.33"])
def test_qualification_round_trips_and_binds_public_artifacts(tmp_path: Path, version: str) -> None:
    payload = _qualification().model_dump(mode="json")
    payload["release_version"] = version
    path = _write(tmp_path, canonical_json_bytes(payload) + b"\n")
    _verify(path)


def test_qualification_cli_is_canonical_and_fails_closed(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    arguments = [
        "--qualification",
        str(path),
        "--expected-sha256",
        digest,
        "--expected-source-commit",
        SOURCE_COMMIT,
        "--expected-worker-image-digest",
        WORKER,
        "--expected-mcp-image-digest",
        MCP,
        "--expected-repository",
        REPOSITORY,
    ]
    assert main(arguments) == 0
    printed = capfd.readouterr()
    assert '"status":"qualified"' in printed.out
    assert printed.err == ""
    assert main([*arguments[:3], "0" * 64, *arguments[4:]]) == 1
    failed = capfd.readouterr()
    assert failed.out == ""
    assert failed.err == "release qualification validation failed\n"


@pytest.mark.parametrize("mutation", ["tamper", "duplicate-key", "non-canonical", "extra-key"])
def test_qualification_rejects_tampered_or_noncanonical_bytes(tmp_path: Path, mutation: str) -> None:
    original = canonical_json_bytes(_qualification()) + b"\n"
    payload = original
    if mutation == "tamper":
        payload = original.replace(b'"conclusion":"success"', b'"conclusion":"failure"')
    elif mutation == "duplicate-key":
        payload = original.rstrip(b"\n")
        payload = payload.replace(b'"schema_version"', b'"schema_version":1,"schema_version"', 1) + b"\n"
    elif mutation == "non-canonical":
        payload = b" " + original.lstrip()
    else:
        data = json.loads(original)
        data["unexpected"] = True
        payload = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()
    assert payload != original
    path = _write(tmp_path, payload)
    with pytest.raises(ReleaseQualificationError):
        _verify(path)


def test_qualification_rejects_foreign_bindings(tmp_path: Path) -> None:
    path = _write(tmp_path)
    with pytest.raises(ReleaseQualificationError, match="source_commit_mismatch"):
        _verify(path, expected_source_commit="d" * 40)
    with pytest.raises(ReleaseQualificationError, match="worker_digest_mismatch"):
        _verify(path, expected_worker_image_digest="sha256:" + "e" * 64)
    with pytest.raises(ReleaseQualificationError, match="mcp_digest_mismatch"):
        _verify(path, expected_mcp_image_digest="sha256:" + "f" * 64)
    with pytest.raises(ReleaseQualificationError, match="foreign_repository"):
        _verify(path, expected_repository="other-owner/other-repository")


def test_qualification_model_rejects_claimed_runtime_or_missing_not_performed() -> None:
    with pytest.raises(Exception, match="exact four"):
        ReleaseQualification(
            **{
                **_qualification().model_dump(),
                "not_performed": ("gold_quality_evaluation", "production_cutover"),
            }
        )
    payload = _qualification().model_dump()
    payload["ci"]["commit"] = "e" * 40
    with pytest.raises(Exception, match="CI run"):
        ReleaseQualification.model_validate(payload)
    payload = _qualification().model_dump()
    payload["operational_reference"]["image_source_commit"] = SOURCE_COMMIT
    with pytest.raises(Exception, match="different source commit"):
        ReleaseQualification.model_validate(payload)


def test_qualification_rejects_nonregular_files(tmp_path: Path) -> None:
    directory = tmp_path / "release-qualification.json"
    directory.mkdir()
    with pytest.raises(ReleaseQualificationError, match="file_invalid"):
        verify_release_qualification(
            directory,
            expected_sha256="0" * 64,
            expected_source_commit=SOURCE_COMMIT,
            expected_worker_image_digest=WORKER,
            expected_mcp_image_digest=MCP,
            expected_repository=REPOSITORY,
        )
