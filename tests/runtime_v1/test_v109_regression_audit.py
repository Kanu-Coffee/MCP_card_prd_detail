from __future__ import annotations

import json
from pathlib import Path

import pytest
from cardrag_core import canonical_sha256
from cardrag_worker.legacy_v4_audit import (
    LegacyV4AuditError,
    load_audit_artifact,
    validate_audit_artifact,
    validate_historical_artifact,
)

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "release-evidence/v1.0.10"


def _reseal(payload: dict[str, object]) -> dict[str, object]:
    unsigned = {key: value for key, value in payload.items() if key != "evidence_sha256"}
    return {**unsigned, "evidence_sha256": canonical_sha256(unsigned)}


def test_historical_and_sealed_reaudit_are_distinct_and_exactly_bound() -> None:
    historical = load_audit_artifact(EVIDENCE / "v109-kb-real-regression-baseline.json")
    sealed = load_audit_artifact(EVIDENCE / "v109-kb-v4-structure-reaudit.json")

    validate_historical_artifact(historical)
    validate_audit_artifact(sealed, require_release_binding=True)
    assert historical["provenance"] == {
        "binding": "observation_only",
        "run_id": "e63725b579b5405fb03c6dc7e3d2b061",
        "source_artifact_sha256": None,
        "source_kind": "worker_run_artifacts",
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


def test_release_gate_does_not_trust_match_or_unbound_historical_observation() -> None:
    historical = load_audit_artifact(EVIDENCE / "v109-kb-real-regression-baseline.json")
    sealed = load_audit_artifact(EVIDENCE / "v109-kb-v4-structure-reaudit.json")

    with pytest.raises(LegacyV4AuditError, match="source artifact hash"):
        validate_historical_artifact(historical, require_source_binding=True)

    forged = json.loads(json.dumps(sealed))
    forged["comparison_to_historical_run"]["match"] = True
    with pytest.raises(LegacyV4AuditError, match="comparison result"):
        validate_audit_artifact(_reseal(forged), require_release_binding=True)


def test_release_workflow_invokes_fail_closed_legacy_audit_validator() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert ".venv/bin/python -m cardrag_worker.legacy_v4_audit" in workflow
    assert '--validate-release-artifact "$legacy_reaudit"' in workflow
    assert '--historical-artifact "$legacy_historical"' in workflow
    assert not tuple(ROOT.glob("release-evidence/**/*.sqlite*"))
