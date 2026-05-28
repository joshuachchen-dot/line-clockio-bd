"""Tests for app/routers/jobs.py — internal Cloud Scheduler job endpoints."""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models.check_in import CheckIn, CheckInType
from app.models.employee import Employee


# ── Helpers ───────────────────────────────────────────────────────────────────

_SECRET = "test-internal-secret"
_AUTH_HEADER = {"X-Internal-Secret": _SECRET}


def _mock_settings(secret: str = _SECRET) -> MagicMock:
    s = MagicMock()
    s.timezone = "Asia/Taipei"
    s.factory_machine_id = "0000000005"
    s.ftp_host = "61.219.81.20"
    s.ftp_user = "lineta"
    s.ftp_password = "Ta12943883"
    s.ftp_remote_dir = "/"
    s.internal_secret = secret
    return s


def _add_employee(db, card: str | None = "A1234567") -> Employee:
    emp = Employee(
        email="worker@aiotek.com.tw",
        line_user_id="Uworker",
        display_name="Worker",
        card_number=card,
        is_active=True,
    )
    db.add(emp)
    db.commit()
    db.refresh(emp)
    return emp


def _add_checkin(db, employee_id: int, ctype: CheckInType, checked_at: datetime) -> CheckIn:
    ci = CheckIn(
        employee_id=employee_id,
        type=ctype,
        latitude=25.0,
        longitude=121.0,
        ip_address="127.0.0.1",
    )
    db.add(ci)
    db.flush()
    ci.checked_at = checked_at
    db.commit()
    db.refresh(ci)
    return ci


# ── Auth ──────────────────────────────────────────────────────────────────────

def test_job_missing_auth_returns_401(client):
    resp = client.post("/internal/jobs/factory_export")
    assert resp.status_code == 401


def test_job_wrong_secret_returns_401(client):
    with patch("app.routers.jobs.get_settings", return_value=_mock_settings()):
        resp = client.post(
            "/internal/jobs/factory_export",
            headers={"X-Internal-Secret": "wrong-secret"},
        )
    assert resp.status_code == 401


def test_job_no_secret_configured_returns_500(client):
    settings = _mock_settings(secret="")
    with patch("app.routers.jobs.get_settings", return_value=settings):
        resp = client.post(
            "/internal/jobs/factory_export",
            headers=_AUTH_HEADER,
        )
    assert resp.status_code == 500


# ── Success path ──────────────────────────────────────────────────────────────

def test_job_uploads_yesterdays_records(client, db):
    """Job queries yesterday's records and calls upload_factory_file."""
    emp = _add_employee(db)
    # Yesterday at 09:00 UTC+8 = 01:00 UTC
    yesterday_utc = datetime(2026, 5, 4, 1, 0, 0, tzinfo=timezone.utc)
    _add_checkin(db, emp.id, CheckInType.clock_in, yesterday_utc)

    settings = _mock_settings()
    with patch("app.routers.jobs.get_settings", return_value=settings), \
         patch("app.routers.jobs.upload_factory_file") as mock_upload, \
         patch("app.routers.jobs.datetime") as mock_dt:
        # Pin "now" to 2026-05-05 23:30 Asia/Taipei so yesterday = 2026-05-04
        from zoneinfo import ZoneInfo
        mock_dt.now.return_value = datetime(2026, 5, 5, 23, 30, 0,
                                            tzinfo=ZoneInfo("Asia/Taipei"))
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        resp = client.post(
            "/internal/jobs/factory_export",
            headers=_AUTH_HEADER,
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["records"] == 1
    assert "20260504" in body["filename"]
    mock_upload.assert_called_once()


def test_job_excludes_employees_without_card(client, db):
    """Employees without a card number are excluded from the FTP file."""
    emp = _add_employee(db, card=None)
    yesterday_utc = datetime(2026, 5, 4, 1, 0, 0, tzinfo=timezone.utc)
    _add_checkin(db, emp.id, CheckInType.clock_in, yesterday_utc)

    settings = _mock_settings()
    with patch("app.routers.jobs.get_settings", return_value=settings), \
         patch("app.routers.jobs.upload_factory_file") as mock_upload, \
         patch("app.routers.jobs.datetime") as mock_dt:
        from zoneinfo import ZoneInfo
        mock_dt.now.return_value = datetime(2026, 5, 5, 23, 30, 0,
                                            tzinfo=ZoneInfo("Asia/Taipei"))
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        resp = client.post(
            "/internal/jobs/factory_export",
            headers=_AUTH_HEADER,
        )

    assert resp.status_code == 200
    assert resp.json()["records"] == 0
    mock_upload.assert_called_once()
    # Empty file still uploaded (factory system expects a file each day)
    _, kwargs = mock_upload.call_args
    assert mock_upload.call_args[1]["content"] == b"" or mock_upload.call_args.kwargs.get("content") == b""


def test_job_ftp_failure_returns_502(client, db):
    """FTP upload error returns 502 so Cloud Scheduler retries."""
    db.execute(__import__("sqlalchemy").text("SELECT 1"))
    settings = _mock_settings()
    with patch("app.routers.jobs.get_settings", return_value=settings), \
         patch("app.routers.jobs.upload_factory_file", side_effect=OSError("connection refused")), \
         patch("app.routers.jobs.datetime") as mock_dt:
        from zoneinfo import ZoneInfo
        mock_dt.now.return_value = datetime(2026, 5, 5, 23, 30, 0,
                                            tzinfo=ZoneInfo("Asia/Taipei"))
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        resp = client.post(
            "/internal/jobs/factory_export",
            headers=_AUTH_HEADER,
        )

    assert resp.status_code == 502
