"""Tests for 019: Unresolved revision history preservation, remediation, and exclusion."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cardrag_worker.cli import app
from cardrag_worker.contracts import SourceRecord
from cardrag_worker.issuer_scope import preserve_inputs
from cardrag_worker.partial_execution import ReuseSource
from cardrag_worker.pipeline import WorkerPipeline
from cardrag_worker.revision_history_v5 import (
    UnresolvedRevisionIdentityV5,
    canonical_unresolved_revision_ledger_v5,
    unresolved_revision_ledger_sha256_v5,
)
from cardrag_worker.state import WorkerState
from cardrag_worker.unresolved_remediation import (
    apply_unresolved_remediation,
    plan_unresolved_remediation,
)

pytest_plugins = ["test_partial_execution"]


def _make_source_record(
    issuer: str = "hana",
    product_code: str = "CARD100",
    product_name: str = "하나 카드",
    version: str = "v1",
    effective_date: date = date(2026, 1, 1),
    url: str = "https://cards.example/card100.pdf",
) -> SourceRecord:
    return SourceRecord(
        issuer=issuer,
        product_code=product_code,
        product_name=product_name,
        effective_date=effective_date,
        source_version=version,
        source_url=url,
        source_post_id=f"post_{product_code}",
        file_name=f"{product_code}.pdf",
    )


def test_plan_and_apply_unresolved_remediation_all_three_reasons(tmp_path: Path) -> None:
    db_path = tmp_path / "worker.state.sqlite3"
    now_iso = datetime.now(UTC).isoformat()
    yesterday_iso = datetime(2026, 10, 18, tzinfo=UTC).isoformat()

    source_meta_unres_id = "source_" + "1" * 64
    pdf_sha1 = "a" * 64

    source_collision_id = "source_" + "2" * 64
    pdf_sha2 = "b" * 64

    source_obj_unavail_id = "source_" + "3" * 64
    pdf_sha3 = "c" * 64

    source_current_error_id = "source_" + "4" * 64
    pdf_sha4 = "d" * 64

    with WorkerState(db_path) as state:
        # Populate pdf_cache_source and revisions
        # 1. source_metadata_unresolved: in pdf_cache_source, but not in known_sources
        state.connection.execute(
            """INSERT INTO pdf_cache_source
               (source_id, issuer, product_code, document_type, source_url, source_version,
                source_post_id, discovery_sha256, first_observed_at, last_observed_at, last_verified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_meta_unres_id,
                "hana",
                "15758",
                "summary",
                "https://hana.example/old.pdf",
                "v1",
                "post1",
                "1" * 64,
                yesterday_iso,
                yesterday_iso,
                yesterday_iso,
            ),
        )
        # 2. collision source
        state.connection.execute(
            """INSERT INTO pdf_cache_source
               (source_id, issuer, product_code, document_type, source_url, source_version,
                source_post_id, discovery_sha256, first_observed_at, last_observed_at, last_verified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_collision_id,
                "hyundai",
                "12461",
                "summary",
                "https://hyundai.example/card1.pdf",
                "v1",
                "post2",
                "2" * 64,
                yesterday_iso,
                yesterday_iso,
                yesterday_iso,
            ),
        )
        # 3. object unavailable (historical)
        state.connection.execute(
            """INSERT INTO pdf_cache_source
               (source_id, issuer, product_code, document_type, source_url, source_version,
                source_post_id, discovery_sha256, first_observed_at, last_observed_at, last_verified_at, superseded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_obj_unavail_id,
                "hyundai",
                "153805",
                "summary",
                "https://hyundai.example/card2.pdf",
                "v1",
                "post3",
                "3" * 64,
                yesterday_iso,
                yesterday_iso,
                yesterday_iso,
                now_iso,
            ),
        )
        # 4. active current revision in failed products -> current_error
        state.connection.execute(
            """INSERT INTO pdf_cache_source
               (source_id, issuer, product_code, document_type, source_url, source_version,
                source_post_id, discovery_sha256, first_observed_at, last_observed_at, last_verified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_current_error_id,
                "woori",
                "99999",
                "summary",
                "https://woori.example/card4.pdf",
                "v1",
                "post4",
                "4" * 64,
                now_iso,
                now_iso,
                now_iso,
            ),
        )

        seal_data = {
            "generation_id": "gen_test_019",
            "run_id": "run_test_019",
            "v5_metrics": {
                "historical_revision_unresolved_identities": [
                    {
                        "source_id": source_meta_unres_id,
                        "pdf_sha256": pdf_sha1,
                        "reason_codes": ["source_metadata_unresolved"],
                    },
                    {
                        "source_id": source_collision_id,
                        "pdf_sha256": pdf_sha2,
                        "reason_codes": ["document_identity_collision"],
                    },
                    {
                        "source_id": source_obj_unavail_id,
                        "pdf_sha256": pdf_sha3,
                        "reason_codes": ["pdf_cache_object_unavailable"],
                    },
                    {
                        "source_id": source_current_error_id,
                        "pdf_sha256": pdf_sha4,
                        "reason_codes": ["pdf_cache_object_unavailable"],
                    },
                ]
            },
        }

        # Case A: Plan remediation with current discovery containing 15758 and 12461
        plan = plan_unresolved_remediation(
            state,
            seal_data,
            known_sources={},  # source_meta_unres_id is not in known_sources
            current_discovery={
                "hana": {"15758"},
                "hyundai": {"12461"},
                "woori": {"99999"},
            },
            current_failed_products={"woori": {"99999"}},
        )

        assert plan.total_identities == 4
        item_by_src = {item.source_id: item for item in plan.items}

        # 1. source_metadata_unresolved with product active in discovery -> updated_to_latest
        assert item_by_src[source_meta_unres_id].disposition == "updated_to_latest"
        assert item_by_src[source_meta_unres_id].issuer == "hana"
        assert item_by_src[source_meta_unres_id].product_code == "15758"

        # 2. collision against active document -> updated_to_latest
        assert item_by_src[source_collision_id].disposition == "updated_to_latest"
        assert item_by_src[source_collision_id].issuer == "hyundai"

        # 3. historical object unavailable, product absent in discovery -> past_excluded
        assert item_by_src[source_obj_unavail_id].disposition == "past_excluded"

        # 4. active current revision with missing object on disk -> current_error
        assert item_by_src[source_current_error_id].disposition == "current_error"

        # Summary
        assert plan.summary["disposition_counts"] == {
            "current_error": 1,
            "past_excluded": 1,
            "updated_to_latest": 2,
        }

        # Verify DB is untouched prior to apply
        assert state.remediated_revisions() == ()

        # Apply remediation
        applied_count = apply_unresolved_remediation(state, plan)
        assert applied_count == 3  # 2 updated_to_latest + 1 past_excluded, current_error not remediated

        # Verify state DB has the records
        remediated_rows = state.remediated_revisions()
        assert len(remediated_rows) == 4
        assert (
            len([r for r in remediated_rows if r["disposition"] in ("updated_to_latest", "past_excluded")])
            == 3
        )
        assert len([r for r in remediated_rows if r["disposition"] == "current_error"]) == 1

        assert state.is_revision_remediated(source_meta_unres_id, pdf_sha1) is True
        assert state.is_revision_remediated(source_collision_id, pdf_sha2) is True
        assert state.is_revision_remediated(source_obj_unavail_id, pdf_sha3) is True
        assert state.is_revision_remediated(source_current_error_id, pdf_sha4) is False

        # Idempotence: re-applying causes no duplicate keys or errors
        reapplied = apply_unresolved_remediation(state, plan)
        assert reapplied == 3
        assert len(state.remediated_revisions()) == 4


@pytest.mark.asyncio
async def test_preserve_inputs_with_missing_source_metadata_does_not_fail(tmp_path: Path) -> None:
    """Live bug reproduction: preserve_inputs previously raised issuer_scope_revision_source_missing."""
    from unittest.mock import MagicMock

    db_path = tmp_path / "worker.state.sqlite3"
    with WorkerState(db_path) as state:
        source_id_hana = "source_" + "e" * 64
        pdf_sha_hana = "f" * 64

        state.connection.execute(
            """INSERT INTO pdf_cache_source
               (source_id, issuer, product_code, document_type, source_url, source_version,
                source_post_id, discovery_sha256, first_observed_at, last_observed_at, last_verified_at)
               VALUES (?, 'hana', '15758', 'summary', 'https://hana.example/p.pdf', 'v1', 'post', ?, ?, ?, ?)""",
            (
                source_id_hana,
                "e" * 64,
                "2026-10-18T00:00:00+00:00",
                "2026-10-18T00:00:00+00:00",
                "2026-10-18T00:00:00+00:00",
            ),
        )

        # Baseline seal has the missing metadata entry for hana
        mock_source = MagicMock(spec=ReuseSource)
        mock_source.manifest = MagicMock()
        mock_source.manifest.issuer_codes = ("woori", "hana")
        mock_source.documents = {}
        mock_source.revisions = {}
        mock_source.failed = []
        mock_source.unsupported = []
        mock_source.connect.return_value.__enter__.return_value = MagicMock()
        mock_source.seal = {
            "v5_metrics": {
                "historical_revision_unresolved_identities": [
                    {
                        "source_id": source_id_hana,
                        "pdf_sha256": pdf_sha_hana,
                        "reason_codes": ["source_metadata_unresolved"],
                    }
                ],
                "structure_fallback_documents": [],
            }
        }

        mock_pipeline = MagicMock()
        mock_pipeline.state = state
        mock_pipeline.state_dir = tmp_path
        mock_pipeline.selected_issuers = ("woori",)
        mock_pipeline.adapters = ()

        # Previously, preserve_inputs raised PartialExecutionError("issuer_scope_revision_source_missing", "scope")
        # Now it succeeds and safely excludes the irrecoverable non-selected historical identity
        acquired, processed, failed, unsupported = preserve_inputs(mock_pipeline, mock_source, "run_test_019")
        assert acquired == ()
        assert processed == ()
        assert failed == ()
        assert unsupported == ()
        assert mock_pipeline._preserved_unresolved == ()


def test_normal_absence_vs_failure_distinction(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    with WorkerState(db_path) as state:
        source_id = "source_" + "7" * 64
        pdf_sha = "8" * 64
        state.connection.execute(
            """INSERT INTO pdf_cache_source
               (source_id, issuer, product_code, document_type, source_url, source_version,
                source_post_id, discovery_sha256, first_observed_at, last_observed_at, last_verified_at)
               VALUES (?, 'hyundai', 'CARD_OLD', 'summary', 'https://example/old.pdf', 'v1', 'post', ?, ?, ?, ?)""",
            (
                source_id,
                "7" * 64,
                "2026-10-18T00:00:00+00:00",
                "2026-10-18T00:00:00+00:00",
                "2026-10-18T00:00:00+00:00",
            ),
        )
        seal_data = {
            "v5_metrics": {
                "historical_revision_unresolved_identities": [
                    {
                        "source_id": source_id,
                        "pdf_sha256": pdf_sha,
                        "reason_codes": ["source_metadata_unresolved"],
                    }
                ]
            }
        }

        # 1. Normal absence: collection completed successfully, CARD_OLD not in discovery -> past_excluded
        plan_normal = plan_unresolved_remediation(
            state,
            seal_data,
            current_discovery={"hyundai": {"CARD_NEW"}},
        )
        assert plan_normal.items[0].disposition == "past_excluded"

        # 2. Failed collection: hyundai had a collection/processing failure on CARD_OLD -> current_error
        plan_failed = plan_unresolved_remediation(
            state,
            seal_data,
            current_discovery={"hyundai": set()},
            current_failed_products={"hyundai": {"CARD_OLD"}},
        )
        assert plan_failed.items[0].disposition == "current_error"


def test_cli_remediate_unresolved_history_dry_run_and_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from test_local_serving_and_backup import create_sample_generation

    from cardrag_worker.cli import app
    from cardrag_worker.local_publisher import LocalServingTransport
    from cardrag_worker.state import WorkerState

    state_dir = tmp_path / "worker-state"
    state_dir.mkdir(parents=True)
    db_path = state_dir / "worker-state.sqlite3"

    serving_dir = tmp_path / "serving"
    transport = LocalServingTransport(serving_dir, channel="stable")
    manifest_obj, ready_obj, pointer_obj, db_bytes, pdf_bytes = create_sample_generation("gen_base")
    db_file = tmp_path / "index.sqlite3"
    db_file.write_bytes(db_bytes)
    pdf_file = tmp_path / "sample.pdf"
    pdf_file.write_bytes(pdf_bytes)
    pdf_sample_sha = manifest_obj.documents[0].pdf.sha256
    unique_objects = [(pdf_file, "application/pdf", pdf_sample_sha, len(pdf_bytes))]
    asyncio.run(
        transport.publish(
            generation_id="gen_base",
            database=db_file,
            manifest=manifest_obj.model_dump(mode="json"),
            vectors=None,
            unique_objects=unique_objects,
        )
    )

    runs_dir = state_dir / "runs" / "run_base" / "sealed"
    runs_dir.mkdir(parents=True)
    seal_file = runs_dir / "publish.json"

    source_id = "source_" + "9" * 64
    pdf_sha = "9" * 64
    with WorkerState(db_path) as state:
        state.connection.execute(
            """INSERT INTO pdf_cache_source
               (source_id, issuer, product_code, document_type, source_url, source_version,
                source_post_id, discovery_sha256, first_observed_at, last_observed_at, last_verified_at)
               VALUES (?, 'hyundai', 'CARD_9', 'summary', 'https://example/9.pdf', 'v1', 'post', ?, ?, ?, ?)""",
            (
                source_id,
                "9" * 64,
                "2026-10-18T00:00:00+00:00",
                "2026-10-18T00:00:00+00:00",
                "2026-10-18T00:00:00+00:00",
            ),
        )
        state.connection.execute(
            """INSERT INTO pdf_cache_object (pdf_sha256, size_bytes, page_count, relative_path, created_at, last_verified_at)
               VALUES (?, 100, 1, 'objects/9.pdf', ?, ?)""",
            (pdf_sha, "2026-10-18T00:00:00+00:00", "2026-10-18T00:00:00+00:00"),
        )
        state.connection.execute(
            """INSERT INTO pdf_cache_source_revision
               (source_id, pdf_sha256, pdf_size_bytes, page_count, final_url,
                first_observed_at, last_observed_at, verified_at, superseded_at)
               VALUES (?, ?, 100, 1, 'https://example/9.pdf', ?, ?, ?, ?)""",
            (
                source_id,
                pdf_sha,
                "2026-10-18T00:00:00+00:00",
                "2026-10-18T00:00:00+00:00",
                "2026-10-18T00:00:00+00:00",
                "2026-10-18T00:00:00+00:00",
            ),
        )
        # Record run as retained publication
        state.connection.execute(
            "INSERT INTO run (run_id, started_at, finished_at, status) VALUES ('run_base', ?, ?, 'succeeded')",
            ("2026-10-18T00:00:00+00:00", "2026-10-18T01:00:00+00:00"),
        )
        state.connection.execute(
            "INSERT INTO publish (generation_id, run_id, corpus_sha256, contract_sha256, serving_sha256, status, published_at, details_json) VALUES ('gen_base', 'run_base', ?, ?, ?, 'ready', ?, '{}')",
            (
                manifest_obj.corpus_sha256,
                manifest_obj.contract_sha256,
                manifest_obj.serving_database.sha256,
                "2026-10-18T01:00:00+00:00",
            ),
        )

    from cardrag_worker.revision_history_v5 import (
        UnresolvedRevisionIdentityV5,
        canonical_unresolved_revision_ledger_v5,
        unresolved_revision_ledger_sha256_v5,
    )

    canonical_test = canonical_unresolved_revision_ledger_v5(
        [UnresolvedRevisionIdentityV5(source_id, pdf_sha, "document_identity_collision")]
    )
    seal_file.write_text(
        json.dumps(
            {
                "generation_id": "gen_base",
                "run_id": "run_base",
                "database_sha256": manifest_obj.serving_database.sha256,
                "manifest": manifest_obj.model_dump(mode="json"),
                "v5_metrics": {
                    "historical_revision_unresolved_count": len(canonical_test),
                    "historical_revision_unresolved_sha256": unresolved_revision_ledger_sha256_v5(
                        canonical_test
                    ),
                    "historical_revision_unresolved_identities": list(canonical_test),
                },
            }
        )
    )

    monkeypatch.setenv("CARDRAG_WORKER_STATE_DIR", str(state_dir))
    monkeypatch.setenv("CARDRAG_PUBLICATION_TRANSPORT", "local")
    monkeypatch.setenv("CARDRAG_SERVING_DIR", str(serving_dir))
    monkeypatch.setenv("CARDRAG_CHANNEL", "stable")

    runner = CliRunner()

    # 1. Test default invocation (dry-run)
    result_default = runner.invoke(app, ["remediate-unresolved-history"])
    assert result_default.exit_code == 0
    parsed_default = json.loads(result_default.stdout)
    assert parsed_default["status"] == "succeeded"
    assert parsed_default["mode"] == "dry_run"
    assert parsed_default["applied_count"] == 0
    assert parsed_default["plan"]["total_identities"] == 1

    # 2. Test explicit --dry-run option
    result_dry = runner.invoke(app, ["remediate-unresolved-history", "--dry-run"])
    assert result_dry.exit_code == 0
    parsed_dry = json.loads(result_dry.stdout)
    assert parsed_dry["status"] == "succeeded"
    assert parsed_dry["mode"] == "dry_run"

    with WorkerState(db_path) as state:
        assert state.is_revision_remediated(source_id, pdf_sha) is False

    # 3. Test invalid seal rejection (corrupted ledger sha)
    corrupted_seal_path = tmp_path / "corrupted_seal.json"
    corrupted_seal_path.write_text(
        json.dumps(
            {
                "generation_id": "gen_base",
                "run_id": "run_base",
                "database_sha256": manifest_obj.serving_database.sha256,
                "manifest": manifest_obj.model_dump(mode="json"),
                "v5_metrics": {
                    "historical_revision_unresolved_count": len(canonical_test),
                    "historical_revision_unresolved_sha256": "INVALID_SHA256",
                    "historical_revision_unresolved_identities": list(canonical_test),
                },
            }
        )
    )
    result_bad = runner.invoke(
        app, ["remediate-unresolved-history", "--seal-path", str(corrupted_seal_path), "--apply"]
    )
    assert result_bad.exit_code == 1
    parsed_bad = json.loads(result_bad.stdout)
    assert parsed_bad["status"] == "failed"
    assert "mismatch" in parsed_bad["error"]

    with WorkerState(db_path) as state:
        assert state.is_revision_remediated(source_id, pdf_sha) is False

    # 4. Test apply
    result_apply = runner.invoke(app, ["remediate-unresolved-history", "--apply"])
    assert result_apply.exit_code == 0
    parsed_apply = json.loads(result_apply.stdout)
    assert parsed_apply["status"] == "succeeded"
    assert parsed_apply["mode"] == "apply"
    assert parsed_apply["applied_count"] == 1

    with WorkerState(db_path) as state:
        assert state.is_revision_remediated(source_id, pdf_sha) is True


@pytest.mark.asyncio
async def test_selected_run_preserves_serving_and_skips_nonselected_adapters(
    corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from cardrag_worker.partial_execution import ReuseSource
    from cardrag_worker.webdav import RemoteGenerationIdentity

    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus

    m = prior.manifest
    dav.current = RemoteGenerationIdentity(
        m.generation_id, m.corpus_sha256, m.contract_sha256, m.schema_version, m.serving_schema
    )

    runner = factory()
    base_adapter = runner.adapters[0]
    base_rec = base_adapter.record

    from test_pipeline_v5 import _Adapter

    # Create two adapters: testbank (selected) and otherbank (non-selected)
    other_rec = replace(
        base_rec,
        issuer="otherbank",
        product_code="OTHER1",
        source_post_id="post_other",
        source_url="https://cards.example/other.pdf",
    )

    selected_adapter = _Adapter(base_rec)
    nonselected_adapter = _Adapter(other_rec)
    nonselected_adapter.spec = replace(nonselected_adapter.spec, code="otherbank", sort_order=2)

    selected_discovered = False
    orig_sel = selected_adapter.discover_current

    async def spy_sel(client):
        nonlocal selected_discovered
        selected_discovered = True
        return await orig_sel(client)

    selected_adapter.discover_current = spy_sel

    nonselected_discovered = False
    orig_nonsel = nonselected_adapter.discover_current

    async def spy_nonsel(client):
        nonlocal nonselected_discovered
        nonselected_discovered = True
        return await orig_nonsel(client)

    nonselected_adapter.discover_current = spy_nonsel

    # Initial 2-issuer run to create a multi-issuer baseline
    pipe_init = WorkerPipeline(
        state=state,
        state_dir=prior.root,
        adapters=[selected_adapter, nonselected_adapter],
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=dav,
        maximum_attempts=1,
    )
    res_init = await pipe_init.run()
    assert res_init.status in {"succeeded", "no_change"}

    # Update baseline pointer
    baseline = ReuseSource(prior.root, res_init.run_id, state.path)
    dav.current = RemoteGenerationIdentity(
        baseline.manifest.generation_id,
        baseline.manifest.corpus_sha256,
        baseline.manifest.contract_sha256,
        baseline.manifest.schema_version,
        baseline.manifest.serving_schema,
    )

    # Inject simulated past unresolved entry into baseline seal
    seal_path = prior.root / "runs" / res_init.run_id / "sealed" / "publish.json"
    seal_data = json.loads(seal_path.read_text())
    past_unresolved_src = "source_" + "8" * 64
    past_unresolved_sha = "7" * 64
    # Insert row into pdf_cache_source so issuer can be resolved as otherbank
    state.connection.execute(
        """INSERT INTO pdf_cache_source
           (source_id, issuer, product_code, document_type, source_url, source_version,
            source_post_id, discovery_sha256, first_observed_at, last_observed_at, last_verified_at)
           VALUES (?, 'otherbank', 'OLD_PROD', 'summary', 'https://example/old.pdf', 'v1', 'post', ?, ?, ?, ?)""",
        (
            past_unresolved_src,
            "8" * 64,
            "2026-10-18T00:00:00+00:00",
            "2026-10-18T00:00:00+00:00",
            "2026-10-18T00:00:00+00:00",
        ),
    )
    canonical_injected = canonical_unresolved_revision_ledger_v5(
        [UnresolvedRevisionIdentityV5(past_unresolved_src, past_unresolved_sha, "source_metadata_unresolved")]
    )
    seal_data["v5_metrics"]["historical_revision_unresolved_identities"] = list(canonical_injected)
    seal_data["v5_metrics"]["historical_revision_unresolved_count"] = len(canonical_injected)
    seal_data["v5_metrics"]["historical_revision_unresolved_sha256"] = unresolved_revision_ledger_sha256_v5(
        canonical_injected
    )
    seal_path.write_text(json.dumps(seal_data))

    # Reset spy flags
    selected_discovered = False
    nonselected_discovered = False
    # Update selected adapter to version 2 to trigger genuine processing
    selected_adapter.record = replace(base_rec, source_version="2", source_post_id="post_v2")

    # Run selected-only pipeline for testbank
    pipe_selected = WorkerPipeline(
        state=state,
        state_dir=prior.root,
        adapters=[selected_adapter, nonselected_adapter],
        selected_issuers=("testbank",),
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=dav,
        maximum_attempts=1,
    )

    res_selected = await pipe_selected.run()
    assert res_selected.status == "succeeded"

    # Verify:
    # 1. Non-selected adapter was NOT discovered or called
    assert selected_discovered is True
    assert nonselected_discovered is False

    # 2. Both issuers are preserved in the published generation
    new_gen = await dav.validated_current_generation()
    assert new_gen is not None
    new_source = ReuseSource(prior.root, res_selected.run_id, state.path)
    issuers_in_manifest = {d.issuer for d in new_source.manifest.documents}
    assert "testbank" in issuers_in_manifest
    assert "otherbank" in issuers_in_manifest

    # 3. Irrecoverable past unresolved entry of otherbank did not block and is excluded
    assert past_unresolved_src not in [
        e["source_id"] for e in new_source.seal["v5_metrics"]["historical_revision_unresolved_identities"]
    ]


def test_disk_validation_active_vs_historical_revision(tmp_path: Path) -> None:
    """Verifies that missing CAS objects on disk are classified as current_error for active revisions."""
    db_path = tmp_path / "worker.state.sqlite3"
    objects_root = tmp_path / "objects" / "sha256"
    objects_root.mkdir(parents=True)
    now_iso = datetime.now(UTC).isoformat()
    yesterday_iso = datetime(2026, 10, 18, tzinfo=UTC).isoformat()

    active_src = "source_" + "a" * 64
    active_sha = "1" * 64

    hist_src = "source_" + "b" * 64
    hist_sha = "2" * 64

    with WorkerState(db_path) as state:
        # Active source & revision in DB, object row in DB, but NO file on disk
        state.connection.execute(
            """INSERT INTO pdf_cache_source
               (source_id, issuer, product_code, document_type, source_url, source_version,
                source_post_id, discovery_sha256, first_observed_at, last_observed_at, last_verified_at)
               VALUES (?, 'woori', 'P_ACT', 'summary', 'https://example/act.pdf', 'v1', 'post', ?, ?, ?, ?)""",
            (active_src, "a" * 64, yesterday_iso, yesterday_iso, yesterday_iso),
        )
        state.connection.execute(
            """INSERT INTO pdf_cache_object (pdf_sha256, size_bytes, page_count, relative_path, created_at, last_verified_at)
               VALUES (?, 100, 1, 'objects/sha256/11/' || ?, ?, ?)""",
            (active_sha, active_sha, yesterday_iso, yesterday_iso),
        )
        state.connection.execute(
            """INSERT INTO pdf_cache_source_revision
               (revision_id, source_id, pdf_sha256, pdf_size_bytes, page_count, final_url, first_observed_at, last_observed_at, verified_at)
               VALUES (1, ?, ?, 100, 1, 'https://example/act.pdf', ?, ?, ?)""",
            (active_src, active_sha, yesterday_iso, yesterday_iso, yesterday_iso),
        )

        # Historical source & revision (superseded in DB), object row in DB, but NO file on disk
        state.connection.execute(
            """INSERT INTO pdf_cache_source
               (source_id, issuer, product_code, document_type, source_url, source_version,
                source_post_id, discovery_sha256, first_observed_at, last_observed_at, last_verified_at, superseded_at)
               VALUES (?, 'woori', 'P_HIST', 'summary', 'https://example/hist.pdf', 'v1', 'post', ?, ?, ?, ?, ?)""",
            (hist_src, "b" * 64, yesterday_iso, yesterday_iso, yesterday_iso, now_iso),
        )
        state.connection.execute(
            """INSERT INTO pdf_cache_object (pdf_sha256, size_bytes, page_count, relative_path, created_at, last_verified_at)
               VALUES (?, 100, 1, 'objects/sha256/22/' || ?, ?, ?)""",
            (hist_sha, hist_sha, yesterday_iso, yesterday_iso),
        )
        state.connection.execute(
            """INSERT INTO pdf_cache_source_revision
               (revision_id, source_id, pdf_sha256, pdf_size_bytes, page_count, final_url, first_observed_at, last_observed_at, verified_at, superseded_at)
               VALUES (2, ?, ?, 100, 1, 'https://example/hist.pdf', ?, ?, ?, ?)""",
            (hist_src, hist_sha, yesterday_iso, yesterday_iso, yesterday_iso, now_iso),
        )

        seal_data = {
            "generation_id": "gen_disk_test",
            "run_id": "run_disk_test",
            "v5_metrics": {
                "historical_revision_unresolved_identities": [
                    {
                        "source_id": active_src,
                        "pdf_sha256": active_sha,
                        "reason_codes": ["pdf_cache_object_unavailable"],
                    },
                    {
                        "source_id": hist_src,
                        "pdf_sha256": hist_sha,
                        "reason_codes": ["pdf_cache_object_unavailable"],
                    },
                ]
            },
        }

        plan = plan_unresolved_remediation(
            state,
            seal_data,
            objects_root=objects_root,
        )

        item_by_src = {item.source_id: item for item in plan.items}

        # 1. Active document with missing file on disk MUST be current_error
        assert item_by_src[active_src].disposition == "current_error"
        assert item_by_src[active_src].pdf_cache_object_exists is True
        assert item_by_src[active_src].pdf_cache_object_disk_valid is False
        assert "Active current cache revision object is missing" in item_by_src[active_src].explanation

        # 2. Historical document with missing file on disk is past_excluded
        assert item_by_src[hist_src].disposition == "past_excluded"
        assert item_by_src[hist_src].pdf_cache_object_exists is True
        assert item_by_src[hist_src].pdf_cache_object_disk_valid is False

        # Apply plan
        apply_unresolved_remediation(state, plan)

        # Active revision error is NOT remediated
        assert state.is_revision_remediated(active_src, active_sha) is False
        # Historical revision is remediated (past_excluded)
        assert state.is_revision_remediated(hist_src, hist_sha) is True

        # 3. Test re-evaluation un-suppression:
        # Pre-seed active_src as past_excluded
        state.record_remediated_revision(
            source_id=active_src,
            pdf_sha256=active_sha,
            reason_code="pdf_cache_object_unavailable",
            disposition="past_excluded",
        )
        assert state.is_revision_remediated(active_src, active_sha) is True

        # Now re-apply plan where active_src evaluates to current_error
        apply_unresolved_remediation(state, plan)

        # MUST be un-suppressed (returns False)
        assert state.is_revision_remediated(active_src, active_sha) is False


def test_validate_unresolved_ledger_security_and_authenticity(tmp_path: Path) -> None:
    """Verifies that invalid, corrupted, or unverified seals are rejected before modifying DB."""
    from cardrag_worker.unresolved_remediation import validate_unresolved_ledger_in_seal

    db_path = tmp_path / "worker.state.sqlite3"
    with WorkerState(db_path) as state:
        state.connection.execute(
            "INSERT INTO run (run_id, started_at, finished_at, status) VALUES ('run_valid', ?, ?, 'succeeded')",
            ("2026-10-18T00:00:00+00:00", "2026-10-18T01:00:00+00:00"),
        )
        state.connection.execute(
            """INSERT INTO publish (generation_id, run_id, corpus_sha256, contract_sha256, serving_sha256, status, published_at, details_json)
               VALUES ('gen_valid', 'run_valid', 'c1', 'c2', 's1', 'ready', ?, '{}')""",
            ("2026-10-18T01:00:00+00:00",),
        )
        state.connection.execute(
            "INSERT INTO run (run_id, started_at, finished_at, status) VALUES ('run_failed', ?, ?, 'failed')",
            ("2026-10-18T00:00:00+00:00", "2026-10-18T01:00:00+00:00"),
        )
        state.connection.execute(
            "INSERT INTO run (run_id, started_at, finished_at, status) VALUES ('run_unpub', ?, ?, 'succeeded')",
            ("2026-10-18T00:00:00+00:00", "2026-10-18T01:00:00+00:00"),
        )

        valid_src = "source_" + "e" * 64
        valid_sha = "f" * 64
        canonical = canonical_unresolved_revision_ledger_v5(
            [UnresolvedRevisionIdentityV5(valid_src, valid_sha, "document_identity_collision")]
        )
        valid_hash = unresolved_revision_ledger_sha256_v5(canonical)

        # 1. Valid seal passes
        good_seal = {
            "generation_id": "gen_valid",
            "run_id": "run_valid",
            "v5_metrics": {
                "historical_revision_unresolved_count": len(canonical),
                "historical_revision_unresolved_sha256": valid_hash,
                "historical_revision_unresolved_identities": list(canonical),
            },
        }
        res = validate_unresolved_ledger_in_seal(good_seal, state=state, require_verified_run=True)
        assert len(res) == 1

        # 1b. Unpublished run (status=succeeded in run, but no row in publish) rejected
        unpub_seal = {
            "generation_id": "gen_unpub",
            "run_id": "run_unpub",
            "v5_metrics": {
                "historical_revision_unresolved_count": len(canonical),
                "historical_revision_unresolved_sha256": valid_hash,
                "historical_revision_unresolved_identities": list(canonical),
            },
        }
        with pytest.raises(ValueError, match="not a verified ready publication"):
            validate_unresolved_ledger_in_seal(unpub_seal, state=state, require_verified_run=True)

        # 2. Corrupted sha256 rejected
        bad_hash_seal = {
            "generation_id": "gen_valid",
            "run_id": "run_valid",
            "v5_metrics": {
                "historical_revision_unresolved_count": len(canonical),
                "historical_revision_unresolved_sha256": "INVALID_SHA256",
                "historical_revision_unresolved_identities": list(canonical),
            },
        }
        with pytest.raises(ValueError, match="mismatch"):
            validate_unresolved_ledger_in_seal(bad_hash_seal, state=state, require_verified_run=True)

        # 3. Unverified/failed run rejected
        failed_run_seal = {
            "generation_id": "gen_valid",
            "run_id": "run_failed",
            "v5_metrics": {
                "historical_revision_unresolved_count": len(canonical),
                "historical_revision_unresolved_sha256": valid_hash,
                "historical_revision_unresolved_identities": list(canonical),
            },
        }
        with pytest.raises(ValueError, match="not a verified completed publication run"):
            validate_unresolved_ledger_in_seal(failed_run_seal, state=state, require_verified_run=True)

        # 4. Unknown reason code rejected
        unknown_reason_seal = {
            "generation_id": "gen_valid",
            "run_id": "run_valid",
            "v5_metrics": {
                "historical_revision_unresolved_count": 1,
                "historical_revision_unresolved_sha256": valid_hash,
                "historical_revision_unresolved_identities": [
                    {
                        "source_id": valid_src,
                        "pdf_sha256": valid_sha,
                        "reason_codes": ["unsupported_unknown_reason"],
                    }
                ],
            },
        }
        with pytest.raises(ValueError, match="unsupported unresolved reason code"):
            validate_unresolved_ledger_in_seal(unknown_reason_seal, state=state)

        # 5. Non-canonical ordering rejected
        src_a = "source_" + "1" * 64
        src_b = "source_" + "2" * 64
        unordered_seal = {
            "generation_id": "gen_valid",
            "run_id": "run_valid",
            "v5_metrics": {
                "historical_revision_unresolved_count": 2,
                "historical_revision_unresolved_sha256": valid_hash,
                "historical_revision_unresolved_identities": [
                    {
                        "source_id": src_b,
                        "pdf_sha256": valid_sha,
                        "reason_codes": ["document_identity_collision"],
                    },
                    {
                        "source_id": src_a,
                        "pdf_sha256": valid_sha,
                        "reason_codes": ["document_identity_collision"],
                    },
                ],
            },
        }
        with pytest.raises(ValueError, match="not canonical or contains duplicates"):
            validate_unresolved_ledger_in_seal(unordered_seal, state=state)


def test_pipeline_discovery_and_acquisition_failures(tmp_path: Path) -> None:
    """Verifies that discovery failure or incomplete collection does not exclude past identities."""
    db_path = tmp_path / "worker.state.sqlite3"
    with WorkerState(db_path) as state:
        source_id = "source_" + "3" * 64
        pdf_sha = "4" * 64
        state.connection.execute(
            """INSERT INTO pdf_cache_source
               (source_id, issuer, product_code, document_type, source_url, source_version,
                source_post_id, discovery_sha256, first_observed_at, last_observed_at, last_verified_at)
               VALUES (?, 'woori', 'P_WOORI', 'summary', 'https://example/w.pdf', 'v1', 'post', ?, ?, ?, ?)""",
            (
                source_id,
                "3" * 64,
                "2026-10-18T00:00:00+00:00",
                "2026-10-18T00:00:00+00:00",
                "2026-10-18T00:00:00+00:00",
            ),
        )

        seal_data = {
            "generation_id": "gen_fail_test",
            "run_id": "run_fail_test",
            "v5_metrics": {
                "historical_revision_unresolved_identities": [
                    {
                        "source_id": source_id,
                        "pdf_sha256": pdf_sha,
                        "reason_codes": ["document_identity_collision"],
                    }
                ]
            },
        }

        # 1. Selected issuer collection failed (not in successful_issuers)
        plan_failed = plan_unresolved_remediation(
            state,
            seal_data,
            selected_issuers={"woori"},
            successful_issuers=set(),  # woori failed
        )
        assert plan_failed.items[0].disposition == "current_error"
        assert "collection failed or incomplete" in plan_failed.items[0].explanation

        # 2. Selected issuer succeeded, but product was in discovery and not acquired
        plan_not_acquired = plan_unresolved_remediation(
            state,
            seal_data,
            selected_issuers={"woori"},
            successful_issuers={"woori"},
            current_discovery={"woori": {"P_WOORI"}},
            current_acquired_products={"woori": set()},  # failed download
        )
        assert plan_not_acquired.items[0].disposition == "current_error"
        assert "not successfully acquired" in plan_not_acquired.items[0].explanation


@pytest.mark.asyncio
async def test_corrupted_baseline_seal_ledger_fails_fast_in_pipeline(corpus) -> None:
    """Verifies that a pipeline run fails fast when the baseline seal unresolved ledger is corrupted."""
    pipeline_factory, baseline_source, state, _, _, _, dav = corpus

    seal_path = baseline_source.directory / "sealed" / "publish.json"
    seal_data = json.loads(seal_path.read_text())
    past_src = "source_" + "1" * 64
    past_sha = "2" * 64
    canonical = canonical_unresolved_revision_ledger_v5(
        [UnresolvedRevisionIdentityV5(past_src, past_sha, "source_metadata_unresolved")]
    )
    seal_data["v5_metrics"]["historical_revision_unresolved_identities"] = list(canonical)
    seal_data["v5_metrics"]["historical_revision_unresolved_count"] = len(canonical)
    seal_data["v5_metrics"]["historical_revision_unresolved_sha256"] = "CORRUPTED_SHA256"
    seal_path.write_text(json.dumps(seal_data))

    from cardrag_worker.webdav import RemoteGenerationIdentity

    dav.current = RemoteGenerationIdentity(
        generation_id=baseline_source.manifest.generation_id,
        corpus_sha256=baseline_source.manifest.corpus_sha256,
        contract_sha256=baseline_source.manifest.contract_sha256,
        generation_schema=baseline_source.manifest.schema_version,
        serving_schema=baseline_source.manifest.serving_schema,
    )

    from cardrag_worker.partial_execution import PartialExecutionError

    pipe = pipeline_factory()
    with pytest.raises((RuntimeError, PartialExecutionError)):
        await pipe.run()

    assert state.is_revision_remediated(past_src, past_sha) is False


@pytest.mark.asyncio
async def test_ocr_failure_does_not_persist_updated_to_latest(corpus) -> None:
    """Verifies that if OCR fails on a current product, past unresolved identities are NOT marked updated_to_latest."""
    pipeline_factory, baseline_source, state, _, _, ocr, dav = corpus

    from cardrag_worker.webdav import RemoteGenerationIdentity

    dav.current = RemoteGenerationIdentity(
        generation_id=baseline_source.manifest.generation_id,
        corpus_sha256=baseline_source.manifest.corpus_sha256,
        contract_sha256=baseline_source.manifest.contract_sha256,
        generation_schema=baseline_source.manifest.schema_version,
        serving_schema=baseline_source.manifest.serving_schema,
    )

    seal_path = baseline_source.directory / "sealed" / "publish.json"
    seal_data = json.loads(seal_path.read_text())
    past_src = "source_" + "5" * 64
    past_sha = "6" * 64
    state.connection.execute(
        """INSERT INTO pdf_cache_source
           (source_id, issuer, product_code, document_type, source_url, source_version,
            source_post_id, discovery_sha256, first_observed_at, last_observed_at, last_verified_at)
           VALUES (?, 'testbank', 'test-001', 'summary', 'https://cards.example/old.pdf', 'v0', 'post0', ?, ?, ?, ?)""",
        (
            past_src,
            "5" * 64,
            "2026-10-18T00:00:00+00:00",
            "2026-10-18T00:00:00+00:00",
            "2026-10-18T00:00:00+00:00",
        ),
    )
    canonical = canonical_unresolved_revision_ledger_v5(
        [UnresolvedRevisionIdentityV5(past_src, past_sha, "source_metadata_unresolved")]
    )
    seal_data["v5_metrics"]["historical_revision_unresolved_identities"] = list(canonical)
    seal_data["v5_metrics"]["historical_revision_unresolved_count"] = len(canonical)
    seal_data["v5_metrics"]["historical_revision_unresolved_sha256"] = unresolved_revision_ledger_sha256_v5(
        canonical
    )
    seal_path.write_text(json.dumps(seal_data))

    from cardrag_worker.ocr import ProviderSystemicError
    from cardrag_worker.pipeline import OCRSystemicFailureError

    async def fail_resolve(*args, **kwargs):
        raise ProviderSystemicError("ocr_unexpected_error", "Simulated OCR systemic failure")

    ocr.resolve = fail_resolve

    from dataclasses import replace

    pipe = pipeline_factory()
    pipe.adapters[0].record = replace(pipe.adapters[0].record, source_version="2", source_post_id="post_v2")
    with pytest.raises(OCRSystemicFailureError):
        await pipe.run()

    # Crucial assertion: updated_to_latest must NOT be committed to state DB
    assert state.is_revision_remediated(past_src, past_sha) is False


@pytest.mark.asyncio
async def test_collect_then_process_remediation_lifecycle(corpus) -> None:
    """Verifies collect phase does not confirm updated_to_latest, while process phase does."""
    from cardrag_worker.partial_execution import ExecutionPlan

    pipeline_factory, baseline_source, state, _, _, ocr, dav = corpus

    from cardrag_worker.webdav import RemoteGenerationIdentity

    dav.current = RemoteGenerationIdentity(
        generation_id=baseline_source.manifest.generation_id,
        corpus_sha256=baseline_source.manifest.corpus_sha256,
        contract_sha256=baseline_source.manifest.contract_sha256,
        generation_schema=baseline_source.manifest.schema_version,
        serving_schema=baseline_source.manifest.serving_schema,
    )

    seal_path = baseline_source.directory / "sealed" / "publish.json"
    seal_data = json.loads(seal_path.read_text())
    from dataclasses import replace

    from cardrag_worker.pdf_cache import PDFSourceIdentity

    rec = pipeline_factory().adapters[0].record
    old = replace(
        rec, source_version="old", source_post_id="old-post", source_url="https://cards.example/old.pdf"
    )
    identity = PDFSourceIdentity.from_source_record(old)
    past_src = identity.source_id
    past_sha = "8" * 64
    state.connection.execute(
        """INSERT INTO pdf_cache_source
           (source_id, issuer, product_code, document_type, source_url, source_version,
            source_post_id, discovery_sha256, first_observed_at, last_observed_at, last_verified_at, superseded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            identity.source_id,
            old.issuer,
            old.product_code,
            old.document_type,
            old.source_url,
            old.source_version,
            old.source_post_id,
            identity.discovery_sha256,
            "2026-10-18T00:00:00+00:00",
            "2026-10-18T00:00:00+00:00",
            "2026-10-18T00:00:00+00:00",
            "2026-10-18T00:00:00+00:00",
        ),
    )
    state.connection.execute(
        """INSERT INTO pdf_cache_object (pdf_sha256, size_bytes, page_count, relative_path, created_at, last_verified_at)
           VALUES (?, 100, 1, 'objects/old.pdf', ?, ?)""",
        (past_sha, "2026-10-18T00:00:00+00:00", "2026-10-18T00:00:00+00:00"),
    )
    state.connection.execute(
        """INSERT INTO pdf_cache_source_revision
           (source_id, pdf_sha256, pdf_size_bytes, page_count, final_url,
            first_observed_at, last_observed_at, verified_at, superseded_at)
           VALUES (?, ?, 100, 1, ?, ?, ?, ?, ?)""",
        (
            identity.source_id,
            past_sha,
            old.source_url,
            "2026-10-18T00:00:00+00:00",
            "2026-10-18T00:00:00+00:00",
            "2026-10-18T00:00:00+00:00",
            "2026-10-18T00:00:00+00:00",
        ),
    )
    canonical = canonical_unresolved_revision_ledger_v5(
        [UnresolvedRevisionIdentityV5(past_src, past_sha, "source_metadata_unresolved")]
    )
    seal_data["v5_metrics"]["historical_revision_unresolved_identities"] = list(canonical)
    seal_data["v5_metrics"]["historical_revision_unresolved_count"] = len(canonical)
    seal_data["v5_metrics"]["historical_revision_unresolved_sha256"] = unresolved_revision_ledger_sha256_v5(
        canonical
    )
    seal_path.write_text(json.dumps(seal_data))
    baseline_source.seal = seal_data

    # 1. Collect phase: acquisition done, OCR skipped
    collect_pipe = pipeline_factory(plan=ExecutionPlan(phase="collect"))
    collect_res = await collect_pipe.run()
    assert collect_res.status == "collected"

    # Pre-OCR collect phase must NOT confirm updated_to_latest
    assert state.is_revision_remediated(past_src, past_sha) is False

    # Audit artifact exists for collect
    inv_file = baseline_source.directory.parent / collect_res.run_id / "remediation-inventory.json"
    assert inv_file.is_file()
    inv_data = json.loads(inv_file.read_text())
    assert inv_data["phase"] == "expansion"

    # 2. Process phase: replay and process corpus via real AcquisitionSource
    from cardrag_worker.acquisition import AcquisitionSource

    acq_source = AcquisitionSource(baseline_source.root, collect_res.run_id, state.path)
    process_pipe = pipeline_factory(
        plan=ExecutionPlan(phase="process", skipped=frozenset({"pdf"}), source_run_id=collect_res.run_id),
        source=acq_source,
    )
    process_res = await process_pipe.run()
    assert process_res.status in {"succeeded", "no_change"}

    # Process phase completed OCR and confirmed updated_to_latest
    assert state.is_revision_remediated(past_src, past_sha) is True

    proc_inv_file = baseline_source.directory.parent / process_res.run_id / "remediation-inventory.json"
    assert proc_inv_file.is_file()
    proc_inv_data = json.loads(proc_inv_file.read_text())
    assert proc_inv_data["phase"] == "process"
    assert proc_inv_data["items"][0]["disposition"] == "updated_to_latest"


def test_cli_rejects_unpublished_and_fabricated_seals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies that CLI --apply rejects unpublished runs and fabricated seals with zero DB mutation."""
    state_dir = tmp_path / "cli-state"
    state_dir.mkdir(parents=True)
    db_path = state_dir / "worker-state.sqlite3"

    with WorkerState(db_path) as state:
        # Completed run that was NEVER published (no row in publish table)
        state.connection.execute(
            "INSERT INTO run (run_id, started_at, finished_at, status) VALUES ('run_never_published', ?, ?, 'succeeded')",
            ("2026-10-18T00:00:00+00:00", "2026-10-18T01:00:00+00:00"),
        )

    fab_seal = tmp_path / "fabricated-seal.json"
    canonical = canonical_unresolved_revision_ledger_v5(
        [UnresolvedRevisionIdentityV5("source_" + "f" * 64, "e" * 64, "source_metadata_unresolved")]
    )
    fab_seal.write_text(
        json.dumps(
            {
                "generation_id": "never_published_generation",
                "run_id": "run_never_published",
                "v5_metrics": {
                    "historical_revision_unresolved_count": len(canonical),
                    "historical_revision_unresolved_sha256": unresolved_revision_ledger_sha256_v5(canonical),
                    "historical_revision_unresolved_identities": list(canonical),
                },
            }
        )
    )

    monkeypatch.setenv("CARDRAG_WORKER_STATE_DIR", str(state_dir))
    monkeypatch.setenv("CARDRAG_PUBLICATION_TRANSPORT", "local")
    monkeypatch.setenv("CARDRAG_SERVING_DIR", str(tmp_path / "serving"))
    monkeypatch.setenv("CARDRAG_CHANNEL", "stable")

    from cardrag_worker.local_publisher import LocalServingTransport
    from cardrag_worker.webdav import RemoteGenerationIdentity

    async def mock_cur(*_args, **_kwargs):
        return RemoteGenerationIdentity(
            "never_published_generation",
            "corpus",
            "contract",
            "cardrag.generation.v5",
            "cardrag.serving-db.v5",
        )

    monkeypatch.setattr(LocalServingTransport, "validated_current_generation", mock_cur)
    runner = CliRunner()

    result = runner.invoke(app, ["remediate-unresolved-history", "--seal-path", str(fab_seal), "--apply"])
    assert result.exit_code == 1
    parsed = json.loads(result.stdout)
    assert parsed["status"] == "failed"
    assert "not a verified ready publication" in parsed["error"]

    with WorkerState(db_path) as state:
        assert state.is_revision_remediated("source_" + "f" * 64, "e" * 64) is False


@pytest.mark.asyncio
async def test_normal_document_plus_drm_succeeds_without_attribute_error(
    corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies that a mix of normal documents and DRM protected PDFs runs cleanly without AttributeError."""
    from test_pipeline_v5 import _install_pdf_http, _MultiAdapter

    from cardrag_worker.webdav import RemoteGenerationIdentity

    factory, prior, state, calls, *_rest, dav = corpus
    m = prior.manifest
    dav.current = RemoteGenerationIdentity(
        m.generation_id, m.corpus_sha256, m.contract_sha256, m.schema_version, m.serving_schema
    )

    pipe = factory()
    rec = pipe.adapters[0].record
    from dataclasses import replace

    protected = replace(
        rec,
        product_code="PROTECTED",
        source_post_id="protected",
        source_url="https://cards.example/protected.pdf",
    )
    pipe.adapters = (_MultiAdapter((rec, protected)),)
    _install_pdf_http(monkeypatch, [b"SCDSA002" + b"\0" * 64], calls)
    result = await pipe.run()
    assert result.status == "succeeded"
    assert result.document_count >= 1
    assert result.unsupported_document_count >= 1

    # Unsupported DRM is properly preserved in manifest and database without crashing
    published = ReuseSource(prior.root, result.run_id, state.path)
    assert published.manifest.counts.documents >= 1
    assert len(published.unsupported) >= 1
    assert published.unsupported[0]["issuer"] == "testbank"
    assert published.unsupported[0]["product_code"] == "PROTECTED"
    assert published.unsupported[0]["protected_magic"] == "SCDSA002"


@pytest.mark.asyncio
async def test_final_ledger_and_corpus_hash_agreement_and_no_change(
    corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies that post-OCR remediation updates the corpus hash to match the final ledger and stabilizes on re-run."""
    from dataclasses import replace

    from cardrag_worker.pdf_cache import PDFSourceIdentity
    from cardrag_worker.webdav import RemoteGenerationIdentity

    factory, prior, state, *_rest, dav = corpus
    m = prior.manifest
    dav.current = RemoteGenerationIdentity(
        m.generation_id, m.corpus_sha256, m.contract_sha256, m.schema_version, m.serving_schema
    )

    rec = factory().adapters[0].record
    old = replace(
        rec, source_version="old", source_post_id="old-post", source_url="https://cards.example/old.pdf"
    )
    identity = PDFSourceIdentity.from_source_record(old)
    doc = next(iter(prior.documents.values()))
    now = "2026-10-19T00:00:00+00:00"

    state.connection.execute(
        """INSERT INTO pdf_cache_source
           (source_id, issuer, product_code, document_type, source_url, source_version,
            source_post_id, discovery_sha256, first_observed_at, last_observed_at, last_verified_at, superseded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            identity.source_id,
            old.issuer,
            old.product_code,
            old.document_type,
            old.source_url,
            old.source_version,
            old.source_post_id,
            identity.discovery_sha256,
            now,
            now,
            now,
            now,
        ),
    )
    state.connection.execute(
        """INSERT INTO pdf_cache_source_revision
           (source_id, pdf_sha256, pdf_size_bytes, page_count, final_url,
            first_observed_at, last_observed_at, verified_at, superseded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            identity.source_id,
            doc.pdf.sha256,
            doc.pdf.size_bytes,
            doc.page_count,
            old.source_url,
            now,
            now,
            now,
            now,
        ),
    )
    ledger = canonical_unresolved_revision_ledger_v5(
        [UnresolvedRevisionIdentityV5(identity.source_id, doc.pdf.sha256, "source_metadata_unresolved")]
    )
    path = prior.directory / "sealed" / "publish.json"
    data = json.loads(path.read_text())
    data["v5_metrics"].update(
        historical_revision_unresolved_count=1,
        historical_revision_unresolved_identities=list(ledger),
        historical_revision_unresolved_sha256=unresolved_revision_ledger_sha256_v5(ledger),
    )
    path.write_text(json.dumps(data))

    result = await factory().run()
    assert result.status in {"succeeded", "no_change"}
    assert state.is_revision_remediated(identity.source_id, doc.pdf.sha256) is True
    # The final corpus hash matches prior serving manifest with 0 unresolved entries
    assert result.corpus_sha256 == prior.manifest.corpus_sha256

    # Re-running with identical inputs must yield no_change without hash drift
    second = await factory().run()
    assert second.status == "no_change"
    assert second.corpus_sha256 == result.corpus_sha256


def test_cli_serving_rollback_and_unlinked_revision_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies CLI rejects application against rolled-back non-current serving and protects unlinked revisions."""
    import asyncio

    from test_local_serving_and_backup import create_sample_generation

    from cardrag_worker.cli import app
    from cardrag_worker.local_publisher import LocalServingTransport
    from cardrag_worker.revision_history_v5 import (
        UnresolvedRevisionIdentityV5,
        canonical_unresolved_revision_ledger_v5,
        unresolved_revision_ledger_sha256_v5,
    )
    from cardrag_worker.state import WorkerState

    state_dir = tmp_path / "rollback-state"
    state_dir.mkdir(parents=True)
    db_path = state_dir / "worker-state.sqlite3"

    serving_dir = tmp_path / "serving"
    transport = LocalServingTransport(serving_dir, channel="stable")

    manifest1, _, _, db1, pdf1 = create_sample_generation("gen_1")
    manifest2, _, _, db2, pdf2 = create_sample_generation("gen_2")

    db_file = tmp_path / "index.sqlite3"
    pdf_file = tmp_path / "sample.pdf"

    # Publish gen_1 then gen_2 to local serving transport
    db_file.write_bytes(db1)
    pdf_file.write_bytes(pdf1)
    asyncio.run(
        transport.publish(
            generation_id="gen_1",
            database=db_file,
            manifest=manifest1.model_dump(mode="json"),
            vectors=None,
            unique_objects=[(pdf_file, "application/pdf", manifest1.documents[0].pdf.sha256, len(pdf1))],
        )
    )

    db_file.write_bytes(db2)
    pdf_file.write_bytes(pdf2)
    asyncio.run(
        transport.publish(
            generation_id="gen_2",
            database=db_file,
            manifest=manifest2.model_dump(mode="json"),
            vectors=None,
            unique_objects=[(pdf_file, "application/pdf", manifest2.documents[0].pdf.sha256, len(pdf2))],
        )
    )

    source_id = "source_" + "a" * 64
    unlinked_sha = "b" * 64
    canonical = canonical_unresolved_revision_ledger_v5(
        [UnresolvedRevisionIdentityV5(source_id, unlinked_sha, "source_metadata_unresolved")]
    )

    with WorkerState(db_path) as state:
        for r_id, g_id, m, t in [
            ("run_1", "gen_1", manifest1, "2026-10-18T01:00:00+00:00"),
            ("run_2", "gen_2", manifest2, "2026-10-18T02:00:00+00:00"),
        ]:
            state.connection.execute(
                "INSERT INTO run (run_id, started_at, finished_at, status) VALUES (?, ?, ?, 'succeeded')",
                (r_id, t, t),
            )
            state.connection.execute(
                "INSERT INTO publish (generation_id, run_id, corpus_sha256, contract_sha256, serving_sha256, status, published_at, details_json) VALUES (?, ?, ?, ?, ?, 'ready', ?, '{}')",
                (g_id, r_id, m.corpus_sha256, m.contract_sha256, m.serving_database.sha256, t),
            )

        state.connection.execute(
            """INSERT INTO pdf_cache_source
               (source_id, issuer, product_code, document_type, source_url, source_version,
                source_post_id, discovery_sha256, first_observed_at, last_observed_at, last_verified_at)
               VALUES (?, 'hyundai', 'CARD_A', 'summary', 'https://example/a.pdf', 'v1', 'post', ?, ?, ?, ?)""",
            (
                source_id,
                "a" * 64,
                "2026-10-18T00:00:00+00:00",
                "2026-10-18T00:00:00+00:00",
                "2026-10-18T00:00:00+00:00",
            ),
        )

    # Create authentic registered seals on disk for both runs
    for r_id, g_id, m in [("run_1", "gen_1", manifest1), ("run_2", "gen_2", manifest2)]:
        s_dir = state_dir / "runs" / r_id / "sealed"
        s_dir.mkdir(parents=True, exist_ok=True)
        seal_p = s_dir / "publish.json"
        seal_p.write_text(
            json.dumps(
                {
                    "generation_id": g_id,
                    "run_id": r_id,
                    "database_sha256": m.serving_database.sha256,
                    "manifest": m.model_dump(mode="json"),
                    "v5_metrics": {
                        "historical_revision_unresolved_count": len(canonical),
                        "historical_revision_unresolved_sha256": unresolved_revision_ledger_sha256_v5(
                            canonical
                        ),
                        "historical_revision_unresolved_identities": list(canonical),
                    },
                }
            )
        )

    seal_gen2_path = state_dir / "runs" / "run_2" / "sealed" / "publish.json"

    monkeypatch.setenv("CARDRAG_WORKER_STATE_DIR", str(state_dir))
    monkeypatch.setenv("CARDRAG_PUBLICATION_TRANSPORT", "local")
    monkeypatch.setenv("CARDRAG_SERVING_DIR", str(serving_dir))
    monkeypatch.setenv("CARDRAG_CHANNEL", "stable")

    runner = CliRunner()

    # 1. Rollback test: Roll back serving pointer to gen_1
    db_file.write_bytes(db1)
    pdf_file.write_bytes(pdf1)
    asyncio.run(
        transport.publish(
            generation_id="gen_1",
            database=db_file,
            manifest=manifest1.model_dump(mode="json"),
            vectors=None,
            unique_objects=[(pdf_file, "application/pdf", manifest1.documents[0].pdf.sha256, len(pdf1))],
        )
    )

    # Attempt to apply gen_2 remediation when current serving is rolled back to gen_1
    result_rollback = runner.invoke(
        app, ["remediate-unresolved-history", "--seal-path", str(seal_gen2_path), "--apply"]
    )
    assert result_rollback.exit_code == 1
    parsed_rollback = json.loads(result_rollback.stdout)
    assert parsed_rollback["status"] == "failed"
    assert "not the current serving publication (current serving is gen_1)" in parsed_rollback["error"]

    with WorkerState(db_path) as state:
        assert state.is_revision_remediated(source_id, unlinked_sha) is False

    # 2. Unlinked revision protection: Move serving pointer back to gen_2
    db_file.write_bytes(db2)
    pdf_file.write_bytes(pdf2)
    asyncio.run(
        transport.publish(
            generation_id="gen_2",
            database=db_file,
            manifest=manifest2.model_dump(mode="json"),
            vectors=None,
            unique_objects=[(pdf_file, "application/pdf", manifest2.documents[0].pdf.sha256, len(pdf2))],
        )
    )

    # Dry-run diagnosis shows current_error for unlinked revision
    result_dry = runner.invoke(app, ["remediate-unresolved-history", "--dry-run"])
    assert result_dry.exit_code == 0
    parsed_dry = json.loads(result_dry.stdout)
    assert parsed_dry["status"] == "succeeded"
    assert parsed_dry["plan"]["items"][0]["disposition"] == "current_error"
    assert "Unverified revision identity not found" in parsed_dry["plan"]["items"][0]["explanation"]

    # Apply attempt succeeds but applied_count is 0 because unlinked revision cannot be excluded
    result_apply = runner.invoke(app, ["remediate-unresolved-history", "--apply"])
    assert result_apply.exit_code == 0
    parsed_apply = json.loads(result_apply.stdout)
    assert parsed_apply["status"] == "succeeded"
    assert parsed_apply["applied_count"] == 0

    with WorkerState(db_path) as state:
        assert state.is_revision_remediated(source_id, unlinked_sha) is False


@pytest.mark.asyncio
async def test_collect_and_process_95_percent_ocr_failure_with_ledger_remediation(
    corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies that a 95% OCR pass rate (19/20) with remediated ledger succeeds cleanly without AttributeError."""
    from dataclasses import replace

    from test_pipeline_v5 import _MultiAdapter

    from cardrag_worker.acquisition import AcquisitionSource
    from cardrag_worker.ocr import OCRValidationError
    from cardrag_worker.partial_execution import ExecutionPlan
    from cardrag_worker.pdf_cache import PDFSourceIdentity
    from cardrag_worker.pipeline import OCRDocumentFailuresError
    from cardrag_worker.webdav import RemoteGenerationIdentity

    factory, prior, state, *_rest, dav = corpus
    m = prior.manifest
    dav.current = RemoteGenerationIdentity(
        m.generation_id, m.corpus_sha256, m.contract_sha256, m.schema_version, m.serving_schema
    )

    # 1. Create 20 distinct records for the same issuer
    pipe = factory()
    base = pipe.adapters[0].record
    records = (base,) + tuple(
        replace(
            base,
            product_code=f"P{i:03d}",
            source_post_id=f"post{i}",
            source_url=f"https://cards.example/{i}.pdf",
            file_name=f"{i}.pdf",
        )
        for i in range(1, 20)
    )
    multi = _MultiAdapter(records)
    pipe.adapters = (multi,)
    init_res = await pipe.run()
    assert init_res.status == "succeeded"

    # Update prior baseline to the 20-document generation
    prior = ReuseSource(prior.root, init_res.run_id, state.path)
    dav.current = RemoteGenerationIdentity(
        prior.manifest.generation_id,
        prior.manifest.corpus_sha256,
        prior.manifest.contract_sha256,
        prior.manifest.schema_version,
        prior.manifest.serving_schema,
    )

    # Inject historical unresolved entry into baseline seal
    old = replace(
        base, source_version="old", source_post_id="old-post", source_url="https://cards.example/old.pdf"
    )
    identity = PDFSourceIdentity.from_source_record(old)
    sample_doc = next(iter(prior.documents.values()))
    now = "2026-10-19T00:00:00+00:00"
    state.connection.execute(
        """INSERT INTO pdf_cache_source
           (source_id, issuer, product_code, document_type, source_url, source_version,
            source_post_id, discovery_sha256, first_observed_at, last_observed_at, last_verified_at, superseded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            identity.source_id,
            old.issuer,
            old.product_code,
            old.document_type,
            old.source_url,
            old.source_version,
            old.source_post_id,
            identity.discovery_sha256,
            now,
            now,
            now,
            now,
        ),
    )
    state.connection.execute(
        """INSERT INTO pdf_cache_source_revision
           (source_id, pdf_sha256, pdf_size_bytes, page_count, final_url,
            first_observed_at, last_observed_at, verified_at, superseded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            identity.source_id,
            sample_doc.pdf.sha256,
            sample_doc.pdf.size_bytes,
            sample_doc.page_count,
            old.source_url,
            now,
            now,
            now,
            now,
        ),
    )
    ledger = canonical_unresolved_revision_ledger_v5(
        [
            UnresolvedRevisionIdentityV5(
                identity.source_id, sample_doc.pdf.sha256, "source_metadata_unresolved"
            )
        ]
    )
    seal_path = prior.directory / "sealed" / "publish.json"
    seal_data = json.loads(seal_path.read_text(encoding="utf-8"))
    seal_data["v5_metrics"].update(
        historical_revision_unresolved_count=1,
        historical_revision_unresolved_identities=list(ledger),
        historical_revision_unresolved_sha256=unresolved_revision_ledger_sha256_v5(ledger),
    )
    seal_path.write_text(json.dumps(seal_data), encoding="utf-8")

    # 2. Run COLLECT phase
    collector = factory(ExecutionPlan(phase="collect"))
    collector.adapters = (multi,)
    collected = await collector.run()
    assert collected.status == "collected"

    source = AcquisitionSource(prior.root, collected.run_id, state.path)
    assert len(source.documents) == 20

    # Pick 1 document to fail OCR
    failed_doc = next(iter(source.documents.values()))
    failed_id = failed_doc.document_id

    process = factory(ExecutionPlan(frozenset({"pdf"}), collected.run_id, phase="process"), source)
    orig_resolve = process.ocr.resolve

    async def mock_resolve(**kwargs):
        if kwargs.get("document_id") == failed_id:
            raise OCRValidationError("Simulated isolatable single document OCR error")
        return await orig_resolve(**kwargs)

    monkeypatch.setattr(process.ocr, "resolve", mock_resolve)

    # 3. Process phase MUST succeed (19/20 = 95%) without AttributeError
    proc_res = await process.run()
    assert proc_res.status == "succeeded"
    assert proc_res.document_count == 20
    assert proc_res.v5_metrics["current_revision_count"] == 19
    assert proc_res.v5_metrics["contract_revision_count"] == 19
    assert state.is_revision_remediated(identity.source_id, sample_doc.pdf.sha256) is True

    # Published manifest has 20 documents: 19 with OCR, 1 with ocr_failure, and valid corpus SHA
    published = ReuseSource(prior.root, proc_res.run_id, state.path)
    assert published.manifest.counts.documents == 20
    assert sum(doc.ocr is not None for doc in published.manifest.documents) == 19
    assert sum(doc.ocr_failure is not None for doc in published.manifest.documents) == 1
    assert published.seal["v5_metrics"]["historical_revision_unresolved_count"] == 0

    # 4. Verify that threshold failure (< 95%, e.g. 2 failures out of 20 = 18/20 = 90%) fails fast
    docs_list = list(source.documents.values())
    failed_two_ids = {docs_list[0].document_id, docs_list[1].document_id}
    process_under_threshold = factory(
        ExecutionPlan(frozenset({"pdf"}), collected.run_id, phase="process"), source
    )

    async def mock_resolve_two_failures(**kwargs):
        if kwargs.get("document_id") in failed_two_ids:
            raise OCRValidationError("Simulated below threshold failure")
        return await orig_resolve(**kwargs)

    monkeypatch.setattr(process_under_threshold.ocr, "resolve", mock_resolve_two_failures)
    with pytest.raises(OCRDocumentFailuresError):
        await process_under_threshold.run()

    # 5. Verify that historical document OCR failure fails fast even if threshold is met
    source.revisions[failed_id]["temporal_status"] = "historical"
    process_hist_fail = factory(ExecutionPlan(frozenset({"pdf"}), collected.run_id, phase="process"), source)

    async def mock_resolve_hist_failure(**kwargs):
        if kwargs.get("document_id") == failed_id:
            raise OCRValidationError("Historical document failure")
        return await orig_resolve(**kwargs)

    monkeypatch.setattr(process_hist_fail.ocr, "resolve", mock_resolve_hist_failure)
    with pytest.raises(OCRDocumentFailuresError):
        await process_hist_fail.run()


def test_cli_transport_candidate_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies CLI properly targets candidate channels from environment configuration."""
    import asyncio

    from test_local_serving_and_backup import create_sample_generation

    from cardrag_worker.local_publisher import LocalServingTransport

    state_dir = tmp_path / "cand-state"
    state_dir.mkdir(parents=True)
    db_path = state_dir / "worker-state.sqlite3"
    serving_dir = tmp_path / "serving"

    channel_name = "candidate-001"
    transport = LocalServingTransport(serving_dir, channel=channel_name)

    manifest, _, _, db_bytes, pdf_bytes = create_sample_generation("gen_cand")
    db_file = tmp_path / "index.sqlite3"
    pdf_file = tmp_path / "sample.pdf"
    db_file.write_bytes(db_bytes)
    pdf_file.write_bytes(pdf_bytes)

    asyncio.run(
        transport.publish(
            generation_id="gen_cand",
            database=db_file,
            manifest=manifest.model_dump(mode="json"),
            vectors=None,
            unique_objects=[(pdf_file, "application/pdf", manifest.documents[0].pdf.sha256, len(pdf_bytes))],
        )
    )

    source_id = "source_" + "c" * 64
    hist_sha = "d" * 64
    now = "2026-10-18T00:00:00+00:00"
    canonical = canonical_unresolved_revision_ledger_v5(
        [UnresolvedRevisionIdentityV5(source_id, hist_sha, "source_metadata_unresolved")]
    )

    with WorkerState(db_path) as state:
        state.connection.execute(
            "INSERT INTO run (run_id, started_at, finished_at, status) VALUES ('run_cand', ?, ?, 'succeeded')",
            (now, now),
        )
        state.connection.execute(
            "INSERT INTO publish (generation_id, run_id, corpus_sha256, contract_sha256, serving_sha256, status, published_at, details_json) VALUES (?, ?, ?, ?, ?, 'ready', ?, '{}')",
            (
                "gen_cand",
                "run_cand",
                manifest.corpus_sha256,
                manifest.contract_sha256,
                manifest.serving_database.sha256,
                now,
            ),
        )
        state.connection.execute(
            """INSERT INTO pdf_cache_source
               (source_id, issuer, product_code, document_type, source_url, source_version,
                source_post_id, discovery_sha256, first_observed_at, last_observed_at, last_verified_at)
               VALUES (?, 'woori', 'CARD_C', 'summary', 'https://example/c.pdf', 'v1', 'post', ?, ?, ?, ?)""",
            (source_id, "c" * 64, now, now, now),
        )
        state.connection.execute(
            """INSERT INTO pdf_cache_object (pdf_sha256, size_bytes, page_count, relative_path, created_at, last_verified_at)
               VALUES (?, 100, 1, 'objects/sha256/dd/' || ?, ?, ?)""",
            (hist_sha, hist_sha, now, now),
        )
        state.connection.execute(
            """INSERT INTO pdf_cache_source_revision
               (revision_id, source_id, pdf_sha256, pdf_size_bytes, page_count, final_url, first_observed_at, last_observed_at, verified_at, superseded_at)
               VALUES (10, ?, ?, 100, 1, 'https://example/c.pdf', ?, ?, ?, ?)""",
            (source_id, hist_sha, now, now, now, now),
        )

    # Authentic registered seal on disk
    seal_dir = state_dir / "runs" / "run_cand" / "sealed"
    seal_dir.mkdir(parents=True, exist_ok=True)
    (seal_dir / "publish.json").write_text(
        json.dumps(
            {
                "generation_id": "gen_cand",
                "run_id": "run_cand",
                "database_sha256": manifest.serving_database.sha256,
                "manifest": manifest.model_dump(mode="json"),
                "v5_metrics": {
                    "historical_revision_unresolved_count": len(canonical),
                    "historical_revision_unresolved_sha256": unresolved_revision_ledger_sha256_v5(canonical),
                    "historical_revision_unresolved_identities": list(canonical),
                },
            }
        )
    )

    monkeypatch.setenv("CARDRAG_WORKER_STATE_DIR", str(state_dir))
    monkeypatch.setenv("CARDRAG_PUBLICATION_TRANSPORT", "local")
    monkeypatch.setenv("CARDRAG_SERVING_DIR", str(serving_dir))
    monkeypatch.setenv("CARDRAG_CHANNEL", channel_name)

    runner = CliRunner()

    # Apply on candidate-001 succeeds
    res_cand = runner.invoke(app, ["remediate-unresolved-history", "--apply"])
    assert res_cand.exit_code == 0
    parsed_cand = json.loads(res_cand.stdout)
    assert parsed_cand["status"] == "succeeded"
    assert parsed_cand["applied_count"] == 1

    with WorkerState(db_path) as state:
        assert state.is_revision_remediated(source_id, hist_sha) is True

    # Switching to candidate-002 (no serving generation) fails without mutating state
    monkeypatch.setenv("CARDRAG_CHANNEL", "candidate-002")
    res_cand2 = runner.invoke(app, ["remediate-unresolved-history", "--apply"])
    assert res_cand2.exit_code == 1
    parsed_cand2 = json.loads(res_cand2.stdout)
    assert parsed_cand2["status"] == "failed"
    assert "No verified current serving generation found on channel 'candidate-002'" in parsed_cand2["error"]


def test_cli_transport_pointer_missing_corrupted_or_network_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies CLI halts with exit 1 and zero state mutation when pointer is missing, corrupted, or transport throws."""
    from test_local_serving_and_backup import create_sample_generation

    from cardrag_worker.webdav import WebDAVClient, WebDAVError

    state_dir = tmp_path / "transport-fault-state"
    state_dir.mkdir(parents=True)
    db_path = state_dir / "worker-state.sqlite3"
    serving_dir = tmp_path / "serving"
    serving_dir.mkdir(parents=True)

    manifest, _, _, _, _ = create_sample_generation("gen_fault")
    source_id = "source_" + "f" * 64
    hist_sha = "9" * 64
    now = "2026-10-18T00:00:00+00:00"
    canonical = canonical_unresolved_revision_ledger_v5(
        [UnresolvedRevisionIdentityV5(source_id, hist_sha, "source_metadata_unresolved")]
    )

    with WorkerState(db_path) as state:
        state.connection.execute(
            "INSERT INTO run (run_id, started_at, finished_at, status) VALUES ('run_fault', ?, ?, 'succeeded')",
            (now, now),
        )
        state.connection.execute(
            "INSERT INTO publish (generation_id, run_id, corpus_sha256, contract_sha256, serving_sha256, status, published_at, details_json) VALUES (?, ?, ?, ?, ?, 'ready', ?, '{}')",
            (
                "gen_fault",
                "run_fault",
                manifest.corpus_sha256,
                manifest.contract_sha256,
                manifest.serving_database.sha256,
                now,
            ),
        )
        state.connection.execute(
            """INSERT INTO pdf_cache_source
               (source_id, issuer, product_code, document_type, source_url, source_version,
                source_post_id, discovery_sha256, first_observed_at, last_observed_at, last_verified_at)
               VALUES (?, 'woori', 'CARD_F', 'summary', 'https://example/f.pdf', 'v1', 'post', ?, ?, ?, ?)""",
            (source_id, "f" * 64, now, now, now),
        )
        state.connection.execute(
            """INSERT INTO pdf_cache_object (pdf_sha256, size_bytes, page_count, relative_path, created_at, last_verified_at)
               VALUES (?, 100, 1, 'objects/sha256/99/' || ?, ?, ?)""",
            (hist_sha, hist_sha, now, now),
        )
        state.connection.execute(
            """INSERT INTO pdf_cache_source_revision
               (revision_id, source_id, pdf_sha256, pdf_size_bytes, page_count, final_url, first_observed_at, last_observed_at, verified_at, superseded_at)
               VALUES (20, ?, ?, 100, 1, 'https://example/f.pdf', ?, ?, ?, ?)""",
            (source_id, hist_sha, now, now, now, now),
        )

    seal_dir = state_dir / "runs" / "run_fault" / "sealed"
    seal_dir.mkdir(parents=True, exist_ok=True)
    seal_path = seal_dir / "publish.json"
    seal_path.write_text(
        json.dumps(
            {
                "generation_id": "gen_fault",
                "run_id": "run_fault",
                "database_sha256": manifest.serving_database.sha256,
                "manifest": manifest.model_dump(mode="json"),
                "v5_metrics": {
                    "historical_revision_unresolved_count": len(canonical),
                    "historical_revision_unresolved_sha256": unresolved_revision_ledger_sha256_v5(canonical),
                    "historical_revision_unresolved_identities": list(canonical),
                },
            }
        )
    )

    monkeypatch.setenv("CARDRAG_WORKER_STATE_DIR", str(state_dir))
    monkeypatch.setenv("CARDRAG_PUBLICATION_TRANSPORT", "local")
    monkeypatch.setenv("CARDRAG_SERVING_DIR", str(serving_dir))
    monkeypatch.setenv("CARDRAG_CHANNEL", "stable")

    runner = CliRunner()

    # 1. Missing pointer: serving dir exists but current.json is missing
    res_missing = runner.invoke(app, ["remediate-unresolved-history", "--apply"])
    assert res_missing.exit_code == 1
    parsed_missing = json.loads(res_missing.stdout)
    assert parsed_missing["status"] == "failed"
    assert "No verified current serving generation found on channel 'stable'" in parsed_missing["error"]
    with WorkerState(db_path) as state:
        assert state.is_revision_remediated(source_id, hist_sha) is False

    # 2. Corrupted pointer: current.json contains malformed JSON
    (serving_dir / "current.json").write_text("{broken json corrupted!!!")
    res_corrupt = runner.invoke(app, ["remediate-unresolved-history", "--apply"])
    assert res_corrupt.exit_code == 1
    parsed_corrupt = json.loads(res_corrupt.stdout)
    assert parsed_corrupt["status"] == "failed"
    assert "No verified current serving generation found" in parsed_corrupt["error"]
    with WorkerState(db_path) as state:
        assert state.is_revision_remediated(source_id, hist_sha) is False

    # 3. WebDAV transport network failure
    monkeypatch.setenv("CARDRAG_PUBLICATION_TRANSPORT", "webdav")
    monkeypatch.setenv("CARDRAG_WEBDAV_BASE_URL", "https://webdav.example/remote.php/webdav")
    monkeypatch.setenv("CARDRAG_WEBDAV_USERNAME", "user")
    monkeypatch.setenv("CARDRAG_WEBDAV_PASSWORD", "pass")

    async def mock_webdav_fail(*_args, **_kwargs):
        raise WebDAVError("Connection reset by peer during audit")

    monkeypatch.setattr(WebDAVClient, "validated_current_generation", mock_webdav_fail)
    res_net = runner.invoke(app, ["remediate-unresolved-history", "--apply"])
    assert res_net.exit_code == 1
    parsed_net = json.loads(res_net.stdout)
    assert parsed_net["status"] == "failed"
    assert (
        "Failed querying current serving generation from transport: Connection reset by peer"
        in parsed_net["error"]
    )
    with WorkerState(db_path) as state:
        assert state.is_revision_remediated(source_id, hist_sha) is False


def test_cli_rejects_missing_manifest_or_registered_disk_seal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies CLI rejects application if authentic on-disk seal is missing or manifest is missing."""
    import asyncio

    from test_local_serving_and_backup import create_sample_generation

    from cardrag_worker.local_publisher import LocalServingTransport

    state_dir = tmp_path / "missing-seal-state"
    state_dir.mkdir(parents=True)
    db_path = state_dir / "worker-state.sqlite3"
    serving_dir = tmp_path / "serving"

    transport = LocalServingTransport(serving_dir, channel="stable")
    manifest, _, _, db_bytes, pdf_bytes = create_sample_generation("gen_missing")
    db_file = tmp_path / "index.sqlite3"
    pdf_file = tmp_path / "sample.pdf"
    db_file.write_bytes(db_bytes)
    pdf_file.write_bytes(pdf_bytes)

    asyncio.run(
        transport.publish(
            generation_id="gen_missing",
            database=db_file,
            manifest=manifest.model_dump(mode="json"),
            vectors=None,
            unique_objects=[(pdf_file, "application/pdf", manifest.documents[0].pdf.sha256, len(pdf_bytes))],
        )
    )

    source_id = "source_" + "e" * 64
    hist_sha = "8" * 64
    now = "2026-10-18T00:00:00+00:00"
    canonical = canonical_unresolved_revision_ledger_v5(
        [UnresolvedRevisionIdentityV5(source_id, hist_sha, "source_metadata_unresolved")]
    )

    with WorkerState(db_path) as state:
        state.connection.execute(
            "INSERT INTO run (run_id, started_at, finished_at, status) VALUES ('run_missing', ?, ?, 'succeeded')",
            (now, now),
        )
        state.connection.execute(
            "INSERT INTO publish (generation_id, run_id, corpus_sha256, contract_sha256, serving_sha256, status, published_at, details_json) VALUES (?, ?, ?, ?, ?, 'ready', ?, '{}')",
            (
                "gen_missing",
                "run_missing",
                manifest.corpus_sha256,
                manifest.contract_sha256,
                manifest.serving_database.sha256,
                now,
            ),
        )
        state.connection.execute(
            """INSERT INTO pdf_cache_source
               (source_id, issuer, product_code, document_type, source_url, source_version,
                source_post_id, discovery_sha256, first_observed_at, last_observed_at, last_verified_at)
               VALUES (?, 'woori', 'CARD_M', 'summary', 'https://example/m.pdf', 'v1', 'post', ?, ?, ?, ?)""",
            (source_id, "e" * 64, now, now, now),
        )
        state.connection.execute(
            """INSERT INTO pdf_cache_object (pdf_sha256, size_bytes, page_count, relative_path, created_at, last_verified_at)
               VALUES (?, 100, 1, 'objects/sha256/88/' || ?, ?, ?)""",
            (hist_sha, hist_sha, now, now),
        )
        state.connection.execute(
            """INSERT INTO pdf_cache_source_revision
               (revision_id, source_id, pdf_sha256, pdf_size_bytes, page_count, final_url, first_observed_at, last_observed_at, verified_at, superseded_at)
               VALUES (30, ?, ?, 100, 1, 'https://example/m.pdf', ?, ?, ?, ?)""",
            (source_id, hist_sha, now, now, now, now),
        )

    # External seal file pointing to run_missing, but registered on-disk runs/run_missing/sealed/publish.json is MISSING
    ext_seal = tmp_path / "external-seal.json"
    ext_seal.write_text(
        json.dumps(
            {
                "generation_id": "gen_missing",
                "run_id": "run_missing",
                "database_sha256": manifest.serving_database.sha256,
                "manifest": manifest.model_dump(mode="json"),
                "v5_metrics": {
                    "historical_revision_unresolved_count": len(canonical),
                    "historical_revision_unresolved_sha256": unresolved_revision_ledger_sha256_v5(canonical),
                    "historical_revision_unresolved_identities": list(canonical),
                },
            }
        )
    )

    monkeypatch.setenv("CARDRAG_WORKER_STATE_DIR", str(state_dir))
    monkeypatch.setenv("CARDRAG_PUBLICATION_TRANSPORT", "local")
    monkeypatch.setenv("CARDRAG_SERVING_DIR", str(serving_dir))
    monkeypatch.setenv("CARDRAG_CHANNEL", "stable")

    runner = CliRunner()

    # 1. Missing registered on-disk seal rejects apply
    res_no_reg = runner.invoke(app, ["remediate-unresolved-history", "--seal-path", str(ext_seal), "--apply"])
    assert res_no_reg.exit_code == 1
    parsed_no_reg = json.loads(res_no_reg.stdout)
    assert parsed_no_reg["status"] == "failed"
    assert "Authentic on-disk sealed publication not found" in parsed_no_reg["error"]

    with WorkerState(db_path) as state:
        assert state.is_revision_remediated(source_id, hist_sha) is False

    # 2. Registered seal on disk exists, but lacks manifest
    seal_dir = state_dir / "runs" / "run_missing" / "sealed"
    seal_dir.mkdir(parents=True, exist_ok=True)
    seal_no_manifest = {
        "generation_id": "gen_missing",
        "run_id": "run_missing",
        "database_sha256": manifest.serving_database.sha256,
        "v5_metrics": {
            "historical_revision_unresolved_count": len(canonical),
            "historical_revision_unresolved_sha256": unresolved_revision_ledger_sha256_v5(canonical),
            "historical_revision_unresolved_identities": list(canonical),
        },
    }
    (seal_dir / "publish.json").write_text(json.dumps(seal_no_manifest))
    ext_seal.write_text(json.dumps(seal_no_manifest))

    res_no_man = runner.invoke(app, ["remediate-unresolved-history", "--apply"])
    assert res_no_man.exit_code == 1
    parsed_no_man = json.loads(res_no_man.stdout)
    assert parsed_no_man["status"] == "failed"
    assert "Sealed data missing valid manifest for verified serving" in parsed_no_man["error"]

    with WorkerState(db_path) as state:
        assert state.is_revision_remediated(source_id, hist_sha) is False


@pytest.mark.asyncio
async def test_reprocess_hash_matches_canonical_normal_run_hash_and_yields_no_change(
    corpus,
) -> None:
    """Verifies that replay/reprocess with remediated ledger matches canonical normal run corpus hash and stabilizes with no_change."""
    from dataclasses import replace

    from test_pipeline_v5 import _MultiAdapter

    from cardrag_worker.partial_execution import ExecutionPlan, ReuseSource
    from cardrag_worker.pdf_cache import PDFSourceIdentity
    from cardrag_worker.webdav import RemoteGenerationIdentity

    factory, prior, state, *_rest, dav = corpus
    m = prior.manifest
    dav.current = RemoteGenerationIdentity(
        m.generation_id, m.corpus_sha256, m.contract_sha256, m.schema_version, m.serving_schema
    )

    initial = factory()
    base = initial.adapters[0].record
    records = (base,) + tuple(
        replace(
            base,
            product_code=f"P{i:03d}",
            source_post_id=f"post{i}",
            source_url=f"https://cards.example/{i}.pdf",
        )
        for i in range(1, 4)
    )
    adapter = _MultiAdapter(records)
    initial.adapters = (adapter,)
    first = await initial.run()
    assert first.status == "succeeded"

    baseline = ReuseSource(prior.root, first.run_id, state.path)
    dav.current = RemoteGenerationIdentity(
        baseline.manifest.generation_id,
        baseline.manifest.corpus_sha256,
        baseline.manifest.contract_sha256,
        baseline.manifest.schema_version,
        baseline.manifest.serving_schema,
    )

    # Inject historical unresolved entry into baseline seal
    old = replace(
        base, source_version="old", source_post_id="old-post", source_url="https://cards.example/old.pdf"
    )
    identity = PDFSourceIdentity.from_source_record(old)
    sample_doc = next(iter(baseline.documents.values()))
    now = "2026-10-19T00:00:00+00:00"
    state.connection.execute(
        """INSERT INTO pdf_cache_source
           (source_id, issuer, product_code, document_type, source_url, source_version,
            source_post_id, discovery_sha256, first_observed_at, last_observed_at, last_verified_at, superseded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            identity.source_id,
            old.issuer,
            old.product_code,
            old.document_type,
            old.source_url,
            old.source_version,
            old.source_post_id,
            identity.discovery_sha256,
            now,
            now,
            now,
            now,
        ),
    )
    state.connection.execute(
        """INSERT INTO pdf_cache_source_revision
           (source_id, pdf_sha256, pdf_size_bytes, page_count, final_url,
            first_observed_at, last_observed_at, verified_at, superseded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            identity.source_id,
            sample_doc.pdf.sha256,
            sample_doc.pdf.size_bytes,
            sample_doc.page_count,
            old.source_url,
            now,
            now,
            now,
            now,
        ),
    )
    ledger = canonical_unresolved_revision_ledger_v5(
        [
            UnresolvedRevisionIdentityV5(
                identity.source_id, sample_doc.pdf.sha256, "source_metadata_unresolved"
            )
        ]
    )
    seal_path = baseline.directory / "sealed" / "publish.json"
    data = json.loads(seal_path.read_text())
    data["v5_metrics"].update(
        historical_revision_unresolved_count=1,
        historical_revision_unresolved_identities=list(ledger),
        historical_revision_unresolved_sha256=unresolved_revision_ledger_sha256_v5(ledger),
    )
    seal_path.write_text(json.dumps(data))

    tampered = ReuseSource(prior.root, first.run_id, state.path)
    replay = factory(ExecutionPlan(frozenset({"pdf"}), first.run_id), tampered)
    replay.adapters = (adapter,)
    result = await replay.run()
    assert result.status == "succeeded"
    assert state.is_revision_remediated(identity.source_id, sample_doc.pdf.sha256) is True

    published = ReuseSource(prior.root, result.run_id, state.path)
    assert published.seal["v5_metrics"]["historical_revision_unresolved_count"] == 0

    # The replayed corpus SHA matches the normal run baseline corpus SHA exactly
    assert published.manifest.corpus_sha256 == baseline.manifest.corpus_sha256

    # Subsequent normal run with unchanged inputs stabilizes at no_change with identical corpus SHA
    dav.current = RemoteGenerationIdentity(
        published.manifest.generation_id,
        published.manifest.corpus_sha256,
        published.manifest.contract_sha256,
        published.manifest.schema_version,
        published.manifest.serving_schema,
    )
    following = factory()
    following.adapters = (adapter,)
    again = await following.run()
    assert again.status == "no_change"
    assert again.corpus_sha256 == baseline.manifest.corpus_sha256
