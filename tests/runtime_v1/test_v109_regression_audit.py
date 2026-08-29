from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest
from cardrag_core import canonical_sha256
from cardrag_worker.legacy_v4_audit import (
    LegacyV4AuditError,
    load_audit_artifact,
    validate_audit_artifact,
    validate_historical_artifact,
    validate_historical_source_artifact,
    validate_release_evidence,
)
from cardrag_worker.legacy_v4_audit import main as legacy_audit_main

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "release-evidence/v1.0.10"


def _reseal(payload: dict[str, object]) -> dict[str, object]:
    unsigned = {key: value for key, value in payload.items() if key != "evidence_sha256"}
    return {**unsigned, "evidence_sha256": canonical_sha256(unsigned)}


def test_historical_and_sealed_reaudit_are_distinct_and_exactly_bound() -> None:
    historical = load_audit_artifact(EVIDENCE / "v109-kb-real-regression-baseline.json")
    historical_source = load_audit_artifact(EVIDENCE / "v109-structure-audit-execution.json")
    sealed = load_audit_artifact(EVIDENCE / "v109-kb-v4-structure-reaudit.json")

    validate_historical_source_artifact(historical_source)
    validate_historical_artifact(
        historical,
        require_source_binding=True,
        source_artifact=historical_source,
    )
    validate_audit_artifact(sealed, require_release_binding=True)
    validate_release_evidence(sealed, historical, historical_source)
    assert historical_source["evidence_scope"] == {
        "authentication_material_included": False,
        "card_document_text_included": False,
        "external_timestamp_attestation": False,
        "full_session_included": False,
        "independent_session_inclusion_proof": False,
        "missing_inputs_fail_closed": False,
        "operational_identifiers_and_local_paths_included": True,
        "run_completion_attested": False,
        "session_reference_locally_verified": True,
        "stable_snapshot_attested": False,
        "trust_root": "repository_review",
        "underlying_run_artifacts_hash_bound": False,
    }
    assert historical["provenance"] == {
        "binding": "execution_record_hash_bound",
        "run_id": "e63725b579b5405fb03c6dc7e3d2b061",
        "source_artifact_sha256": ("260b8e5302f368e6f37b2e2556b0acfdcd4ee24b4b17c159bcb6f02bc1f7b1fe"),
        "source_kind": "codex_command_execution",
    }
    observed = sealed["comparison_to_historical_run"]["observed"]
    assert observed == {
        "continuation_chunks": {
            "denominator": 4175,
            "numerator": 1379,
            "percent_4dp": "33.0299",
        },
        "fragmented_markdown_tables": {
            "denominator": 2779,
            "numerator": 45,
            "percent_4dp": "1.6193",
        },
        "mid_line_continuations": {
            "denominator": 1379,
            "numerator": 1293,
            "percent_4dp": "93.7636",
        },
        "titled_body_chunks": {
            "denominator": 3710,
            "numerator": 1467,
            "percent_4dp": "39.5418",
        },
        "titleless_continuations": {
            "denominator": 1379,
            "numerator": 388,
            "percent_4dp": "28.1363",
        },
    }
    assert sealed["source_database"] == {
        "contract_sha256": "65b4f44212114f34641f38c30221acfbd903701b3e4097883c9dc6017940dece",
        "corpus_sha256": "d11f80f9af71b98f675510529d8660da41786dedb220917180379120ab9170ab",
        "generation_id": "g-2208f0c6076649c4be915be1-d11f80f9af71",
        "schema_id": "cardrag.serving-db.v4",
        "sha256": "d25be45bc5d39af6561e587635b08312913107b6f6416500da39ab9eb757d38f",
        "size_bytes": 58466304,
    }


def test_release_gate_rejects_historical_source_tamper_or_mismatch() -> None:
    historical = load_audit_artifact(EVIDENCE / "v109-kb-real-regression-baseline.json")
    historical_source = load_audit_artifact(EVIDENCE / "v109-structure-audit-execution.json")
    sealed = load_audit_artifact(EVIDENCE / "v109-kb-v4-structure-reaudit.json")

    with pytest.raises(LegacyV4AuditError, match="source artifact is required"):
        validate_historical_artifact(historical, require_source_binding=True)

    tampered_source = json.loads(json.dumps(historical_source))
    encoded = tampered_source["raw_record_base64"]
    assert isinstance(encoded, str)
    tampered_source["raw_record_base64"] = ("A" if encoded[0] != "A" else "B") + encoded[1:]
    with pytest.raises(LegacyV4AuditError, match="source record bytes"):
        validate_historical_source_artifact(_reseal(tampered_source))

    mismatched = json.loads(json.dumps(historical))
    mismatched["provenance"]["source_artifact_sha256"] = "0" * 64
    with pytest.raises(LegacyV4AuditError, match="does not match"):
        validate_historical_artifact(
            _reseal(mismatched),
            require_source_binding=True,
            source_artifact=historical_source,
        )

    resealed_observation = json.loads(json.dumps(historical))
    resealed_observation["observations"]["kb"]["chunks"] = 4174
    with pytest.raises(LegacyV4AuditError, match="differ from source stdout"):
        validate_historical_artifact(
            _reseal(resealed_observation),
            require_source_binding=True,
            source_artifact=historical_source,
        )

    forged = json.loads(json.dumps(sealed))
    forged["comparison_to_historical_run"]["match"] = True
    with pytest.raises(LegacyV4AuditError, match="comparison result"):
        validate_audit_artifact(_reseal(forged), require_release_binding=True)


def test_release_workflow_invokes_fail_closed_legacy_audit_validator() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert ".venv/bin/python -m cardrag_worker.legacy_v4_audit" in workflow
    assert '--validate-release-artifact "$legacy_reaudit"' in workflow
    assert '--historical-artifact "$legacy_historical"' in workflow
    assert '--historical-source-artifact "$legacy_historical_source"' in workflow
    assert not tuple(ROOT.glob("release-evidence/**/*.sqlite*"))


def test_release_cli_requires_and_accepts_the_exact_execution_record() -> None:
    sealed = EVIDENCE / "v109-kb-v4-structure-reaudit.json"
    historical = EVIDENCE / "v109-kb-real-regression-baseline.json"
    historical_source_path = EVIDENCE / "v109-structure-audit-execution.json"

    assert (
        legacy_audit_main(
            [
                "--validate-release-artifact",
                str(sealed),
                "--historical-artifact",
                str(historical),
            ]
        )
        == 1
    )
    assert (
        legacy_audit_main(
            [
                "--validate-release-artifact",
                str(sealed),
                "--historical-artifact",
                str(historical),
                "--historical-source-artifact",
                str(historical_source_path),
            ]
        )
        == 0
    )

    historical_source = load_audit_artifact(historical_source_path)
    raw_record = base64.b64decode(historical_source["raw_record_base64"], validate=True)
    for pattern in (
        rb"authorization",
        rb"bearer\s",
        rb"api[_-]?key",
        rb"credential",
        rb"password",
        rb"secret",
        rb"token",
        rb"https?://",
    ):
        assert re.search(pattern, raw_record, flags=re.IGNORECASE) is None
