from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cardrag.db import Postgres
from cardrag.generation import GenerationStore, new_generation_id
from cardrag.generation_builder import GenerationBuilder

pytestmark = pytest.mark.integration


def _insert_generation(
    database: Postgres,
    generation_id: str,
    *,
    state: str,
    age_days: int,
) -> None:
    with database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO generations(
                generation_id, state, manifest_sha256, root_uri, schema_version,
                embedding_provider, embedding_model, embedding_dimension, created_at
            ) VALUES (%s,%s,repeat('0',64),%s,'cardrag-generation.v1',
                      'openrouter','fixture-embedding-v1',1536,
                      now() - make_interval(days => %s))
            """,
            (generation_id, state, f"generations/{generation_id}", age_days),
        )
        connection.commit()


def _generation_tree(root: Path, generation_id: str, *, ready: bool, age_days: int) -> Path:
    path = root / generation_id
    path.mkdir(parents=True)
    (path / "diagnostic.json").write_text('{"fixture":true}\n', encoding="utf-8")
    if ready:
        (path / "READY").write_text("ready\n", encoding="utf-8")
    modified = (datetime.now(UTC) - timedelta(days=age_days)).timestamp()
    os.utime(path, (modified, modified))
    return path


def test_database_and_filesystem_retention_keeps_latest_three_pin_active_and_recent_failed(
    clean_database: Postgres,
    tmp_path: Path,
) -> None:
    store = GenerationStore(
        tmp_path / "published",
        tmp_path / "build" / "generation-candidates",
    )
    successful = [
        new_generation_id(datetime(2026, 8, day, tzinfo=UTC), f"{day:012x}")
        for day in range(1, 6)
    ]
    # Five successful generations prove both sides of the policy: the newest
    # three remain, the oldest remains only because it is pinned, and the
    # fourth-ranked unpinned generation is removed.  Filesystem mtimes are
    # deliberately the reverse of database created_at: successful[1] is among
    # the three newest trees by mtime, so an independent FS top-three policy
    # would incorrectly retain it.
    for index, generation_id in enumerate(successful):
        database_age_days = 5 - index
        _insert_generation(
            clean_database,
            generation_id,
            state="published" if index == 4 else "retired",
            age_days=database_age_days,
        )
        _generation_tree(store.generations, generation_id, ready=True, age_days=index + 1)

    # Simulate a previous coordinator dying after its DB delete committed but
    # before the exact filesystem deletion.  A retry must reconcile this fresh
    # READY orphan even though mtime-only retention would keep it.
    committed_orphan = new_generation_id(
        datetime(2026, 8, 10, tzinfo=UTC),
        "0000000000cc",
    )
    _generation_tree(store.generations, committed_orphan, ready=True, age_days=0)

    recent_failed = new_generation_id(datetime(2026, 7, 30, tzinfo=UTC), "0000000000aa")
    expired_failed = new_generation_id(datetime(2026, 7, 29, tzinfo=UTC), "0000000000bb")
    old_building = new_generation_id(datetime(2026, 7, 28, tzinfo=UTC), "0000000000dd")
    old_build_orphan = new_generation_id(datetime(2026, 7, 27, tzinfo=UTC), "0000000000ee")
    _insert_generation(clean_database, recent_failed, state="failed", age_days=6)
    _insert_generation(clean_database, expired_failed, state="failed", age_days=8)
    _insert_generation(clean_database, old_building, state="building", age_days=20)
    _generation_tree(store.build_root, recent_failed, ready=False, age_days=6)
    _generation_tree(store.build_root, expired_failed, ready=False, age_days=8)
    _generation_tree(store.build_root, old_building, ready=False, age_days=20)
    _generation_tree(store.build_root, old_build_orphan, ready=False, age_days=20)

    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO active_generation(singleton, generation_id) VALUES (true, %s)",
            (successful[-1],),
        )
        cursor.execute(
            "INSERT INTO generation_pins(generation_id, reason) VALUES (%s, 'incident fixture')",
            (successful[0],),
        )
        connection.commit()

    removed = GenerationBuilder(clean_database, store).prune()

    assert removed == sorted(
        [successful[1], expired_failed, committed_orphan, old_build_orphan]
    )
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT generation_id FROM generations ORDER BY generation_id")
        retained = {str(row["generation_id"]) for row in cursor.fetchall()}
        cursor.execute("SELECT generation_id FROM active_generation WHERE singleton=true")
        active = cursor.fetchone()
        cursor.execute("SELECT generation_id FROM generation_pins")
        pinned = cursor.fetchone()
    assert retained == {successful[0], *successful[2:], recent_failed, old_building}
    assert active == {"generation_id": successful[-1]}
    assert pinned == {"generation_id": successful[0]}
    assert not (store.generations / successful[1]).exists()
    assert not (store.generations / committed_orphan).exists()
    assert not (store.build_root / expired_failed).exists()
    assert (store.generations / successful[0]).is_dir()
    assert all((store.generations / generation_id).is_dir() for generation_id in successful[2:])
    assert (store.build_root / recent_failed).is_dir()
    assert (store.build_root / old_building).is_dir()
    assert not (store.build_root / old_build_orphan).exists()
