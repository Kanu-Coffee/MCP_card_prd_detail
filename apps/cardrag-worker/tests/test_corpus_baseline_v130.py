"""Rolling corpus baseline (FIX_03 A) tests: sealing, verification, fallback, prune."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from cardrag_worker.corpus_baseline import (
    CorpusBaselineDocument,
    CorpusBaselineError,
    load_corpus_baseline,
    prune_corpus_baselines,
    record_corpus_baseline,
)
from cardrag_worker.state import WorkerState


def _documents(offset: int = 0, count: int = 3) -> tuple[CorpusBaselineDocument, ...]:
    return tuple(
        CorpusBaselineDocument(
            document_id=f"doc_{offset + i:064x}",
            source_id=f"source_{offset + i:064x}",
            issuer="kb" if (offset + i) % 2 == 0 else "lotte",
            product_code=f"P{offset + i}",
            document_type="product-manual",
            temporal_status="current" if (offset + i) % 4 else "historical",
            pdf_sha256=f"{offset + i:064x}",
        )
        for i in range(count)
    )


def _bind(state: WorkerState, *, run_id: str, generation_id: str, corpus: str, contract: str) -> None:
    state.connection.execute(
        "INSERT INTO run(run_id, started_at, finished_at, status, corpus_sha256, contract_sha256) "
        "VALUES(?, ?, ?, 'succeeded', ?, ?)",
        (run_id, "2026-09-30T00:00:00+00:00", "2026-09-30T02:00:00+00:00", corpus, contract),
    )
    state.record_publish(
        generation_id=generation_id,
        run_id=run_id,
        corpus_sha256=corpus,
        contract_sha256=contract,
        serving_sha256="a" * 64,
        status="ready",
        details={"manifest_sha256": "b" * 64},
    )


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Path, WorkerState]:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state = WorkerState(state_dir / "worker-state.sqlite3")
    return state_dir, state


def test_record_and_load_roundtrip(env: tuple[Path, WorkerState]) -> None:
    state_dir, state = env
    documents = _documents()
    path = record_corpus_baseline(
        state_dir,
        generation_id="g-run-a" + "-c" * 52,
        run_id="run-a",
        corpus_sha256="c" * 64,
        contract_sha256="d" * 64,
        documents=documents,
        issuer_counts={"kb": 2, "lotte": 1},
    )
    _bind(state, run_id="run-a", generation_id="g-run-a" + "-c" * 52, corpus="c" * 64, contract="d" * 64)
    assert path.name == f"{path.stem}.json"
    mode = path.lstat().st_mode
    assert stat.S_IMODE(mode) == 0o600
    baseline = load_corpus_baseline(state_dir, state)
    assert baseline is not None
    assert baseline.baseline_sha256 == path.stem
    assert len(baseline.documents) == 3
    assert baseline.current_doc_ids ^ baseline.historical_doc_ids == {d.document_id for d in documents}


def test_re_record_is_idempotent(env: tuple[Path, WorkerState]) -> None:
    state_dir, state = env
    kwargs = {
        "generation_id": "g-x",
        "run_id": "run-x",
        "corpus_sha256": "e" * 64,
        "contract_sha256": "f" * 64,
        "documents": _documents(),
        "issuer_counts": {"kb": 2, "lotte": 1},
    }
    first = record_corpus_baseline(state_dir, **kwargs)
    second = record_corpus_baseline(state_dir, **kwargs)
    assert first == second


def test_tampered_baseline_fails_closed(env: tuple[Path, WorkerState]) -> None:
    state_dir, state = env
    path = record_corpus_baseline(
        state_dir,
        generation_id="g-run-a",
        run_id="run-a",
        corpus_sha256="c" * 64,
        contract_sha256="d" * 64,
        documents=_documents(),
        issuer_counts={"kb": 2, "lotte": 1},
    )
    _bind(state, run_id="run-a", generation_id="g-run-a", corpus="c" * 64, contract="d" * 64)
    payload = json.loads(path.read_text())
    payload["counts"]["total"] = 99
    path.write_text(json.dumps(payload))
    with pytest.raises(CorpusBaselineError) as raised:
        load_corpus_baseline(state_dir, state)
    assert raised.value.code == "corpus_baseline_invalid"


def test_unbound_run_or_publish_is_rejected(env: tuple[Path, WorkerState]) -> None:
    state_dir, state = env
    record_corpus_baseline(
        state_dir,
        generation_id="g-orphan",
        run_id="run-orphan",
        corpus_sha256="1" * 64,
        contract_sha256="2" * 64,
        documents=_documents(),
        issuer_counts={"kb": 2, "lotte": 1},
    )
    with pytest.raises(CorpusBaselineError) as raised:
        load_corpus_baseline(state_dir, state)
    assert raised.value.code == "corpus_baseline_unbound"


def test_empty_directory_is_none(tmp_path: Path, env: tuple[Path, WorkerState]) -> None:
    state_dir, state = env
    assert load_corpus_baseline(state_dir, state) is None
    (state_dir / "audit-reports" / "corpus-baseline").mkdir(parents=True)
    assert load_corpus_baseline(state_dir, state) is None


def test_prune_keeps_newest_never_losing_latest(env: tuple[Path, WorkerState]) -> None:
    state_dir, state = env
    paths = []
    for index in range(4):
        path = record_corpus_baseline(
            state_dir,
            generation_id=f"g-{index}",
            run_id=f"run-{index}",
            corpus_sha256=f"{index + 10:064x}",
            contract_sha256=f"{index + 20:064x}",
            documents=_documents(offset=index),
            issuer_counts={"kb": 2, "lotte": 1},
        )
        os.utime(path, (1_000 + index, 1_000 + index))
        paths.append(path)
    assert prune_corpus_baselines(state_dir, keep=3) == 1
    assert not paths[0].exists()
    assert paths[3].exists()
    assert prune_corpus_baselines(state_dir, keep=3) == 0
    with pytest.raises(CorpusBaselineError):
        prune_corpus_baselines(state_dir, keep=0)


def test_duplicate_documents_rejected(env: tuple[Path, WorkerState]) -> None:
    state_dir, state = env
    documents = _documents()
    with pytest.raises(CorpusBaselineError) as raised:
        record_corpus_baseline(
            state_dir,
            generation_id="g-dup",
            run_id="run-dup",
            corpus_sha256="a" * 64,
            contract_sha256="b" * 64,
            documents=documents + (documents[0],),
            issuer_counts={},
        )
    assert raised.value.code == "baseline_duplicate_document"
