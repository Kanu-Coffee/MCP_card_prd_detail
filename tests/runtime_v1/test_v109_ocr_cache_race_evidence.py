from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from cardrag_core import canonical_json_bytes, canonical_sha256

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "release-evidence/v1.0.10/v109-ocr-cache-race-causality.json"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _load() -> tuple[bytes, dict[str, Any]]:
    assert EVIDENCE.is_file() and not EVIDENCE.is_symlink()
    raw = EVIDENCE.read_bytes()
    payload = json.loads(raw)
    assert isinstance(payload, dict)
    return raw, payload


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_race_evidence_is_canonical_self_bound_and_secret_free() -> None:
    raw, payload = _load()
    assert raw == canonical_json_bytes(payload) + b"\n"
    assert set(payload) == {
        "audit_scope",
        "candidate",
        "causal_finding",
        "checkpoint_comparison",
        "evidence_sha256",
        "mitigation",
        "mitigation_required",
        "observed_at",
        "schema_version",
        "sensitive_material_included",
        "shared_identity",
        "timeline",
        "v109",
    }
    claimed = payload["evidence_sha256"]
    assert isinstance(claimed, str) and SHA256.fullmatch(claimed)
    assert claimed == "0f1084efc30858528c586a1b01f0c91abfa198c756c13340be7d66ac1e5fbbc5"
    unsigned = {key: value for key, value in payload.items() if key != "evidence_sha256"}
    assert canonical_sha256(unsigned) == claimed
    assert payload["schema_version"] == "cardrag.v109-ocr-cache-race-causality.v1"
    assert payload["sensitive_material_included"] is False

    lowered = raw.lower()
    for prohibited in (
        b"authorization",
        b"api_key",
        b"base_url",
        b"bearer ",
        b"http://",
        b"https://",
        b"password",
        b"username",
    ):
        assert prohibited not in lowered


def test_race_evidence_binds_same_identity_to_two_distinct_manifests() -> None:
    _, payload = _load()
    shared = payload["shared_identity"]
    candidate = payload["candidate"]
    v109 = payload["v109"]

    assert candidate["run_id"] == "1f1763a9cd474a81952a6eb6ffb6e397"
    assert candidate["image"] == {
        "id": "sha256:ff6c5baf7642db8df0a69b0f96263ebe4f251a9db3d96c533aa2e1b6b6d2b63b",
        "revision": "e4d3bfd8f2435f5952a6bc0f4d1b0b41af922a02",
        "version": "1.0.10",
    }
    assert v109["run"]["run_id"] == "e63725b579b5405fb03c6dc7e3d2b061"
    assert v109["image"] == {
        "id": "sha256:a3be8e1b74cb310c3f0d00496db440a00f31d65a0851337205e627153ea103c8",
        "revision": "fee8f65a9fda7ae0c286ac92cf4c3f55c1a6f113",
        "version": "1.0.9",
    }

    reuse_keys = {
        shared["reuse_key"],
        candidate["manifest"]["reuse_key"],
        candidate["ready"]["reuse_key"],
        v109["manifest"]["reuse_key"],
    }
    assert len(reuse_keys) == 1
    assert all(SHA256.fullmatch(value) for value in reuse_keys)
    assert shared["document_id"] == ("doc_f854b36963cc9f37c94da019cf44dcce35d7d5b553950feb60fe15bb753ef409")
    assert shared["pdf_sha256"] == ("cf54873e4f170de53b9ac9a6c1368c39461a25ff3145f59c995a5f2f2e3bb4a7")
    assert shared["ocr_contract_sha256"] == (
        "782fb558fd0102a01406b00009c36a5c1c1a7ce851fe388425c0003b4fff536a"
    )

    candidate_manifest = candidate["manifest"]
    v109_manifest = v109["manifest"]
    assert candidate_manifest["sha256"] != v109_manifest["sha256"]
    assert candidate["ocr"]["sha256"] != v109["ocr"]["sha256"]
    assert candidate_manifest["origin"] == "remote_first_writer"
    assert v109_manifest["origin"] == "local_loser_unpublished"
    assert candidate["ready"]["manifest_sha256"] == candidate_manifest["sha256"]
    assert candidate["ready"]["ocr_sha256"] == candidate["ocr"]["sha256"]
    assert candidate_manifest["remote_get_status"] == 200
    assert candidate["ready"]["remote_get_status"] == 200
    assert candidate["ocr"]["remote_get_status"] == 200
    assert v109["ocr"]["remote_get_status"] == 200

    comparison = payload["checkpoint_comparison"]
    assert len(comparison["identical_input_sha256"]) == 5
    assert comparison["different_output_chunk_indexes"] == [0, 1, 2, 3]
    assert comparison["identical_output_chunk_indexes"] == [4]
    differing = [
        index
        for index, (candidate_sha, v109_sha) in enumerate(
            zip(
                comparison["candidate_output_sha256"],
                comparison["v109_output_sha256"],
                strict=True,
            )
        )
        if candidate_sha != v109_sha
    ]
    assert differing == comparison["different_output_chunk_indexes"]


def test_race_evidence_timeline_proves_first_writer_causality() -> None:
    _, payload = _load()
    timeline = payload["timeline"]
    assert [row["event"] for row in timeline] == [
        "candidate_manifest_committed",
        "candidate_ready_committed",
        "candidate_ocr_stage_succeeded",
        "v109_local_manifest_created",
        "v109_manifest_integrity_failure",
        "v109_run_failed",
        "v109_container_exited",
    ]
    instants = [_instant(row["occurred_at"]) for row in timeline]
    assert instants == sorted(instants)
    assert len(set(instants)) == len(instants)

    candidate = payload["candidate"]
    v109 = payload["v109"]
    assert _instant(v109["container"]["started_at"]) < _instant(candidate["container_started_at"])
    assert _instant(candidate["container_started_at"]) < instants[0]
    assert instants[2] < _instant(v109["manifest"]["created_at"])
    assert v109["failure"] == {
        "error_class_category": "ocr_cache_publication",
        "error_kind": "integrity",
        "occurred_at": "2026-08-29T11:41:03.412402Z",
        "phase": "manifest",
        "reason": "OCR cache publication integrity verification failed",
        "reason_code": "ocr_cache_publication_manifest_integrity",
        "retryable": False,
    }
    finding = payload["causal_finding"]
    assert finding["evidence_grade"] == "confirmed_direct"
    assert finding["concurrent_first_writer_race"] is True
    assert finding["candidate_first_writer_completed_before_v109_manifest_creation"] is True
    assert finding["candidate_fix_updates_v109"] is False


def test_race_evidence_requires_candidate_remote_read_only_with_zero_audit_mutations() -> None:
    _, payload = _load()
    assert payload["mitigation_required"] == "remote_read_only"
    assert payload["mitigation"] == {
        "candidate_only": True,
        "generation_only_local_ocr_on_remote_miss": True,
        "remote_cache_lookup_allowed": True,
        "remote_cache_publication_allowed": False,
        "required_mode": "remote_read_only",
        "stable_channel_change_required": False,
        "v109_change_required": False,
    }
    scope = payload["audit_scope"]
    assert scope["docker_mutations"] == 0
    assert scope["remote_mutations"] == 0
    assert scope["volume_mutations"] == 0
    assert scope["operating_service_signals"] == 0
    assert scope["operating_service_restarts"] == 0
    assert scope["source_code_mutations_during_audit"] == 0
    assert scope["remote_authoritative_checks"] == {
        "cas_full_gets": 2,
        "control_full_gets": 2,
        "head_requests": 4,
        "operations": ["GET", "HEAD"],
        "refs_derived_from_sealed_identities": True,
    }
