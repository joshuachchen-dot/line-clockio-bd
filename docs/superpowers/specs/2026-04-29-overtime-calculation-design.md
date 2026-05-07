# Overtime Calculation — Design Spec

**Date:** 2026-04-29
**Status:** Approved
**Scope:** Calculation layer only. Overtime pay request/approval flow deferred pending HR meeting.

---

## Overview

Calculate daily work hours and overtime from existing `clock_in` / `clock_out` records based on Taiwan's Labor Standards Act (勞基法). Display results in the LIFF 出勤紀錄 page. No new DB tables in this phase.

---

## Calculation Rules (勞基法)

All 勞基法 parameters are defined as named constants in `app/services/overtime.py`. If the law changes, only that file needs updating.

### Daily work hours
- **Pairing:** first `clock_in` + last `clock_out` of the day
- **Break deduction:** none — actual punch times are used as-is (employee responsibility)
- **In-progress:** if no `clock_out` exists for today, mark `in_progress=True`; no overtime calculated

### Overtime tiers

| Tier | Minutes worked | 勞基法 article | Pay multiplier |
|------|---------------|----------------|----------------|
| Regular | 0 – 480 min (8 h) | Art. 30 | 1× |
| Tier 1 OT | 481 – 600 min (next 2 h) | Art. 24, item 1 | 4/3× |
| Tier 2 OT | 601 – 720 min (next 2 h) | Art. 24, item 2 | 5/3× |
| Illegal excess | > 720 min (> 12 h) | Art. 32 violated | — |

### 30-minute unit rule
Overtime is counted in 30-minute increments. Fractions under 30 minutes are not counted.

```
ot_raw      = work_minutes - 480          # total overtime minutes before rounding
ot_counted  = (ot_raw // 30) * 30        # floor to nearest 30-min block
```

Examples:
- 8 h 20 min → `ot_raw=20` → `ot_counted=0` (no overtime)
- 8 h 30 min → `ot_raw=30` → `ot_counted=30` (30 min tier 1)
- 9 h 45 min → `ot_raw=105` → `ot_counted=90` (90 min tier 1)
- 10 h 30 min → `ot_raw=150` → `ot_counted=150` (120 min tier 1 + 30 min tier 2)

### Monthly limit
- Legal cap: **46 hours (2,760 minutes)** per month
- Flag `exceeds_monthly_limit=True` when exceeded

---

## Architecture

### `app/services/overtime.py` (new file)
Pure functions — no DB dependency, independently testable.

**Constants block:**
```python
REGULAR_WORK_MINUTES = 480       # 正常工時 8 小時
OT_UNIT_MINUTES = 30             # 最小加班單位（勞基法）
OT_TIER1_CAP_MINUTES = 120       # 加班第一段上限（2 小時）
OT_TIER2_CAP_MINUTES = 120       # 加班第二段上限（2 小時）
DAILY_OT_LEGAL_MAX = 240         # 每日加班法定上限（4 小時）
MONTHLY_OT_LIMIT = 2760          # 月加班上限（46 小時）
```

**`DailyWorkSummary` dataclass:**
```python
@dataclass
class DailyWorkSummary:
    date: date
    clock_in: datetime | None
    clock_out: datetime | None
    work_minutes: int              # actual total minutes worked
    regular_minutes: int           # capped at 480
    ot_tier1_minutes: int          # counted tier 1 OT (0–120)
    ot_tier2_minutes: int          # counted tier 2 OT (0–120)
    ot_counted_minutes: int        # ot_tier1 + ot_tier2 (already rounded)
    ot_remainder_minutes: int      # fraction dropped by 30-min rule
    in_progress: bool              # clocked in, not yet out
    exceeds_legal_limit: bool      # work > 12 h
```

**Public functions:**
- `compute_daily_summary(date, check_ins) -> DailyWorkSummary`
- `compute_monthly_summaries(year, month, check_ins) -> list[DailyWorkSummary]`
- `monthly_ot_total(summaries) -> int` — total counted OT minutes for the month

### `app/routers/liff.py` — `/liff/records` endpoint
No calculation logic here. Calls `compute_monthly_summaries()`, serialises the result.

**Response shape:**
```json
{
  "month": "2026-04",
  "total_ot_counted_minutes": 210,
  "exceeds_monthly_limit": false,
  "records": [
    {
      "date": "2026-04-29",
      "weekday": "二",
      "clock_in": "08:45",
      "clock_out": "19:12",
      "work_minutes": 627,
      "regular_minutes": 480,
      "ot_tier1_minutes": 120,
      "ot_tier2_minutes": 27,
      "ot_counted_minutes": 120,
      "ot_remainder_minutes": 27,
      "in_progress": false,
      "exceeds_legal_limit": false
    }
  ]
}
```

---

## Frontend (LIFF 出勤紀錄)

Display logic per row (no new UI components — reuse existing card style):

| Condition | Display |
|-----------|---------|
| `work_minutes ≤ 480` | Clock times only, no OT tag |
| `ot_counted_minutes > 0` | Orange tag: 加班 Xh Ym |
| `exceeds_legal_limit` | Red warning: 超過法定上限 |
| `in_progress` | Show "上班中", hide work hours |

Monthly summary banner at top of list:
- 本月加班累計 X 小時 Y 分
- If `exceeds_monthly_limit`: red warning 已超過月加班上限（46 小時）

---

## Testing

- Unit tests for `overtime.py` covering: no OT, exact 30-min boundary, tier1/tier2 split, exceeds limit, in-progress, monthly total
- Integration test: `/liff/records` response includes OT fields

---

## Out of Scope (this phase)

- Overtime pay calculation (requires hourly rate — not in Employee model yet)
- Overtime pay request / approval flow (pending HR meeting)
- Rest day (休息日) and holiday (例假日) overtime multipliers
