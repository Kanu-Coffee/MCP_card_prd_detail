"""Current Korean calendar dates without a runtime IANA database dependency."""

from datetime import date, datetime, timedelta, timezone

# These serving decisions concern the current calendar date only, never conversion
# of historical Korean timestamps. Korea's current civil offset is UTC+09:00.
# A fixed offset keeps the shell-free production image independent of tzdata.
SEOUL_CURRENT_OFFSET = timezone(timedelta(hours=9), name="Asia/Seoul")


def seoul_today(*, clock: type[datetime] = datetime) -> date:
    return clock.now(SEOUL_CURRENT_OFFSET).date()
