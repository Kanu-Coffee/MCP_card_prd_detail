from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime

import pytest
from test_metadata_tools import _set_launch_texts
from test_metadata_tools import v5_runtime as catalog_runtime
from v5_fixtures import install_v5_fixture

import cardrag_mcp.catalog as catalog_module
import cardrag_mcp.repository as repository_module
from cardrag_mcp.launch_date import resolve_launch_date_details
from cardrag_mcp.metadata_cache import MetadataCache
from cardrag_mcp.models import ProductCoverage, ProductSummaryBatch, ProductSummaryRequest


@pytest.fixture
def v5_runtime(tmp_path, monkeypatch):
    return catalog_runtime.__wrapped__(tmp_path, monkeypatch)


def _add_products(fixture, count: int, *, launch: str | None = "2026.08.12") -> None:
    """Extend a temporary fixture before queries, without touching runtime state."""
    with sqlite3.connect(fixture.database) as connection:
        connection.row_factory = sqlite3.Row
        revision = dict(
            connection.execute(
                "SELECT * FROM contract_revisions WHERE contract_revision_id=?",
                (fixture.current_revision_id,),
            ).fetchone()
        )
        lineage = dict(
            connection.execute(
                "SELECT * FROM product_lineages WHERE product_lineage_id=?",
                (revision["product_lineage_id"],),
            ).fetchone()
        )
        node = dict(
            connection.execute(
                "SELECT * FROM structure_nodes WHERE contract_revision_id=? AND ordinal=3",
                (fixture.current_revision_id,),
            ).fetchone()
        )
        for index in range(count):
            code = f"EXTRA{index:03d}"
            lineage.update(
                product_lineage_id=f"lineage-{code}",
                product_code=code,
                name=f"알파 추가 {index:03d}",
            )
            revision.update(
                contract_revision_id=f"revision-{code}",
                product_lineage_id=lineage["product_lineage_id"],
                document_id=f"document-{code}",
                supersedes_revision_id=None,
            )
            node.update(
                node_id=f"node-{code}",
                contract_revision_id=revision["contract_revision_id"],
                parent_id=None,
                parent_contract_revision_id=None,
                display_text=f"출시일: {launch}" if launch else "출시일 미기재",
            )
            for table, values in (
                ("product_lineages", lineage),
                ("contract_revisions", revision),
                ("structure_nodes", node),
            ):
                columns = ",".join(values)
                placeholders = ",".join("?" for _ in values)
                connection.execute(
                    f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",  # noqa: S608 - fixture names
                    tuple(values.values()),
                )


@pytest.mark.asyncio
async def test_exact_past_period_and_multiple_issuer_aliases(v5_runtime) -> None:
    _, repository, fixture = v5_runtime
    _set_launch_texts(fixture, "출시일: 2026.08.12")
    page = await repository.list_recent_products(
        start_date=date(2026, 8, 12), end_date=date(2026, 8, 12), issuers=["KB국민카드", "우리카드"]
    )
    assert page.total_count == 1
    assert page.period_start == page.period_end == date(2026, 8, 12)
    assert page.items[0].launch_date == date(2026, 8, 12)
    assert page.items[0].contract_revision_id == fixture.current_revision_id
    assert page.items[0].launch_date_status == "confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"start_date": date(2026, 8, 1)}, "provided together"),
        ({"start_date": date(2026, 8, 2), "end_date": date(2026, 8, 1)}, "must not be after"),
        ({"start_date": date(2026, 8, 1), "end_date": date(2026, 9, 10)}, "future"),
        (
            {"months": 3, "start_date": date(2026, 8, 1), "end_date": date(2026, 8, 2)},
            "mutually exclusive",
        ),
        ({"issuer": "kb", "issuers": ["kb"]}, "mutually exclusive"),
        ({"issuers": []}, "between 1 and 8"),
        ({"issuers": ["unknown issuer"]}, "unknown issuer"),
        ({"limit": True}, "limit must"),
        ({"limit": 101}, "limit must"),
        ({"expected_generation_id": "old-generation"}, "generation mismatch"),
    ],
)
async def test_recent_rejects_ambiguous_or_invalid_inputs(v5_runtime, kwargs, message) -> None:
    _, repository, _ = v5_runtime
    with pytest.raises(ValueError, match=message):
        await repository.list_recent_products(**kwargs)


def test_today_uses_seoul_calendar_not_host_timezone(monkeypatch) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 8, 15, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(repository_module, "datetime", FrozenDatetime)
    assert repository_module._seoul_today() == date(2026, 9, 9)


@pytest.mark.asyncio
async def test_new_catalog_paginates_without_duplicates_while_legacy_is_unlimited(
    v5_runtime,
) -> None:
    _, repository, fixture = v5_runtime
    _add_products(fixture, 104)
    legacy = await repository.find_products("알파")
    assert len(legacy.items) == legacy.total_count == 105
    first = await repository.find_products(mode="catalog", sort="launch_date")
    assert first.total_count == 105 and len(first.items) == 50
    assert first.next_cursor is not None
    second = await repository.find_products(
        mode="catalog", sort="launch_date", cursor=first.next_cursor
    )
    third = await repository.find_products(
        mode="catalog", sort="launch_date", cursor=second.next_cursor
    )
    combined = first.items + second.items + third.items
    assert len({item.contract_revision_id for item in combined}) == 105
    assert third.next_cursor is None
    assert combined[-1].product_code == "ALPHA" and combined[-1].launch_date is None
    assert [item.product_code for item in combined[:-1]] == [f"EXTRA{i:03d}" for i in range(104)]
    with pytest.raises(ValueError, match="invalid or stale cursor"):
        await repository.find_products(mode="catalog", sort="name", cursor=first.next_cursor)
    with pytest.raises(ValueError, match="invalid or stale cursor"):
        await repository.find_products(
            mode="catalog", sort="launch_date", issuers=["kb"], cursor=first.next_cursor
        )
    with pytest.raises(ValueError, match="invalid or stale cursor"):
        await repository.find_products(
            mode="catalog", sort="launch_date", cursor=first.next_cursor[:-2] + "xx"
        )


@pytest.mark.asyncio
async def test_recent_default_page_and_scope_bound_cursor(v5_runtime) -> None:
    _, repository, fixture = v5_runtime
    _add_products(fixture, 60)
    legacy = await repository.list_recent_products(months=3)
    assert len(legacy.items) == 60
    first = await repository.list_recent_products(
        start_date=date(2026, 8, 1), end_date=date(2026, 8, 31)
    )
    assert len(first.items) == 50 and first.total_count == 60
    second = await repository.list_recent_products(
        start_date=date(2026, 8, 1), end_date=date(2026, 8, 31), cursor=first.next_cursor
    )
    assert len(second.items) == 10 and second.next_cursor is None
    assert len({item.product_code for item in first.items + second.items}) == 60
    with pytest.raises(ValueError, match="invalid or stale cursor"):
        await repository.list_recent_products(
            start_date=date(2026, 8, 2), end_date=date(2026, 8, 31), cursor=first.next_cursor
        )


@pytest.mark.asyncio
async def test_coverage_reports_supported_loaded_and_unavailable_separately(v5_runtime) -> None:
    store, repository, _ = v5_runtime
    install_v5_fixture(store, generation_id="coverage-with-dispositions", include_dispositions=True)
    coverage = await repository.find_products(mode="coverage")
    assert isinstance(coverage, ProductCoverage)
    assert len(coverage.supported_issuers) == 8
    assert coverage.loaded_issuers == ("kb",)
    assert coverage.product_count == 4
    kb = next(item for item in coverage.issuers if item.issuer == "kb")
    assert kb.product_count == 4 and kb.available_product_count == 2
    assert kb.unknown_launch_date_count == kb.current_revision_count == 1
    assert kb.confirmed_launch_date_count == 0
    assert kb.unsupported_drm_count == kb.ocr_failed_count == 1
    woori = next(item for item in coverage.issuers if item.issuer == "woori")
    assert not woori.loaded and woori.product_count == 0
    assert coverage.coverage_scope == "loaded_generation_only"


@pytest.mark.asyncio
async def test_summaries_batch_one_pin_order_null_and_source_evidence(
    v5_runtime, monkeypatch
) -> None:
    store, repository, fixture = v5_runtime
    _set_launch_texts(fixture, "출시일: 2026.08.12")
    original_pin = store.pin
    calls = []

    @contextmanager
    def counted_pin():
        calls.append(True)
        with original_pin() as handle:
            yield handle

    monkeypatch.setattr(store, "pin", counted_pin)
    result = await repository.get_product_summary(
        products=[
            ProductSummaryRequest(issuer="KB국민카드", identifier="ALPHA"),
            ProductSummaryRequest(issuer="kb", identifier="absent"),
            ProductSummaryRequest(issuer="kb", identifier="ALPHA"),
        ]
    )
    assert isinstance(result, ProductSummaryBatch)
    assert calls == [True]
    first = result.items[0]
    assert first is not None and first == result.items[2] and result.items[1] is None
    assert first.generation_id == result.generation_id == fixture.generation_id
    assert first.contract_revision_id == fixture.current_revision_id
    assert first.launch_date_status == "confirmed" and first.launch_date_evidence
    assert first.document_id and first.source_id and first.source_url and first.pdf_sha256
    assert any(item.field == "launch_date" and item.node_id for item in first.evidence)
    assert any(item.pages for item in first.evidence)


@pytest.mark.asyncio
async def test_ambiguous_summary_identifies_candidates_and_exact_lineage_resolves(
    v5_runtime,
) -> None:
    _, repository, fixture = v5_runtime
    _add_products(fixture, 2)
    with pytest.raises(ValueError, match="ambiguous product identifier") as error:
        await repository.get_product_summary("kb", "알파")
    assert "EXTRA000" in str(error.value) and "ALPHA" in str(error.value)
    selected = await repository.get_product_summary("kb", "lineage-EXTRA000")
    assert selected is not None and selected.product_code == "EXTRA000"


@pytest.mark.asyncio
@pytest.mark.parametrize("products", [[], [{"issuer": "kb", "identifier": "ALPHA"}] * 51])
async def test_batch_summary_bounds(v5_runtime, products) -> None:
    _, repository, _ = v5_runtime
    with pytest.raises(ValueError, match="between 1 and 50"):
        await repository.get_product_summary(products=products)


@pytest.mark.asyncio
async def test_metadata_reuses_missing_dates_and_invalidates_parser_version(
    v5_runtime, monkeypatch
) -> None:
    _, repository, fixture = v5_runtime
    first = await repository.find_products("알파")
    assert first.items[0].launch_date is None
    before = repository.metadata_cache.snapshot()
    await repository.list_recent_products(months=3)
    after = repository.metadata_cache.snapshot()
    assert after["hits"] > before["hits"] and after["misses"] == before["misses"]
    # Simulate a parser deployment against a private fixture. Immutable runtime
    # generations would only change with a new generation/revision/source hash.
    _set_launch_texts(fixture, "출시일: 2026.08.12")
    monkeypatch.setattr(catalog_module, "PARSER_VERSION", "test-parser-v-next")
    updated = await repository.find_products("알파")
    assert updated.items[0].launch_date == date(2026, 8, 12)
    assert repository.metadata_cache.snapshot()["misses"] > after["misses"]


def test_metadata_cache_lru_bounds_and_serialized_values() -> None:
    cache = MetadataCache(max_entries=2, max_bytes=100)
    value = {"state": "missing"}
    cache.set(("g1", "r1"), value)
    value["state"] = "mutated"
    assert cache.get(("g1", "r1")) == {"state": "missing"}
    assert cache.get(("g2", "r1")) is None
    cache.set(("g1", "r2"), {"state": "invalid"})
    cache.get(("g1", "r1"))
    cache.set(("g1", "r3"), {"state": "missing"})
    assert cache.get(("g1", "r2")) is None
    assert cache.snapshot()["entries"] == 2
    cache.set(("g1", "huge"), {"state": "x" * 1000})
    assert cache.snapshot()["bytes"] <= 100
    assert cache.snapshot()["evictions"] == 1


@pytest.mark.parametrize(
    "texts,status",
    [
        (["출시일 미기재"], "missing"),
        (["출시일: 2026.08.12"], "confirmed"),
        (["출시일: 2026.08.99"], "invalid"),
        (["출시일: 2026.08.12", "출시일: 2026.09.01"], "conflicting"),
        (["출시일: 2026.08.12 / 출시일: 2026.09.01abc"], "invalid"),
    ],
)
def test_launch_resolution_reasons(texts, status) -> None:
    result = resolve_launch_date_details(texts)
    assert result.status == status
    assert (result.launch_date is not None) == (status == "confirmed")


@pytest.mark.asyncio
async def test_generation_switch_rejects_cursor_and_releases_old_handle(v5_runtime) -> None:
    store, repository, fixture = v5_runtime
    _add_products(fixture, 2)
    first = await repository.find_products(mode="catalog", limit=1)
    old_generation = first.generation_id
    assert first.next_cursor is not None
    install_v5_fixture(store, generation_id="new-catalog-generation")
    with pytest.raises(ValueError, match="invalid or stale cursor"):
        await repository.find_products(mode="catalog", limit=1, cursor=first.next_cursor)
    with pytest.raises(ValueError, match="generation mismatch"):
        await repository.get_product_summary("kb", "ALPHA", expected_generation_id=old_generation)
    # Serialized cache values retain no GenerationHandle references.
    assert all(entry.references == 0 for entry in store._entries.values())


@pytest.mark.asyncio
async def test_legacy_coverage_keeps_counts_but_dates_are_explicitly_unsupported(
    active_runtime,
) -> None:
    _, repository, _, _ = active_runtime
    result = await repository.find_products(mode="coverage")
    assert result.launch_date_support == "unsupported_schema"
    assert result.product_count > 0
    assert all(item.current_revision_count is None for item in result.issuers)
    assert all(item.unknown_launch_date_count is None for item in result.issuers)
    catalog = await repository.find_products(mode="catalog")
    assert catalog.schema_status == "unsupported_schema"
    assert catalog.items == ()
    with pytest.raises(ValueError, match="require serving schema v5"):
        await repository.get_product_summary(products=[{"issuer": "kb", "identifier": "test"}])


@pytest.mark.asyncio
async def test_fifty_summary_inputs_resolve_once_and_cache_source_changes(
    v5_runtime, monkeypatch
) -> None:
    _, repository, fixture = v5_runtime
    result = await repository.get_product_summary(
        products=[{"issuer": "kb", "identifier": "ALPHA"}] * 50
    )
    assert len(result.items) == 50
    before = repository.metadata_cache.snapshot()
    await repository.get_product_summary(products=[{"issuer": "kb", "identifier": "ALPHA"}] * 50)
    after = repository.metadata_cache.snapshot()
    assert after["hits"] == before["hits"] + 1
    assert after["misses"] == before["misses"]
    _set_launch_texts(fixture, "출시일: 2026.08.12")
    with sqlite3.connect(fixture.database) as connection:
        connection.execute(
            "UPDATE contract_revisions SET pdf_sha256=? WHERE contract_revision_id=?",
            ("f" * 64, fixture.current_revision_id),
        )
    updated = await repository.get_product_summary("kb", "ALPHA")
    assert updated.launch_date == date(2026, 8, 12)
    assert updated.pdf_sha256 == "f" * 64
