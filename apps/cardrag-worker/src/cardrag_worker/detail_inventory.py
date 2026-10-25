"""Complete inventories assembled from fresh lists and rolling, parsed detail rows."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .contracts import SourceRecord

if TYPE_CHECKING:
    from .state import WorkerState

from .contracts import canonical_json_bytes, canonical_sha256


class DetailInventory:
    def __init__(self, state: WorkerState) -> None:
        self.state = state
        state.connection.executescript("""
            CREATE TABLE IF NOT EXISTS issuer_detail_inventory (
                issuer TEXT, product_code TEXT, fingerprint TEXT NOT NULL,
                payload_json TEXT NOT NULL, detail_observed_at TEXT NOT NULL, next_detail_at TEXT NOT NULL,
                PRIMARY KEY(issuer,product_code)
            );
        """)

    def lookup(
        self, issuer: str, product_code: str, fingerprint: str, *, now: datetime
    ) -> list[SourceRecord] | None:
        from datetime import datetime

        from .pipeline import _restore_snapshot

        row = self.state.connection.execute(
            "SELECT * FROM issuer_detail_inventory WHERE issuer=? AND product_code=?", (issuer, product_code)
        ).fetchone()
        if (
            row is None
            or row["fingerprint"] != fingerprint
            or now >= datetime.fromisoformat(row["next_detail_at"])
        ):
            return None
        snapshot = _restore_snapshot(
            json.loads(row["payload_json"]),
            observed_at=now,
            expected_issuer=issuer,
            expected_parser_version="detail-inventory.v1",
        )
        return list(snapshot.records)

    def put(
        self,
        issuer: str,
        product_code: str,
        fingerprint: str,
        records: Sequence[SourceRecord],
        *,
        now: datetime,
    ) -> None:
        fraction = int(canonical_sha256([issuer, product_code])[:8], 16) / 0xFFFFFFFF
        payload = {
            "contract_version": "cardrag.source-snapshot.v1",
            "issuer": issuer,
            "parser_version": "detail-inventory.v1",
            "records": [r.discovery_payload for r in records],
        }
        self.state.connection.execute(
            "INSERT OR REPLACE INTO issuer_detail_inventory VALUES (?,?,?,?,?,?)",
            (
                issuer,
                product_code,
                fingerprint,
                canonical_json_bytes(payload).decode(),
                now.isoformat(),
                (now + timedelta(days=15 + 15 * fraction)).isoformat(),
            ),
        )
        self.state.connection.commit()
