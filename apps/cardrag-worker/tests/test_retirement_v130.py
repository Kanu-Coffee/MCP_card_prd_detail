"""Retirement policy engine + durable ledger (FIX_03 B/C) tests, incl. the
2026-09-30 lotte eight-lineage regression in two consecutive runs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cardrag_worker.retirement import (
    AbsentDocument,
    RetirementError,
    RetirementLedger,
    RetirementPolicy,
    evaluate_retirements,
    ledger_bytes,
    load_retirement_ledger,
    prune_retirement_ledgers,
    write_retirement_ledger,
)

LOTTE_CODES = ("1151", "1835", "1836", "1862", "1863", "1864", "1865", "1885")


def _policy(**overrides: object) -> RetirementPolicy:
    values: dict[str, object] = {
        "grace_runs": 2,
        "grace_days": 3,
        "max_per_run": 25,
        "max_ratio": 0.005,
    }
    values.update(overrides)
    return RetirementPolicy(**values)  # type: ignore[arg-type]


def _absent(code: str, issuer: str = "lotte") -> AbsentDocument:
    index = int(code)
    return AbsentDocument(
        document_id=f"doc_{index:064x}",
        source_id=f"source_{index:064x}",
        issuer=issuer,
        product_code=code,
        document_type="product-manual",
        pdf_sha256=f"{index:064x}",
        ocr_sha256=None,
        last_observed_run_id="run-base",
        last_observed_at="2026-09-29T00:00:00+00:00",
    )


def _full_maps(items: tuple[AbsentDocument, ...]) -> tuple[dict[str, bool], dict[str, bool]]:
    return {i.document_id: True for i in items}, {i.document_id: True for i in items}


def _evaluate(
    items: tuple[AbsentDocument, ...],
    *,
    run_id: str,
    started_at: datetime,
    ledger: RetirementLedger | None,
    policy: RetirementPolicy | None = None,
    baseline_total: int = 10_000,
    durable: dict[str, bool] | None = None,
    lineage_absent: dict[str, bool] | None = None,
    discovered: set[tuple[str, str, str]] | None = None,
):
    durable_map, lineage_map = _full_maps(items)
    return evaluate_retirements(
        run_id=run_id,
        run_started_at=started_at,
        policy=policy or _policy(),
        baseline_total=baseline_total,
        ledger=ledger,
        absent=items,
        durable_ok=durable if durable is not None else durable_map,
        lineage_absent=lineage_absent if lineage_absent is not None else lineage_map,
        discovered_lineages=discovered or set(),
    )


def test_lotte_regression_two_runs_candidate_then_retired() -> None:
    items = tuple(_absent(code) for code in LOTTE_CODES)
    first = _evaluate(items, run_id="run-1", started_at=datetime(2026, 9, 30, tzinfo=UTC), ledger=None)
    assert first.retired == ()
    assert len(first.candidates) == 8
    assert first.unjustified == {}
    assert all(e.status == "candidate" and e.consecutive_absences == 1 for e in first.ledger.entries)

    # Run 2: documents are outside the rolling baseline; only the open ledger
    # sweep keeps their absence continuous.
    sweep = tuple(
        AbsentDocument(
            document_id=e.document_id,
            source_id=e.source_id,
            issuer=e.issuer,
            product_code=e.product_code,
            document_type=e.document_type,
            pdf_sha256=e.pdf_sha256,
            ocr_sha256=e.ocr_sha256,
            last_observed_run_id=e.last_observed_run_id,
            last_observed_at=e.last_observed_at,
        )
        for e in first.ledger.entries
        if e.status == "candidate"
    )
    second = _evaluate(
        sweep, run_id="run-2", started_at=datetime(2026, 10, 1, tzinfo=UTC), ledger=first.ledger
    )
    assert len(second.retired) == 8
    assert second.candidates == ()
    assert second.unjustified == {}
    retired = {r["document_id"] for r in second.retired}
    assert retired == {i.document_id for i in items}
    for record in second.retired:
        assert record["consecutive_absences"] == 2
        assert record["retired_run_id"] == "run-2"
        assert record["pdf_sha256"]
    entries = {e.document_id: e for e in second.ledger.entries}
    assert all(entries[i.document_id].status == "retired" for i in items)
    assert entries[items[0].document_id].decision_inputs["grace_runs"] == 2


def test_grace_days_path_retires_early() -> None:
    items = (_absent("1151"),)
    first = _evaluate(items, run_id="run-1", started_at=datetime(2026, 9, 28, tzinfo=UTC), ledger=None)
    later = _evaluate(
        items,
        run_id="run-2",
        started_at=datetime(2026, 10, 1, tzinfo=UTC) + timedelta(days=1),
        ledger=first.ledger,
    )
    assert len(later.retired) == 1
    assert later.unjustified == {}


def test_grace_unmet_stays_candidate_never_retired_early() -> None:
    items = tuple(_absent(code) for code in LOTTE_CODES[:2])
    outcome = _evaluate(items, run_id="run-1", started_at=datetime(2026, 9, 30, tzinfo=UTC), ledger=None)
    assert outcome.retired == ()
    assert len(outcome.candidates) == 2


def test_missing_evidence_is_unjustified() -> None:
    items = tuple(_absent(code) for code in LOTTE_CODES[:3])
    durable_map, lineage_map = _full_maps(items)
    victim = items[1].document_id
    durable_map[victim] = False
    outcome = _evaluate(
        items,
        run_id="run-1",
        started_at=datetime(2026, 9, 30, tzinfo=UTC),
        ledger=None,
        durable=durable_map,
        lineage_absent=lineage_map,
    )
    assert outcome.unjustified == {victim: "retirement_evidence_incomplete"}


def test_lineage_still_discovered_is_unjustified() -> None:
    items = (_absent("1151"),)
    discovered = {("lotte", "1151", "product-manual")}
    outcome = _evaluate(
        items,
        run_id="run-1",
        started_at=datetime(2026, 9, 30, tzinfo=UTC),
        ledger=None,
        lineage_absent={items[0].document_id: False},
        discovered=discovered,
    )
    assert outcome.unjustified == {items[0].document_id: "retirement_evidence_incomplete"}


def test_absolute_and_ratio_caps_fail_closed_on_overflow() -> None:
    items = tuple(
        AbsentDocument(
            document_id=f"doc{i:062x}",
            source_id=f"source{i:062x}",
            issuer="kb",
            product_code=f"C{i}",
            document_type="product-manual",
            pdf_sha256=f"{i:064x}",
            ocr_sha256=None,
            last_observed_run_id="run-base",
            last_observed_at="2026-09-29T00:00:00+00:00",
        )
        for i in range(30)
    )
    outcome = _evaluate(
        items,
        run_id="run-1",
        started_at=datetime(2026, 9, 30, tzinfo=UTC),
        ledger=None,
        policy=_policy(max_per_run=25),
        baseline_total=100_000,
    )
    assert len(outcome.candidates) == 25
    assert len(outcome.unjustified) == 5
    assert set(outcome.unjustified.values()) == {"retirement_cap_exceeded"}

    ratio_outcome = _evaluate(
        items,
        run_id="run-1",
        started_at=datetime(2026, 9, 30, tzinfo=UTC),
        ledger=None,
        policy=_policy(max_per_run=25),
        baseline_total=2_000,
    )
    assert len(ratio_outcome.candidates) == 10  # 0.5% of 2,000
    assert len(ratio_outcome.unjustified) == 20


def test_reinstatement_closes_open_retirement_records() -> None:
    items = tuple(_absent(code) for code in LOTTE_CODES[:2])
    first = _evaluate(items, run_id="run-1", started_at=datetime(2026, 9, 30, tzinfo=UTC), ledger=None)
    second = _evaluate(
        (),
        run_id="run-2",
        started_at=datetime(2026, 10, 1, tzinfo=UTC),
        ledger=first.ledger,
        discovered={("lotte", "1151", "product-manual"), ("lotte", "1835", "product-manual")},
    )
    assert set(second.reinstated) == {i.document_id for i in items}
    statuses = {e.document_id: e.status for e in second.ledger.entries}
    assert all(statuses[i.document_id] == "reinstated" for i in items)
    # Reinstated documents do not re-enter the absent sweep and never retire.
    assert second.retired == () and second.candidates == ()


def test_policy_validation_requires_two_run_grace() -> None:
    with pytest.raises(RetirementError) as raised:
        _policy(grace_runs=1)
    assert raised.value.code == "retirement_grace_runs_minimum"


def test_ledger_seal_and_load_roundtrip(tmp_path: Path) -> None:
    assert load_retirement_ledger(tmp_path) is None
    items = (_absent("1151"),)
    outcome = _evaluate(items, run_id="run-1", started_at=datetime(2026, 9, 30, tzinfo=UTC), ledger=None)
    path = write_retirement_ledger(tmp_path, outcome.ledger)
    assert path.read_bytes() == ledger_bytes(outcome.ledger)
    loaded = load_retirement_ledger(tmp_path)
    assert loaded is not None
    assert loaded.entries[0].status == "candidate"
    assert loaded.ledger_sha256 == path.stem
    # Pointer-less crash window still loads the newest sealed version.
    (tmp_path / "audit-reports" / "retirements" / "latest").unlink()
    assert load_retirement_ledger(tmp_path) is not None
    # Tampered pointer fails closed.
    write_retirement_ledger(tmp_path, outcome.ledger)
    pointer = tmp_path / "audit-reports" / "retirements" / "latest"
    pointer.write_text("0" * 64 + "\n")
    with pytest.raises(RetirementError) as raised:
        load_retirement_ledger(tmp_path)
    assert raised.value.code == "retirement_ledger_invalid"


def test_prune_never_deletes_pointer_target(tmp_path: Path) -> None:
    for index in range(4):
        items = tuple(_absent(code) for code in LOTTE_CODES[: index + 1])
        outcome = _evaluate(
            items,
            run_id=f"run-{index}",
            started_at=datetime(2026, 9, 30, tzinfo=UTC),
            ledger=None,
        )
        write_retirement_ledger(tmp_path, outcome.ledger)
    assert prune_retirement_ledgers(tmp_path, keep=2) == 2
    loaded = load_retirement_ledger(tmp_path)
    assert loaded is not None
    assert loaded.updated_run_id == "run-3"
    with pytest.raises(RetirementError):
        prune_retirement_ledgers(tmp_path, keep=0)


def test_ledger_schema_guard(tmp_path: Path) -> None:
    directory = tmp_path / "audit-reports" / "retirements"
    directory.mkdir(parents=True)
    fake = b'{"schema_version": "bogus", "entries": []}'
    import hashlib as _hashlib

    name = _hashlib.sha256(fake).hexdigest() + ".json"
    (directory / name).write_bytes(fake)
    (directory / "latest").write_text(name[:-5] + "\n")
    with pytest.raises(RetirementError):
        load_retirement_ledger(tmp_path)


def test_grace_days_alone_without_two_runs_never_retires() -> None:
    items = (_absent("1151"),)
    first = _evaluate(items, run_id="run-1", started_at=datetime(2026, 9, 25, tzinfo=UTC), ledger=None)
    assert first.retired == ()
    assert len(first.candidates) == 1
    assert first.ledger.entries[0].consecutive_absences == 1
    # 5 days have elapsed, but only 1 run has evaluated it. Must NOT retire.
    resumed_same_run = _evaluate(
        items,
        run_id="run-1",
        started_at=datetime(2026, 9, 30, tzinfo=UTC),
        ledger=first.ledger,
    )
    assert resumed_same_run.retired == ()
    assert len(resumed_same_run.candidates) == 1
    assert resumed_same_run.ledger.entries[0].consecutive_absences == 1
    assert resumed_same_run.ledger.entries[0].status == "candidate"


def test_same_run_resume_is_idempotent() -> None:
    items = (_absent("1151"),)
    first = _evaluate(items, run_id="run-1", started_at=datetime(2026, 9, 30, 3, 0, tzinfo=UTC), ledger=None)
    assert first.ledger.entries[0].consecutive_absences == 1
    # Resuming the exact same run_id must not increment absences
    resumed = _evaluate(
        items,
        run_id="run-1",
        started_at=datetime(2026, 9, 30, 3, 15, tzinfo=UTC),
        ledger=first.ledger,
    )
    assert resumed.ledger.entries[0].consecutive_absences == 1
    assert resumed.ledger.entries[0].status == "candidate"
    assert resumed.retired == ()


def test_retirement_requires_two_distinct_qualifying_runs() -> None:
    items = (_absent("1151"),)
    # Run 1: candidate (absences = 1)
    run1 = _evaluate(items, run_id="run-1", started_at=datetime(2026, 9, 30, tzinfo=UTC), ledger=None)
    assert run1.retired == ()
    assert run1.ledger.entries[0].consecutive_absences == 1
    assert run1.ledger.entries[0].status == "candidate"

    # Distinct Run 2: reaches 2 absences -> retired
    run2 = _evaluate(items, run_id="run-2", started_at=datetime(2026, 10, 1, tzinfo=UTC), ledger=run1.ledger)
    assert len(run2.retired) == 1
    assert run2.candidates == ()
    assert run2.ledger.entries[0].consecutive_absences == 2
    assert run2.ledger.entries[0].status == "retired"
    assert run2.ledger.entries[0].retired_run_id == "run-2"


def test_load_retirement_ledger_validates_completed_run_status(tmp_path: Path) -> None:
    class DummyState:
        def __init__(self, statuses: dict[str, str]) -> None:
            self._statuses = statuses

        def run_status(self, run_id: str) -> str | None:
            return self._statuses.get(run_id)

    items = (_absent("1151"),)
    outcome1 = _evaluate(items, run_id="run-1", started_at=datetime(2026, 9, 30, tzinfo=UTC), ledger=None)
    write_retirement_ledger(tmp_path, outcome1.ledger)

    # When run-1 is succeeded, it loads successfully
    state = DummyState({"run-1": "succeeded"})
    loaded = load_retirement_ledger(tmp_path, state)  # type: ignore[arg-type]
    assert loaded is not None
    assert loaded.updated_run_id == "run-1"

    # When run-1 is failed or interrupted, load_retirement_ledger returns None
    state_failed = DummyState({"run-1": "failed"})
    assert load_retirement_ledger(tmp_path, state_failed) is None  # type: ignore[arg-type]

    # Now write run-2
    outcome2 = _evaluate(
        items, run_id="run-2", started_at=datetime(2026, 10, 1, tzinfo=UTC), ledger=outcome1.ledger
    )
    write_retirement_ledger(tmp_path, outcome2.ledger)

    # run-2 is failed/interrupted, but run-1 was succeeded:
    # load_retirement_ledger must skip unconfirmed run-2 and fall back to run-1
    state_mix = DummyState({"run-1": "succeeded", "run-2": "failed"})
    fallback = load_retirement_ledger(tmp_path, state_mix)  # type: ignore[arg-type]
    assert fallback is not None
    assert fallback.updated_run_id == "run-1"

    # When run-2 finishes as no_change or succeeded, it is loaded
    state_ok = DummyState({"run-1": "succeeded", "run-2": "no_change"})
    loaded2 = load_retirement_ledger(tmp_path, state_ok)  # type: ignore[arg-type]
    assert loaded2 is not None
    assert loaded2.updated_run_id == "run-2"
