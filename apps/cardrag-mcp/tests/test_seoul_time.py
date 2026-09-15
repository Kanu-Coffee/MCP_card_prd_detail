from __future__ import annotations

import zoneinfo
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

import cardrag_mcp.exact as exact_module
import cardrag_mcp.repository as repository_module
from cardrag_mcp.models import ContractSearchRequest
from cardrag_mcp.seoul_time import seoul_today


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [(14, 59, date(2026, 9, 8)), (15, 0, date(2026, 9, 9))],
)
def test_calendar_rollover_does_not_require_iana_data(monkeypatch, hour, minute, expected) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 8, hour, minute, tzinfo=UTC).astimezone(tz)

    def absent_tzdata(key):
        raise zoneinfo.ZoneInfoNotFoundError(key)

    # Reproduce the production image: no system TZPATH and no tzdata package.
    previous_path = zoneinfo.TZPATH
    zoneinfo.ZoneInfo.clear_cache()
    zoneinfo.reset_tzpath(())
    monkeypatch.setattr(zoneinfo._common, "load_tzdata", absent_tzdata)
    try:
        with pytest.raises(zoneinfo.ZoneInfoNotFoundError):
            zoneinfo.ZoneInfo("Asia/Seoul")
        monkeypatch.setattr(repository_module, "datetime", FrozenDatetime)
        monkeypatch.setattr(exact_module, "datetime", FrozenDatetime)
        assert seoul_today(clock=FrozenDatetime) == expected
        assert repository_module._seoul_today() == expected
        assert repository_module._recent_product_period(3)[1] == expected
        assert repository_module._explicit_period(expected, expected) == (expected, expected)
        request = ContractSearchRequest(
            query="혜택", launch_start_date=expected, launch_end_date=expected
        )
        # No revisions are needed to exercise the exact-search date gate.
        assert (
            exact_module.V5ExactRepository._filter_launch_dates(
                SimpleNamespace(), SimpleNamespace(), (), request
            )
            == ()
        )
    finally:
        zoneinfo.reset_tzpath(previous_path)
        zoneinfo.ZoneInfo.clear_cache()
