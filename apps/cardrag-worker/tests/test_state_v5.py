from __future__ import annotations

import math
import sqlite3
import struct
from pathlib import Path

import pytest

from cardrag_worker.state import WorkerState


def test_v5_embedding_cache_is_profile_bound_normalized_and_separate_from_v4(
    tmp_path: Path,
) -> None:
    cache_key = "1" * 64
    input_sha256 = "2" * 64
    with WorkerState(tmp_path / "state.sqlite3") as state:
        state.put_embedding(
            cache_key=cache_key,
            contract_sha256="3" * 64,
            text_sha256=input_sha256,
            embedding=b"\x00" * 6144,
        )
        assert (
            state.get_embedding_v5(
                cache_key,
                profile_id="profile-deepinfra",
                input_sha256=input_sha256,
                dimension=4,
            )
            is None
        )

        cached = state.put_embedding_v5(
            cache_key=cache_key,
            profile_id="profile-deepinfra",
            input_sha256=input_sha256,
            dimension=4,
            values=[3.0, 4.0, 0.0, 0.0],
        )
        assert cached.profile_id == "profile-deepinfra"
        assert cached.dtype == "float32"
        assert cached.normalization == "l2"
        values = struct.unpack("<4f", cached.embedding)
        assert values == pytest.approx((0.6, 0.8, 0.0, 0.0))
        assert math.isclose(sum(value * value for value in values), 1.0, rel_tol=2e-5)

        loaded = state.get_embedding_v5(
            cache_key,
            profile_id="profile-deepinfra",
            input_sha256=input_sha256,
            dimension=4,
        )
        assert loaded == cached
        with pytest.raises(RuntimeError, match="different profile or input"):
            state.get_embedding_v5(
                cache_key,
                profile_id="profile-nebius",
                input_sha256=input_sha256,
                dimension=4,
            )
        with pytest.raises(RuntimeError, match="collision"):
            state.put_embedding_v5(
                cache_key=cache_key,
                profile_id="profile-deepinfra",
                input_sha256=input_sha256,
                dimension=4,
                values=[1.0, 0.0, 0.0, 0.0],
            )


@pytest.mark.parametrize(
    "values",
    (
        [0.0, 0.0],
        [math.inf, 0.0],
        [math.nan, 1.0],
        [1.0],
    ),
)
def test_v5_embedding_cache_rejects_invalid_vectors(tmp_path: Path, values: list[float]) -> None:
    with WorkerState(tmp_path / "state.sqlite3") as state, pytest.raises(ValueError):
        state.put_embedding_v5(
            cache_key="a" * 64,
            profile_id="profile",
            input_sha256="b" * 64,
            dimension=2,
            values=values,
        )


def test_v5_embedding_cache_sql_check_binds_blob_length_to_dimension(tmp_path: Path) -> None:
    with WorkerState(tmp_path / "state.sqlite3") as state, pytest.raises(sqlite3.IntegrityError):
        state.connection.execute(
            """INSERT INTO embedding_cache_v5
               (cache_key,profile_id,input_sha256,dimension,dtype,normalization,embedding,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                "c" * 64,
                "profile",
                "d" * 64,
                4096,
                "float32",
                "l2",
                b"wrong-size",
                "2026-01-01T00:00:00+00:00",
            ),
        )


@pytest.mark.parametrize(
    "values,match",
    (([2.0, 0.0], "non-normalized"), ([math.inf, 0.0], "non-finite"), ([math.nan, 0.0], "non-finite")),
)
def test_v5_embedding_cache_detects_invalid_persisted_bytes(
    tmp_path: Path, values: list[float], match: str
) -> None:
    with WorkerState(tmp_path / "state.sqlite3") as state:
        state.connection.execute(
            """INSERT INTO embedding_cache_v5
               (cache_key,profile_id,input_sha256,dimension,dtype,normalization,embedding,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                "e" * 64,
                "profile",
                "f" * 64,
                2,
                "float32",
                "l2",
                struct.pack("<2f", *values),
                "2026-01-01T00:00:00+00:00",
            ),
        )
        with pytest.raises(RuntimeError, match=match):
            state.get_embedding_v5(
                "e" * 64,
                profile_id="profile",
                input_sha256="f" * 64,
                dimension=2,
            )
        # The explicit fast path defers these numeric checks to the exporter.
        cached = state.get_embedding_v5(
            "e" * 64,
            profile_id="profile",
            input_sha256="f" * 64,
            dimension=2,
            validate_norm=False,
        )
        assert cached is not None
        assert cached.embedding == struct.pack("<2f", *values)


@pytest.mark.parametrize("validate_norm", (True, False))
def test_v5_embedding_cache_always_validates_identity_and_length(tmp_path: Path, validate_norm: bool) -> None:
    with WorkerState(tmp_path / "state.sqlite3") as state:
        state.put_embedding_v5(
            cache_key="a" * 64,
            profile_id="profile",
            input_sha256="b" * 64,
            dimension=2,
            values=[1.0, 0.0],
        )
        with pytest.raises(RuntimeError, match="different profile or input"):
            state.get_embedding_v5(
                "a" * 64,
                profile_id="other-profile",
                input_sha256="b" * 64,
                dimension=2,
                validate_norm=validate_norm,
            )
        # Simulate an externally corrupted row, bypassing the normal SQL
        # constraint only inside this fixture.
        state.connection.execute("PRAGMA ignore_check_constraints=ON")
        state.connection.execute(
            "UPDATE embedding_cache_v5 SET embedding=? WHERE cache_key=?", (b"bad", "a" * 64)
        )
        state.connection.execute("PRAGMA ignore_check_constraints=OFF")
        with pytest.raises(RuntimeError, match="blob length"):
            state.get_embedding_v5(
                "a" * 64,
                profile_id="profile",
                input_sha256="b" * 64,
                dimension=2,
                validate_norm=validate_norm,
            )


@pytest.mark.parametrize("cache_mib,mmap_mib", ((256, 2048), (1, 0), (1024, 4096)))
def test_worker_state_observes_actual_sqlite_tuning(tmp_path: Path, cache_mib: int, mmap_mib: int) -> None:
    with WorkerState(
        tmp_path / "state.sqlite3", sqlite_cache_mib=cache_mib, sqlite_mmap_mib=mmap_mib
    ) as state:
        settings = state.sqlite_settings
        assert settings["cache_size"] == -cache_mib * 1024
        assert 0 <= settings["mmap_size"] <= mmap_mib * 1024 * 1024
        observed_mmap = state.connection.execute("PRAGMA mmap_size").fetchone()
        assert settings["mmap_size"] == (0 if observed_mmap is None else int(observed_mmap[0]))
        assert settings["temp_store"] == 0
        assert state.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert state.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert state.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 30000


@pytest.mark.parametrize(
    "cache_mib,mmap_mib",
    ((0, 2048), (1025, 2048), (True, 2048), (256, -1), (256, 4097), (256, False)),
)
def test_worker_state_rejects_invalid_tuning_before_creating_database(
    tmp_path: Path, cache_mib: int, mmap_mib: int
) -> None:
    path = tmp_path / "nested" / "state.sqlite3"
    with pytest.raises(ValueError, match="sqlite_(cache|mmap)_mib"):
        WorkerState(path, sqlite_cache_mib=cache_mib, sqlite_mmap_mib=mmap_mib)
    assert not path.parent.exists()
