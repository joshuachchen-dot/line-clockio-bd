from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, contains_eager

from app.models.check_in import CheckIn
from app.models.employee import Employee


def build_checkin_query(
    db: Session,
    tz: ZoneInfo,
    employee_id: int | None,
    date_from: str | None,
    date_to: str | None,
):
    """Filtered CheckIn query joined to Employee — shared by dashboard, jobs, and exports."""
    query = (
        db.query(CheckIn)
        .join(CheckIn.employee)
        .filter(Employee.is_active.is_(True))
        .options(contains_eager(CheckIn.employee))
    )
    if employee_id:
        query = query.filter(CheckIn.employee_id == employee_id)
    if date_from:
        try:
            dt_from = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=tz)
            query = query.filter(CheckIn.checked_at >= dt_from)
        except ValueError:
            pass
    if date_to:
        try:
            dt_to = datetime.strptime(date_to, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=tz
            )
            query = query.filter(CheckIn.checked_at <= dt_to)
        except ValueError:
            pass
    return query
