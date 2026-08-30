from __future__ import annotations

import hashlib
import http.client
import os
import re
import threading
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from cardrag_core import canonical_json_bytes
from v5_fixtures import build_v5_fixture

import cardrag_mcp.gold_review as review
from cardrag_mcp.evaluation import (
    REQUIRED_RELEASE_SLICES,
    GoldQuery,
    load_blind_evaluation_jsonl,
    load_gold_jsonl,
)
from cardrag_mcp.gold_review import (
    BlindReviewDecision,
    BlindReviewState,
    DraftEvidence,
    DraftManifest,
    GoldDraftRecord,
    GoldReviewDecision,
    GoldReviewError,
    GoldReviewState,
)

SOURCE_COMMIT = "1" * 40
BASELINE_COMMIT = "fee8f65a9fda7ae0c286ac92cf4c3f55c1a6f113"


def _write_models(path: Path, models: list[Any] | tuple[Any, ...]) -> None:
    path.write_bytes(b"".join(canonical_json_bytes(item) + b"\n" for item in models))


def _inventory() -> tuple[review._InventorySpan, ...]:
    rows: list[review._InventorySpan] = []
    for issuer in review.ISSUERS:
        for index in range(100):
            contract_number = index // 2
            lineage_number = index // 4
            text = (
                f"혜택 적립 할인 캐시백 전월 실적 10,000원 한도 월 2회 최소 결제 "
                f"연회비 해외 수수료 발급 제외 예외 유예 유의사항 개정 {issuer} {index}"
            )
            node_type = (
                "TABLE_ROW" if index % 10 == 0 else "FOOTNOTE" if index % 10 == 1 else "PARAGRAPH"
            )
            rows.append(
                review._InventorySpan(
                    span_id=f"node-{issuer}-{index:03d}",
                    contract_revision_id=f"revision-{issuer}-{contract_number:03d}",
                    issuer=issuer,
                    product_name=f"{issuer.upper()} 카드 {contract_number:03d}",
                    product_lineage_id=f"lineage-{issuer}-{lineage_number:03d}",
                    temporal_status="current" if index % 4 < 2 else "superseded",
                    effective_date=f"202{index % 6}-01-01",
                    node_type=node_type,
                    major_class="NOTICE" if index % 7 == 0 else "BENEFIT",
                    heading=f"혜택 조건 {index}",
                    page=(index % 2) + 1,
                    source_start=0,
                    source_end=len(text),
                    text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    text=text,
                )
            )
    return tuple(rows)


def test_real_v5_inventory_binds_exact_page_spans(tmp_path: Path) -> None:
    fixture = build_v5_fixture(tmp_path / "v5")

    generation_id, corpus_sha256, database_sha256, size_bytes, inventory_sha256, rows = (
        review._inventory_rows(fixture.database)
    )

    assert generation_id == fixture.generation_id
    assert re.fullmatch(r"[0-9a-f]{64}", corpus_sha256)
    assert re.fullmatch(r"[0-9a-f]{64}", database_sha256)
    assert re.fullmatch(r"[0-9a-f]{64}", inventory_sha256)
    assert size_bytes == fixture.database.stat().st_size
    assert rows
    for row in rows:
        assert hashlib.sha256(row.text.encode()).hexdigest() == row.text_sha256
        assert row.source_end > row.source_start


def test_inventory_sqlite_open_is_inode_pinned_during_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authentic = build_v5_fixture(
        tmp_path / "authentic",
        generation_id="gen-v5-authentic",
    )
    substitute = build_v5_fixture(
        tmp_path / "substitute",
        generation_id="gen-v5-substitute",
    )
    authentic_sha256 = hashlib.sha256(authentic.database.read_bytes()).hexdigest()
    authentic_directory = authentic.database.parent
    substitute_directory = substitute.database.parent
    displaced_directory = tmp_path / "authentic-displaced"
    real_connect = review.sqlite3.connect
    swaps = 0

    def connect_during_swap(database_uri: str, *args: Any, **kwargs: Any) -> Any:
        nonlocal swaps
        swaps += 1
        authentic_directory.replace(displaced_directory)
        substitute_directory.replace(authentic_directory)
        try:
            connection = real_connect(database_uri, *args, **kwargs)
        finally:
            authentic_directory.replace(substitute_directory)
            displaced_directory.replace(authentic_directory)
        return connection

    monkeypatch.setattr(review.sqlite3, "connect", connect_during_swap)
    generation_id, _corpus, database_sha256, _size, _inventory_sha256, rows = (
        review._inventory_rows(authentic.database)
    )

    assert swaps == 1
    assert generation_id == authentic.generation_id
    assert generation_id != substitute.generation_id
    assert database_sha256 == authentic_sha256
    assert rows


def test_inventory_rejects_same_byte_atomic_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = build_v5_fixture(tmp_path / "v5")
    replacement = tmp_path / "same-bytes.sqlite3"
    replacement.write_bytes(fixture.database.read_bytes())
    real_connect = review.sqlite3.connect

    def connect_then_replace(database_uri: str, *args: Any, **kwargs: Any) -> Any:
        connection = real_connect(database_uri, *args, **kwargs)
        replacement.replace(fixture.database)
        return connection

    monkeypatch.setattr(review.sqlite3, "connect", connect_then_replace)
    with pytest.raises(GoldReviewError, match="input_changed_during_read"):
        review._inventory_rows(fixture.database)


def test_inventory_rejects_symlink_and_nonregular_database(tmp_path: Path) -> None:
    fixture = build_v5_fixture(tmp_path / "v5")
    symlink = tmp_path / "database-link.sqlite3"
    symlink.symlink_to(fixture.database)
    with pytest.raises(GoldReviewError, match="input_not_regular"):
        review._inventory_rows(symlink)

    directory = tmp_path / "database-directory"
    directory.mkdir()
    with pytest.raises(GoldReviewError, match="input_not_regular"):
        review._inventory_rows(directory)


@pytest.mark.skipif(not hasattr(os, "O_NONBLOCK"), reason="requires POSIX nonblocking open")
@pytest.mark.parametrize(
    ("reader_kind", "expected_error"),
    (
        ("database", "input_size_invalid"),
        ("review", "input_not_regular"),
    ),
)
def test_review_readers_nonblocking_open_rejects_fifo_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader_kind: str,
    expected_error: str,
) -> None:
    artifact = tmp_path / "review-input"
    artifact.write_bytes(b"sealed-review-input")
    real_open = review.os.open
    opened_descriptors: list[int] = []
    raced = False
    observed_flags = 0

    def racing_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal observed_flags, raced
        if not raced and dir_fd is None and Path(os.fspath(path)) == artifact:
            raced = True
            observed_flags = flags
            artifact.unlink()
            os.mkfifo(artifact)
            assert flags & os.O_NONBLOCK
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        opened_descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(review.os, "open", racing_open)

    with pytest.raises(GoldReviewError, match=expected_error):
        if reader_kind == "database":
            with review._pinned_sqlite_database(artifact):
                raise AssertionError("FIFO must be rejected during context entry")
        else:
            review._read_regular(artifact)

    assert raced
    assert observed_flags & os.O_NONBLOCK
    assert len(opened_descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(opened_descriptors[0])


@pytest.mark.parametrize(
    ("maximum", "expected_error"),
    ((4, "input_size_invalid"), (8, "input_changed_during_read")),
)
def test_regular_reader_rejects_same_inode_growth_before_open_without_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    maximum: int,
    expected_error: str,
) -> None:
    artifact = tmp_path / "review-input.json"
    artifact.write_bytes(b"seal")
    inode = artifact.stat().st_ino
    real_open = review.os.open
    mutated = False

    def open_after_growth(path: Path, flags: int) -> int:
        nonlocal mutated
        if not mutated:
            artifact.write_bytes(b"seal!")
            mutated = True
        return real_open(path, flags)

    monkeypatch.setattr(review.os, "open", open_after_growth)
    monkeypatch.setattr(
        review.os,
        "read",
        lambda *_args: pytest.fail("reader consumed a file that changed before open"),
    )

    with pytest.raises(GoldReviewError, match=expected_error):
        review._read_regular(artifact, maximum=maximum)

    assert mutated
    assert artifact.stat().st_ino == inode


def test_regular_reader_rejects_path_replacement_after_bounded_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "review-input.json"
    replacement = tmp_path / "replacement.json"
    artifact.write_bytes(b"sealed-review-input")
    replacement.write_bytes(artifact.read_bytes())
    real_fstat = review.os.fstat
    calls = 0

    def fstat_then_replace(descriptor: int) -> Any:
        nonlocal calls
        result = real_fstat(descriptor)
        calls += 1
        if calls == 2:
            replacement.replace(artifact)
        return result

    monkeypatch.setattr(review.os, "fstat", fstat_then_replace)

    with pytest.raises(GoldReviewError, match="input_changed_during_read"):
        review._read_regular(artifact)

    assert calls == 2


def test_regular_reader_accepts_stable_hardlink(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    hardlink = tmp_path / "hardlink.json"
    payload = b"sealed-review-input"
    source.write_bytes(payload)
    hardlink.hardlink_to(source)

    assert hardlink.stat().st_nlink == 2
    assert review._read_regular(hardlink) == payload


def test_sampler_is_deterministic_stratified_and_performance_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _inventory()
    inventory_result = (
        "generation-test",
        "b" * 64,
        "c" * 64,
        4096,
        "d" * 64,
        inventory,
    )
    monkeypatch.setattr(review, "_inventory_rows", lambda _path: inventory_result)

    first = review.build_draft(Path("unused.sqlite3"), count=300, no_answer_count=24, seed=1010)
    again = review.build_draft(Path("unused.sqlite3"), count=300, no_answer_count=24, seed=1010)
    changed = review.build_draft(Path("unused.sqlite3"), count=300, no_answer_count=24, seed=1011)

    assert canonical_json_bytes(first) == canonical_json_bytes(again)
    assert canonical_json_bytes(first) != canonical_json_bytes(changed)
    manifest, records = first
    assert manifest.query_count == 300
    assert manifest.no_answer_count == 24
    assert manifest.issuer_counts == {issuer: 75 for issuer in review.ISSUERS}
    assert manifest.candidate_performance_selection is False
    assert manifest.provider_calls is False
    assert not hasattr(manifest, "score")
    assert REQUIRED_RELEASE_SLICES.issubset(
        {slice_name for record in records for slice_name in record.proposed_gold.slices}
    )
    assert sum(record.proposed_gold.no_answer for record in records) == 24
    assert any(record.proposed_gold.condition_groups for record in records)
    assert any(record.proposed_gold.expected_revision_ids for record in records)
    assert any(
        record.proposed_gold.high_risk and record.proposed_gold.expected_numeric_facts
        for record in records
    )
    for record in records:
        assert record.selection_basis == "corpus_inventory_only"
        allowed = {item.span_id: item for item in record.evidence}
        for span in record.proposed_gold.spans:
            source = allowed[span.span_id]
            assert (
                span.contract_revision_id,
                span.page,
                span.source_start,
                span.source_end,
                span.text_sha256,
            ) == (
                source.contract_revision_id,
                source.page,
                source.source_start,
                source.source_end,
                source.text_sha256,
            )
            assert hashlib.sha256(source.text.encode()).hexdigest() == source.text_sha256


def _source(
    *,
    issuer: str,
    query_index: int,
    span_suffix: str = "a",
    text: str = "혜택 월 10,000원",
    page: int = 1,
) -> DraftEvidence:
    return DraftEvidence.model_validate(
        {
            "span_id": f"span-{query_index:03d}-{span_suffix}",
            "contract_revision_id": f"contract-{query_index:03d}",
            "issuer": issuer,
            "product_name": f"검증 카드 {query_index:03d}",
            "product_lineage_id": f"lineage-{query_index:03d}",
            "temporal_status": "current",
            "effective_date": "2026-01-01",
            "node_type": "PARAGRAPH",
            "major_class": "BENEFIT" if span_suffix == "a" else "NOTICE",
            "heading": "혜택 조건",
            "page": page,
            "source_start": 0,
            "source_end": len(text),
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "text": text,
        }
    )


def _release_draft(tmp_path: Path) -> tuple[Path, Path, Path]:
    records: list[GoldDraftRecord] = []
    decisions: list[GoldReviewDecision] = []
    no_answer_count = 0
    for index in range(300):
        issuer = review.ISSUERS[index // 75]
        query_id = f"gold-{index + 1:03d}"
        no_answer = index % 75 >= 69
        if no_answer:
            no_answer_count += 1
            gold_payload: dict[str, Any] = {
                "schema_version": "cardrag.gold-query.v1",
                "query_id": query_id,
                "question": f"자료에 없는 가상 상품 {index + 1}의 보험 혜택은?",
                "slices": sorted((f"issuer:{issuer}", "hard_negative", "no_answer")),
                "contracts": [],
                "spans": [],
                "condition_groups": [],
                "expected_numeric_facts": [],
                "expected_revision_ids": [],
                "no_answer": True,
                "high_risk": False,
            }
            evidence: tuple[DraftEvidence, ...] = ()
        elif index == 0:
            first = _source(issuer=issuer, query_index=index + 1)
            second = _source(
                issuer=issuer,
                query_index=index + 1,
                span_suffix="b",
                text="전월 실적과 현재 개정 조건",
                page=2,
            )
            slices = {
                item
                for item in REQUIRED_RELEASE_SLICES
                if item != "no_answer" and not item.startswith("issuer:")
            }
            slices.add("issuer:kb")
            gold_payload = {
                "schema_version": "cardrag.gold-query.v1",
                "query_id": query_id,
                "question": "검증 카드의 혜택, 월 한도, 적용 조건과 현재 개정 기준은?",
                "slices": sorted(slices),
                "contracts": [{"contract_revision_id": first.contract_revision_id, "relevance": 3}],
                "spans": [
                    {
                        "span_id": first.span_id,
                        "contract_revision_id": first.contract_revision_id,
                        "page": first.page,
                        "source_start": first.source_start,
                        "source_end": first.source_end,
                        "text_sha256": first.text_sha256,
                        "relevance": 3,
                        "roles": ["benefit", "numeric"],
                    },
                    {
                        "span_id": second.span_id,
                        "contract_revision_id": second.contract_revision_id,
                        "page": second.page,
                        "source_start": second.source_start,
                        "source_end": second.source_end,
                        "text_sha256": second.text_sha256,
                        "relevance": 3,
                        "roles": ["condition", "revision"],
                    },
                ],
                "condition_groups": [
                    {"at_k": 10, "span_ids": sorted((first.span_id, second.span_id))}
                ],
                "expected_numeric_facts": ["10,000원"],
                "expected_revision_ids": [first.contract_revision_id],
                "no_answer": False,
                "high_risk": True,
            }
            evidence = (first, second)
        else:
            first = _source(issuer=issuer, query_index=index + 1)
            gold_payload = {
                "schema_version": "cardrag.gold-query.v1",
                "query_id": query_id,
                "question": f"검증 카드 {index + 1}의 혜택은?",
                "slices": sorted(("benefit", f"issuer:{issuer}", "major:benefit")),
                "contracts": [{"contract_revision_id": first.contract_revision_id, "relevance": 3}],
                "spans": [
                    {
                        "span_id": first.span_id,
                        "contract_revision_id": first.contract_revision_id,
                        "page": first.page,
                        "source_start": first.source_start,
                        "source_end": first.source_end,
                        "text_sha256": first.text_sha256,
                        "relevance": 3,
                        "roles": ["benefit", "numeric"],
                    }
                ],
                "condition_groups": [],
                "expected_numeric_facts": ["10,000원"],
                "expected_revision_ids": [],
                "no_answer": False,
                "high_risk": True,
            }
            evidence = (first,)
        gold = GoldQuery.model_validate(gold_payload)
        record = GoldDraftRecord(
            schema_version="cardrag.gold-draft-query.v1",
            ordinal=index + 1,
            query_id=query_id,
            issuer=issuer,  # type: ignore[arg-type]
            primary_slice="no_answer" if no_answer else "benefit",
            selection_basis="corpus_inventory_only",
            proposed_gold=gold,
            evidence=evidence,
        )
        records.append(record)
        decisions.append(GoldReviewDecision(query_id=query_id, status="approved", annotation=gold))

    manifest = DraftManifest(
        schema_version="cardrag.gold-draft-artifact.v1",
        algorithm="corpus-inventory-stratified.v1",
        generation_id="generation-release-test",
        corpus_sha256="b" * 64,
        serving_database_sha256="c" * 64,
        serving_database_size_bytes=4096,
        inventory_sha256="d" * 64,
        seed=1010,
        query_count=300,
        no_answer_count=no_answer_count,
        issuer_counts={issuer: 75 for issuer in review.ISSUERS},
        required_slices=tuple(sorted(REQUIRED_RELEASE_SLICES)),
        candidate_performance_selection=False,
        provider_calls=False,
    )
    draft_path = tmp_path / "gold-draft.jsonl"
    _write_models(draft_path, [manifest, *records])
    draft = review._load_draft(draft_path)
    state = GoldReviewState(
        schema_version="cardrag.gold-review-state.v1",
        draft_sha256=draft.sha256,
        query_count=300,
        decisions=tuple(decisions),
    )
    state_path = tmp_path / "gold-review-state.json"
    state_path.write_bytes(canonical_json_bytes(state) + b"\n")
    return draft_path, state_path, tmp_path / "gold.jsonl"


def test_gold_seal_requires_all_approved_and_calls_release_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft_path, state_path, output = _release_draft(tmp_path)
    original_loader = load_gold_jsonl
    release_flags: list[bool] = []

    def spy(path: Path, *, release_gate: bool = False) -> Any:
        release_flags.append(release_gate)
        return original_loader(path, release_gate=release_gate)

    monkeypatch.setattr(review, "load_gold_jsonl", spy)
    digest = review.seal_gold(draft_path, state_path, output)

    assert release_flags == [True]
    assert digest == hashlib.sha256(output.read_bytes()).hexdigest()
    sealed = original_loader(output, release_gate=True)
    assert len(sealed.queries) == 300
    assert REQUIRED_RELEASE_SLICES.issubset(
        {slice_name for query in sealed.queries for slice_name in query.slices}
    )

    state = review._load_gold_state(state_path, review._load_draft(draft_path))
    incomplete = state.model_copy(
        update={
            "decisions": (
                state.decisions[0].model_copy(update={"status": "pending"}),
                *state.decisions[1:],
            )
        }
    )
    state_path.write_bytes(canonical_json_bytes(incomplete) + b"\n")
    with pytest.raises(GoldReviewError, match="gold_review_incomplete"):
        review.seal_gold(draft_path, state_path, tmp_path / "must-not-exist.jsonl")


def _run_manifest(lane: str, gold_sha256: str, query_count: int) -> dict[str, Any]:
    if lane == "v109_baseline":
        return {
            "schema_version": "cardrag.gold-run-artifact.v1",
            "lane": lane,
            "profile_id": "cardrag.eval.v109-small-rrf.v1",
            "gold_sha256": gold_sha256,
            "query_count": query_count,
            "source_version": "v1.0.9",
            "source_commit": BASELINE_COMMIT,
            "generation_id": "generation-v109",
            "generation_manifest_sha256": "e" * 64,
            "serving_schema": "cardrag.serving-db.v4",
            "embedding_model": "openai/text-embedding-3-small",
            "embedding_dimension": 1536,
            "retrieval_policy": "small_rrf",
            "rrf_k": 60,
            "shadow_only": False,
            "primary_lane": None,
            "shadow_model": None,
        }
    return {
        "schema_version": "cardrag.gold-run-artifact.v1",
        "lane": lane,
        "profile_id": "cardrag.eval.qwen-structure-exact.v1",
        "gold_sha256": gold_sha256,
        "query_count": query_count,
        "source_version": "v1.0.10-candidate",
        "source_commit": SOURCE_COMMIT,
        "generation_id": "generation-v110",
        "generation_manifest_sha256": "f" * 64,
        "serving_schema": "cardrag.serving-db.v5",
        "embedding_model": "qwen/qwen3-embedding-8b",
        "embedding_dimension": 4096,
        "retrieval_policy": "qwen_structure_exact",
        "rrf_k": None,
        "shadow_only": False,
        "primary_lane": None,
        "shadow_model": None,
    }


def _write_runs(gold_path: Path, directory: Path) -> tuple[Path, Path]:
    gold = load_gold_jsonl(gold_path, release_gate=True)
    baseline_results: list[dict[str, Any]] = []
    candidate_results: list[dict[str, Any]] = []
    for index, query in enumerate(gold.queries, start=1):
        contract = {
            "contract_revision_id": f"observed-contract-{index:03d}",
            "rank": 1,
            "score": 0.5,
        }
        span = {
            "span_id": f"observed-span-{index:03d}",
            "contract_revision_id": contract["contract_revision_id"],
            "rank": 1,
            "score": 0.5,
        }
        baseline_results.append(
            {
                "schema_version": "cardrag.gold-run-result.v1",
                "query_id": query.query_id,
                "lane": "v109_baseline",
                "contracts": [contract],
                "spans": [span],
                "answer": {
                    "text": f"기준 답변 {index:03d}",
                    "no_answer": False,
                    "citation_span_ids": [],
                    "numeric_facts": [],
                    "selected_revision_ids": [],
                },
                "v109_baseline": {
                    "kind": "v109_small_rrf",
                    "rrf_k": 60,
                    "dense_contracts": [contract],
                    "dense_spans": [span],
                },
                "shadow": None,
            }
        )
        candidate_results.append(
            {
                "schema_version": "cardrag.gold-run-result.v1",
                "query_id": query.query_id,
                "lane": "qwen_structure_exact",
                "contracts": [contract],
                "spans": [span],
                "answer": {
                    "text": f"개선 답변 {index:03d}",
                    "no_answer": False,
                    "citation_span_ids": [],
                    "numeric_facts": [],
                    "selected_revision_ids": [],
                },
                "v109_baseline": None,
                "shadow": None,
            }
        )
    baseline_path = directory / "baseline.jsonl"
    candidate_path = directory / "exact.jsonl"
    baseline_records = [_run_manifest("v109_baseline", gold.sha256, 300), *baseline_results]
    candidate_records = [
        _run_manifest("qwen_structure_exact", gold.sha256, 300),
        *candidate_results,
    ]
    baseline_path.write_bytes(
        b"".join(canonical_json_bytes(item) + b"\n" for item in baseline_records)
    )
    candidate_path.write_bytes(
        b"".join(canonical_json_bytes(item) + b"\n" for item in candidate_records)
    )
    return baseline_path, candidate_path


@pytest.fixture
def blind_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    draft_path, state_path, gold_path = _release_draft(tmp_path)
    review.seal_gold(draft_path, state_path, gold_path)
    baseline, candidate = _write_runs(gold_path, tmp_path)
    return gold_path, baseline, candidate, tmp_path / "packet.jsonl", tmp_path / "blind-state.json"


def _complete_blind_state(packet: review.BlindPacketDataset) -> BlindReviewState:
    return BlindReviewState(
        schema_version="cardrag.blind-review-state.v1",
        packet_sha256=packet.sha256,
        pair_count=len(packet.pairs),
        decisions=tuple(
            BlindReviewDecision(
                pair_id=pair.pair_id,
                query_id=pair.query_id,
                rater_key=pair.rater_key,
                naturalness_preference="tie",
                factual_completeness_preference="tie",
            )
            for pair in packet.pairs
        ),
    )


def test_blind_packet_is_hidden_balanced_hash_bound_and_resumable(
    tmp_path: Path,
    blind_inputs: tuple[Path, Path, Path, Path, Path],
) -> None:
    gold, baseline, candidate, packet_path, state_path = blind_inputs
    packet = review.prepare_blind(
        gold,
        baseline,
        candidate,
        packet_path,
        state_path,
        rater_keys=("anonymous-rater-01",),
        seed=1010,
    )

    packet_bytes = packet_path.read_bytes()
    state_bytes = state_path.read_bytes()
    for forbidden in (
        b'"candidate_position"',
        b'"candidate_lane"',
        b'"baseline_lane"',
        b"qwen_structure_exact",
        b"v109_baseline",
    ):
        assert forbidden not in packet_bytes
        assert forbidden not in state_bytes
    assert (
        review._load_or_create_blind_state(state_path, packet).decisions
        == review._load_blind_state(state_path, packet).decisions
    )

    complete = _complete_blind_state(packet)
    state_path.write_bytes(canonical_json_bytes(complete) + b"\n")
    output = tmp_path / "blind.jsonl"
    digest = review.seal_blind(gold, baseline, candidate, packet_path, state_path, output)
    sealed = load_blind_evaluation_jsonl(output)
    assert digest == sealed.sha256
    positions = Counter(item.candidate_position for item in sealed.ratings)
    assert positions == {"left": 150, "right": 150}
    assert sealed.manifest.ratings_per_query == 1
    assert sealed.manifest.pair_count == 300

    tampered_models: list[Any] = [packet.manifest, *packet.pairs]
    first = packet.pairs[0].model_copy(
        update={
            "left_answer": "조작된 답변",
            "left_answer_sha256": hashlib.sha256("조작된 답변".encode()).hexdigest(),
        }
    )
    tampered_models[1] = first
    tampered_path = tmp_path / "tampered-packet.jsonl"
    _write_models(tampered_path, tampered_models)
    tampered = review._load_blind_packet(tampered_path)
    tampered_state_path = tmp_path / "tampered-state.json"
    tampered_state_path.write_bytes(canonical_json_bytes(_complete_blind_state(tampered)) + b"\n")
    with pytest.raises(GoldReviewError, match="blind_packet_answer_binding_mismatch"):
        review.seal_blind(
            gold,
            baseline,
            candidate,
            tampered_path,
            tampered_state_path,
            tmp_path / "tampered-output.jsonl",
        )

    question_models: list[Any] = [packet.manifest, *packet.pairs]
    question_models[1] = packet.pairs[0].model_copy(update={"question": "편향된 질문"})
    question_path = tmp_path / "question-tampered-packet.jsonl"
    _write_models(question_path, question_models)
    question_packet = review._load_blind_packet(question_path)
    question_state_path = tmp_path / "question-tampered-state.json"
    question_state_path.write_bytes(
        canonical_json_bytes(_complete_blind_state(question_packet)) + b"\n"
    )
    with pytest.raises(GoldReviewError, match="blind_packet_question_binding_mismatch"):
        review.seal_blind(
            gold,
            baseline,
            candidate,
            question_path,
            question_state_path,
            tmp_path / "question-tampered-output.jsonl",
        )


class _HTTPController(review._ReviewController):
    def __init__(self) -> None:
        self.applied: list[Mapping[str, Any]] = []

    def public_payload(self) -> dict[str, Any]:
        return {"items": [], "mode": "blind", "rubric": "local-only"}

    def apply(self, payload: Mapping[str, Any]) -> None:
        self.applied.append(payload)


def test_review_http_enforces_loopback_origin_csrf_and_security_headers() -> None:
    controller = _HTTPController()
    server = review.create_review_server(controller, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    host = f"127.0.0.1:{port}"
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/", headers={"Host": "attacker.invalid"})
        assert connection.getresponse().status == 403
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/", headers={"Host": host})
        response = connection.getresponse()
        body = response.read().decode()
        assert response.status == 200
        assert response.getheader("Cache-Control") == "no-store, max-age=0"
        csp = response.getheader("Content-Security-Policy") or ""
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "connect-src 'self'" in csp
        token_match = re.search(r'const csrf="([^"]+)"', body)
        assert token_match is not None
        token = token_match.group(1)
        connection.close()

        payload = b'{"safe":true}'
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "POST",
            "/api/state",
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Host": host,
                "Origin": f"http://{host}",
            },
        )
        assert connection.getresponse().status == 403
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.putrequest("POST", "/api/state", skip_host=True)
        connection.putheader("Host", host)
        connection.putheader("Origin", f"http://{host}")
        connection.putheader("X-CSRF-Token", token)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(review.MAX_REVIEW_BODY_BYTES + 1))
        connection.endheaders()
        assert connection.getresponse().status == 413
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "POST",
            "/api/state",
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Host": host,
                "Origin": "http://attacker.invalid",
                "X-CSRF-Token": token,
            },
        )
        assert connection.getresponse().status == 403
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "POST",
            "/api/state",
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Host": host,
                "Origin": f"http://{host}",
                "X-CSRF-Token": token,
            },
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("X-Frame-Options") == "DENY"
        response.read()
        connection.close()
        assert controller.applied == [{"safe": True}]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
