"""Unit tests for the overtime calculation service (app/services/overtime.py).

All tests use plain CheckIn-like objects — no DB required.
"""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services.overtime import (
    MONTHLY_OT_LIMIT,
    REGULAR_WORK_MINUTES,
    compute_daily_summary,
    compute_monthly_summaries,
    monthly_ot_total,
)

TZ = ZoneInfo("Asia/Taipei")


def _dt(hour: int, minute: int = 0, day: int = 1) -> datetime:
    """Return a timezone-aware datetime in Asia/Taipei on 2026-04-{day}."""
    return datetime(2026, 4, day, hour, minute, tzinfo=TZ)


class _FakeCheckIn:
    """Minimal stand-in for CheckIn ORM row."""
    def __init__(self, type_, checked_at: datetime):
        self.type = type_
        self.checked_at = checked_at


def _make(type_str: str, hour: int, minute: int = 0, day: int = 1) -> _FakeCheckIn:
    from app.models.check_in import CheckInType
    return _FakeCheckIn(CheckInType(type_str), _dt(hour, minute, day))


# ── compute_daily_summary ──────────────────────────────────────────────────────

def test_no_records_returns_zero():
    s = compute_daily_summary(date(2026, 4, 1), [])
    assert s.work_minutes == 0
    assert s.ot_counted_minutes == 0
    assert not s.in_progress
    assert not s.exceeds_legal_limit


def test_only_clock_in_is_in_progress():
    records = [_make("clock_in", 9)]
    s = compute_daily_summary(date(2026, 4, 1), records)
    assert s.in_progress
    assert s.work_minutes == 0
    assert s.ot_counted_minutes == 0


def test_only_clock_out_no_work():
    records = [_make("clock_out", 18)]
    s = compute_daily_summary(date(2026, 4, 1), records)
    assert not s.in_progress
    assert s.work_minutes == 0


def test_exactly_8_hours_no_overtime():
    records = [_make("clock_in", 9), _make("clock_out", 17)]
    s = compute_daily_summary(date(2026, 4, 1), records)
    assert s.work_minutes == 480
    assert s.ot_counted_minutes == 0
    assert s.regular_minutes == 480


def test_under_30_min_overtime_not_counted():
    # 8h 29min → no OT
    records = [_make("clock_in", 9), _make("clock_out", 17, 29)]
    s = compute_daily_summary(date(2026, 4, 1), records)
    assert s.ot_counted_minutes == 0
    assert s.ot_remainder_minutes == 29


def test_exactly_30_min_overtime_counts():
    # 8h 30min → 30 min tier-1 OT
    records = [_make("clock_in", 9), _make("clock_out", 17, 30)]
    s = compute_daily_summary(date(2026, 4, 1), records)
    assert s.ot_counted_minutes == 30
    assert s.ot_tier1_minutes == 30
    assert s.ot_tier2_minutes == 0
    assert s.ot_remainder_minutes == 0


def test_90_min_overtime_all_tier1():
    # 9h 30min → 90 min tier-1
    records = [_make("clock_in", 9), _make("clock_out", 18, 30)]
    s = compute_daily_summary(date(2026, 4, 1), records)
    assert s.ot_tier1_minutes == 90
    assert s.ot_tier2_minutes == 0
    assert s.ot_counted_minutes == 90


def test_tier1_full_and_tier2_starts():
    # 10h 30min → 120 min tier-1 + 30 min tier-2
    records = [_make("clock_in", 9), _make("clock_out", 19, 30)]
    s = compute_daily_summary(date(2026, 4, 1), records)
    assert s.ot_tier1_minutes == 120
    assert s.ot_tier2_minutes == 30
    assert s.ot_counted_minutes == 150


def test_remainder_dropped_when_tier2_partially_filled():
    # 10h 45min → ot_raw=165, ot_counted=150 (150=120t1+30t2), remainder=15
    records = [_make("clock_in", 9), _make("clock_out", 19, 45)]
    s = compute_daily_summary(date(2026, 4, 1), records)
    assert s.ot_tier1_minutes == 120
    assert s.ot_tier2_minutes == 30
    assert s.ot_counted_minutes == 150
    assert s.ot_remainder_minutes == 15


def test_exceeds_legal_limit():
    # 12h 30min → OT raw=270 > 240 (4h)
    records = [_make("clock_in", 9), _make("clock_out", 21, 30)]
    s = compute_daily_summary(date(2026, 4, 1), records)
    assert s.exceeds_legal_limit


def test_exactly_12_hours_not_exceeded():
    # 12h → OT raw=240 == limit, not exceeded
    records = [_make("clock_in", 9), _make("clock_out", 21)]
    s = compute_daily_summary(date(2026, 4, 1), records)
    assert not s.exceeds_legal_limit


def test_multiple_punches_uses_first_in_last_out():
    # Extra clock_in at 10h should be ignored; clock_out at 19h used
    records = [
        _make("clock_in", 9),
        _make("clock_in", 10),
        _make("clock_out", 18),
        _make("clock_out", 19),
    ]
    s = compute_daily_summary(date(2026, 4, 1), records)
    assert s.work_minutes == 600  # 9:00 → 19:00


# ── compute_monthly_summaries ──────────────────────────────────────────────────

def test_monthly_groups_by_date():
    records = [
        _make("clock_in",  9,  0, day=1),
        _make("clock_out", 18, 0, day=1),
        _make("clock_in",  8, 30, day=2),
        _make("clock_out", 17, 30, day=2),
    ]
    summaries = compute_monthly_summaries(records, TZ)
    assert len(summaries) == 2
    # sorted descending
    assert summaries[0].date > summaries[1].date


def test_monthly_total():
    records = [
        _make("clock_in",  9,  0, day=1),
        _make("clock_out", 18, 30, day=1),  # 90 min OT (9h30m - 8h)
        _make("clock_in",  9,  0, day=2),
        _make("clock_out", 19,  0, day=2),  # 120 min OT (10h - 8h)
    ]
    summaries = compute_monthly_summaries(records, TZ)
    assert monthly_ot_total(summaries) == 210


def test_monthly_ot_limit_constant():
    assert MONTHLY_OT_LIMIT == 2760  # 46 h × 60
