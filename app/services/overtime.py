from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Sequence
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from app.models.check_in import CheckIn

# ── 勞基法常數（法規修改時只改這個區塊）─────────────────────
REGULAR_WORK_MINUTES = 480   # 正常工時 8 小時
OT_UNIT_MINUTES = 30         # 最小加班單位（不足 30 分鐘不計）
OT_TIER1_CAP_MINUTES = 120   # 加班第一段上限（第 1–2 小時，勞基法第 24 條第 1 款）
OT_TIER2_CAP_MINUTES = 120   # 加班第二段上限（第 3–4 小時，勞基法第 24 條第 2 款）
DAILY_OT_LEGAL_MAX = 240     # 每日加班法定上限（4 小時，勞基法第 32 條）
MONTHLY_OT_LIMIT = 2760      # 月加班上限（46 小時，勞基法第 32 條）


@dataclass
class DailyWorkSummary:
    date: date
    clock_in: datetime | None    # first clock_in of the day (UTC)
    clock_out: datetime | None   # last clock_out of the day (UTC)
    work_minutes: int
    regular_minutes: int
    ot_tier1_minutes: int        # counted tier-1 OT (加班第一段，0–120 min)
    ot_tier2_minutes: int        # counted tier-2 OT (加班第二段，0–120 min)
    ot_counted_minutes: int      # tier1 + tier2 (30-min-unit rounded)
    ot_remainder_minutes: int    # fraction dropped by the 30-min rule
    in_progress: bool            # clocked in, no clock_out yet
    exceeds_legal_limit: bool    # total OT > 4 h (勞基法違規)


def compute_daily_summary(day: date, records: Sequence[CheckIn]) -> DailyWorkSummary:
    """Compute work/overtime for one calendar day from its CheckIn rows."""
    from app.models.check_in import CheckInType

    ins = sorted(
        [r for r in records if r.type == CheckInType.clock_in],
        key=lambda r: r.checked_at,
    )
    outs = sorted(
        [r for r in records if r.type == CheckInType.clock_out],
        key=lambda r: r.checked_at,
    )

    first_in = ins[0].checked_at if ins else None
    last_out = outs[-1].checked_at if outs else None
    in_progress = first_in is not None and last_out is None

    if first_in is None or last_out is None:
        return DailyWorkSummary(
            date=day,
            clock_in=first_in,
            clock_out=last_out,
            work_minutes=0,
            regular_minutes=0,
            ot_tier1_minutes=0,
            ot_tier2_minutes=0,
            ot_counted_minutes=0,
            ot_remainder_minutes=0,
            in_progress=in_progress,
            exceeds_legal_limit=False,
        )

    work_minutes = int((last_out - first_in).total_seconds() // 60)
    regular_minutes = min(work_minutes, REGULAR_WORK_MINUTES)

    ot_raw = max(work_minutes - REGULAR_WORK_MINUTES, 0)
    ot_counted = (ot_raw // OT_UNIT_MINUTES) * OT_UNIT_MINUTES
    ot_remainder = ot_raw - ot_counted

    ot_tier1 = min(ot_counted, OT_TIER1_CAP_MINUTES)
    ot_tier2 = min(max(ot_counted - OT_TIER1_CAP_MINUTES, 0), OT_TIER2_CAP_MINUTES)

    return DailyWorkSummary(
        date=day,
        clock_in=first_in,
        clock_out=last_out,
        work_minutes=work_minutes,
        regular_minutes=regular_minutes,
        ot_tier1_minutes=ot_tier1,
        ot_tier2_minutes=ot_tier2,
        ot_counted_minutes=ot_tier1 + ot_tier2,
        ot_remainder_minutes=ot_remainder,
        in_progress=in_progress,
        exceeds_legal_limit=ot_raw > DAILY_OT_LEGAL_MAX,
    )


def compute_monthly_summaries(
    records: Sequence[CheckIn],
    tz: ZoneInfo,
) -> list[DailyWorkSummary]:
    """Group CheckIn rows by local date and return a DailyWorkSummary per day."""
    by_date: dict[date, list] = defaultdict(list)
    for r in records:
        by_date[r.checked_at.astimezone(tz).date()].append(r)

    return [
        compute_daily_summary(day, day_records)
        for day, day_records in sorted(by_date.items(), reverse=True)
    ]


def monthly_ot_total(summaries: list[DailyWorkSummary]) -> int:
    """Total counted overtime minutes across all daily summaries."""
    return sum(s.ot_counted_minutes for s in summaries)
