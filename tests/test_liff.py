"""Tests for app/routers/liff.py — page serving and Pydantic model validation."""

import pytest
from datetime import datetime, timedelta, timezone
from pydantic import ValidationError
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.check_in import CheckIn, CheckInType
from app.models.employee import Employee
from app.models.makeup_request import MakeupRequest, MakeupRequestStatus
from app.routers.liff import CheckInRequest

LINE_UID = "Uabc1234567890abcdef"


def _add_employee(db, display_name: str = "Alice") -> Employee:
    emp = Employee(
        email="alice@aiotek.com.tw",
        line_user_id=LINE_UID,
        display_name=display_name,
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
    # Override the server-default timestamp
    ci.checked_at = checked_at
    db.commit()
    db.refresh(ci)
    return ci


# ── GET /liff/ page ───────────────────────────────────────────────────────────

def test_liff_page_returns_200(client):
    """LIFF page is served with HTTP 200."""
    resp = client.get("/liff/")
    assert resp.status_code == 200


def test_liff_page_is_html(client):
    """Response Content-Type is text/html."""
    resp = client.get("/liff/")
    assert "text/html" in resp.headers["content-type"]


def test_liff_page_contains_liff_id(client):
    """The stub LIFF ID from conftest is injected into the page."""
    resp = client.get("/liff/")
    assert "test-liff-id" in resp.text


def test_liff_page_contains_api_url(client):
    """The APP_BASE_URL is injected so the JS fetch target is correct."""
    resp = client.get("/liff/")
    assert "http://localhost:8000" in resp.text


def test_liff_page_contains_checkin_buttons(client):
    """Both clock-in and clock-out buttons are present."""
    resp = client.get("/liff/")
    assert "上班打卡" in resp.text
    assert "下班打卡" in resp.text


def test_liff_page_loads_liff_sdk(client):
    """LIFF SDK script tag is present."""
    resp = client.get("/liff/")
    assert "line-scdn.net/liff" in resp.text


# ── POST /liff/* — liff_enabled guard (shared dependency) ────────────────────

def _disabled_settings():
    from app.config import Settings
    s = MagicMock(spec=Settings)
    s.liff_enabled = False
    return s


def test_checkin_503_when_liff_not_configured(client):
    """POST /liff/checkin returns 503 when LIFF credentials are not set."""
    with patch("app.routers.liff.get_settings", return_value=_disabled_settings()):
        resp = client.post(
            "/liff/checkin",
            json={"type": "clock_in", "latitude": 25.0, "longitude": 121.0, "id_token": "tok"},
        )
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"]


def test_status_503_when_liff_not_configured(client):
    """POST /liff/status returns 503 when LIFF credentials are not set."""
    with patch("app.routers.liff.get_settings", return_value=_disabled_settings()):
        resp = client.post("/liff/status", json={"id_token": "tok"})
    assert resp.status_code == 503


def test_records_503_when_liff_not_configured(client):
    """POST /liff/records returns 503 when LIFF credentials are not set."""
    with patch("app.routers.liff.get_settings", return_value=_disabled_settings()):
        resp = client.post("/liff/records", json={"id_token": "tok"})
    assert resp.status_code == 503


# ── CheckInRequest field bounds ───────────────────────────────────────────────

def test_valid_clock_in():
    req = CheckInRequest(type="clock_in", latitude=25.033, longitude=121.565, id_token="tok")
    assert req.type == "clock_in"


def test_valid_clock_out_extreme_coords():
    req = CheckInRequest(type="clock_out", latitude=-90.0, longitude=180.0, id_token="tok")
    assert req.type == "clock_out"


def test_latitude_above_max_rejected():
    with pytest.raises(ValidationError):
        CheckInRequest(type="clock_in", latitude=90.001, longitude=0.0, id_token="tok")


def test_latitude_below_min_rejected():
    with pytest.raises(ValidationError):
        CheckInRequest(type="clock_in", latitude=-90.001, longitude=0.0, id_token="tok")


def test_longitude_above_max_rejected():
    with pytest.raises(ValidationError):
        CheckInRequest(type="clock_in", latitude=0.0, longitude=180.001, id_token="tok")


def test_longitude_below_min_rejected():
    with pytest.raises(ValidationError):
        CheckInRequest(type="clock_in", latitude=0.0, longitude=-180.001, id_token="tok")


# ── POST /liff/status ─────────────────────────────────────────────────────────

def _mock_settings_liff(tz: str = "Asia/Taipei") -> MagicMock:
    s = MagicMock()
    s.timezone = tz
    s.liff_channel_id = "test-liff-channel-id"
    s.liff_enabled = True
    return s


def test_status_returns_display_name(client, db):
    """Status returns display_name when employee is bound."""
    emp = _add_employee(db, display_name="Alice")
    settings = _mock_settings_liff()

    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/status", json={"id_token": "tok"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "Alice"
    assert body["clock_in_time"] is None
    assert body["clock_out_time"] is None


def test_status_shows_todays_clock_in(client, db):
    """Status returns today's clock-in time when a record exists."""
    emp = _add_employee(db)
    now_utc = datetime.now(timezone.utc).replace(hour=1, minute=0, second=0, microsecond=0)
    _add_checkin(db, emp.id, CheckInType.clock_in, now_utc)
    settings = _mock_settings_liff()

    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/status", json={"id_token": "tok"})

    assert resp.status_code == 200
    assert resp.json()["clock_in_time"] is not None


def test_status_403_for_unbound_user(db):
    """_get_employee raises 403 when LINE user has no active employee record."""
    from fastapi import HTTPException
    from app.routers.liff import _get_employee

    with pytest.raises(HTTPException) as exc:
        _get_employee(db, "nonexistent_uid")
    assert exc.value.status_code == 403


# ── POST /liff/records ────────────────────────────────────────────────────────

def test_records_returns_month_label(client, db):
    """Records endpoint returns a month label."""
    _add_employee(db)
    settings = _mock_settings_liff()

    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/records", json={"id_token": "tok"})

    assert resp.status_code == 200
    body = resp.json()
    assert "月" in body["month"]
    assert isinstance(body["records"], list)


def test_records_includes_checkin_entries(client, db):
    """Records returns daily summaries with overtime fields."""
    emp = _add_employee(db)
    now_utc = datetime.now(timezone.utc)
    _add_checkin(db, emp.id, CheckInType.clock_in, now_utc)
    settings = _mock_settings_liff()

    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/records", json={"id_token": "tok"})

    assert resp.status_code == 200
    body = resp.json()
    assert "total_ot_counted_minutes" in body
    assert "exceeds_monthly_limit" in body
    records = body["records"]
    assert len(records) == 1
    r = records[0]
    assert "date" in r
    assert "weekday" in r
    assert "clock_in" in r
    assert "clock_out" in r
    assert "ot_counted_minutes" in r
    assert r["in_progress"] is True   # only clock_in, no clock_out


def test_records_403_for_unbound_user(db):
    """_get_employee raises 403 when LINE user has no active employee record (shared with status test)."""
    from fastapi import HTTPException
    from app.routers.liff import _get_employee

    with pytest.raises(HTTPException) as exc:
        _get_employee(db, "ghost_uid")
    assert exc.value.status_code == 403


# ── POST /liff/status — is_manager field ──────────────────────────────────────

def test_status_returns_is_manager_false_for_regular_employee(client, db):
    """Status returns is_manager=False for a non-manager employee."""
    _add_employee(db)
    settings = _mock_settings_liff()

    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/status", json={"id_token": "tok"})

    assert resp.status_code == 200
    assert resp.json()["is_manager"] is False
    assert resp.json()["pending_makeup_count"] == 0


def test_status_returns_is_manager_true_and_pending_count(client, db):
    """Status returns is_manager=True and correct pending_makeup_count for managers."""
    emp = _add_employee(db)
    emp.is_manager = True
    db.commit()

    # Add a pending makeup request
    req = MakeupRequest(
        employee_id=emp.id,
        type=CheckInType.clock_in,
        requested_at=datetime.now(timezone.utc) - timedelta(hours=3),
        reason="忘記打卡",
        status=MakeupRequestStatus.pending,
    )
    db.add(req)
    db.commit()
    db.refresh(req)  # anchors session connection before endpoint call

    settings = _mock_settings_liff()

    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/status", json={"id_token": "tok"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_manager"] is True
    assert body["pending_makeup_count"] == 1


# ── POST /liff/makeup/request ─────────────────────────────────────────────────

def test_makeup_request_success(client, db):
    """Employee can submit a makeup punch request for a past time."""
    _add_employee(db)
    settings = _mock_settings_liff()
    past_time = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()

    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/makeup/request", json={
            "id_token": "tok",
            "type": "clock_in",
            "requested_at": past_time,
            "reason": "忘記打卡",
        })

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert db.query(MakeupRequest).count() == 1


def test_makeup_request_rejects_future_time(client, db):
    """Makeup request for a future time is rejected with 400."""
    _add_employee(db)
    settings = _mock_settings_liff()
    future_time = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()

    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/makeup/request", json={
            "id_token": "tok",
            "type": "clock_in",
            "requested_at": future_time,
            "reason": "test",
        })

    assert resp.status_code == 400


def test_makeup_request_rejects_naive_datetime(client, db):
    """Makeup request with a naive (timezone-unaware) requested_at is rejected with 422."""
    _add_employee(db)
    settings = _mock_settings_liff()
    # Send a datetime string without any timezone offset
    naive_time = "2026-04-01T09:00:00"

    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/makeup/request", json={
            "id_token": "tok",
            "type": "clock_in",
            "requested_at": naive_time,
            "reason": "test",
        })

    assert resp.status_code == 422
    assert "timezone" in resp.json()["detail"].lower()


def test_makeup_request_rejects_invalid_type(client, db):
    """Makeup request with an unrecognised type returns 400."""
    _add_employee(db)
    settings = _mock_settings_liff()
    past_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/makeup/request", json={
            "id_token": "tok",
            "type": "invalid_type",
            "requested_at": past_time,
            "reason": "test",
        })

    assert resp.status_code == 400
    assert "Invalid type" in resp.json()["detail"]


def test_makeup_request_rejects_duplicate_pending(client, db):
    """Second makeup request for the same slot while first is still pending returns 409."""
    emp = _add_employee(db)
    settings = _mock_settings_liff()
    past_time = datetime.now(timezone.utc) - timedelta(hours=3)
    past_time_iso = past_time.isoformat()

    # Insert an existing pending request for the same slot
    existing = MakeupRequest(
        employee_id=emp.id,
        type=CheckInType.clock_in,
        requested_at=past_time,
        reason="第一次申請",
        status=MakeupRequestStatus.pending,
    )
    db.add(existing)
    db.commit()
    db.refresh(existing)

    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/makeup/request", json={
            "id_token": "tok",
            "type": "clock_in",
            "requested_at": past_time_iso,
            "reason": "重複申請",
        })

    assert resp.status_code == 409


# ── POST /liff/makeup/pending ─────────────────────────────────────────────────

def test_makeup_pending_requires_manager(client, db):
    """Non-manager employees cannot access the pending list."""
    _add_employee(db)
    settings = _mock_settings_liff()

    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/makeup/pending", json={"id_token": "tok"})

    assert resp.status_code == 403


def test_makeup_pending_returns_pending_requests(client, db):
    """Manager sees all pending makeup requests."""
    emp = _add_employee(db)
    emp.is_manager = True
    db.commit()

    req = MakeupRequest(
        employee_id=emp.id,
        type=CheckInType.clock_in,
        requested_at=datetime.now(timezone.utc) - timedelta(hours=3),
        reason="測試原因",
        status=MakeupRequestStatus.pending,
    )
    db.add(req)
    db.commit()
    db.refresh(req)  # anchors session connection before endpoint call

    settings = _mock_settings_liff()

    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/makeup/pending", json={"id_token": "tok"})

    assert resp.status_code == 200
    reqs = resp.json()["requests"]
    assert len(reqs) == 1
    assert reqs[0]["type"] == "clock_in"
    assert reqs[0]["reason"] == "測試原因"


# ── POST /liff/makeup/review ──────────────────────────────────────────────────

def test_makeup_review_approve_creates_checkin(client, db):
    """Approving a makeup request inserts a CheckIn record."""
    emp = _add_employee(db)
    emp.is_manager = True
    db.commit()

    req = MakeupRequest(
        employee_id=emp.id,
        type=CheckInType.clock_in,
        requested_at=datetime.now(timezone.utc) - timedelta(hours=3),
        reason="忘記打卡",
        status=MakeupRequestStatus.pending,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    req_id = req.id

    settings = _mock_settings_liff()

    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/makeup/review", json={
            "id_token": "tok",
            "request_id": req_id,
            "action": "approve",
        })

    assert resp.status_code == 200
    assert resp.json()["success"] is True

    db.expire_all()
    updated = db.query(MakeupRequest).filter_by(id=req_id).first()
    assert updated.status == MakeupRequestStatus.approved
    assert updated.reviewed_by == emp.id

    checkin = db.query(CheckIn).filter_by(employee_id=emp.id).first()
    assert checkin is not None
    assert checkin.ip_address == "makeup:approved"


def test_makeup_review_reject_does_not_create_checkin(client, db):
    """Rejecting a makeup request does not insert a CheckIn record."""
    emp = _add_employee(db)
    emp.is_manager = True
    db.commit()

    req = MakeupRequest(
        employee_id=emp.id,
        type=CheckInType.clock_out,
        requested_at=datetime.now(timezone.utc) - timedelta(hours=1),
        reason="忘記打卡",
        status=MakeupRequestStatus.pending,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    req_id = req.id

    settings = _mock_settings_liff()

    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/makeup/review", json={
            "id_token": "tok",
            "request_id": req_id,
            "action": "reject",
        })

    assert resp.status_code == 200
    db.expire_all()
    updated = db.query(MakeupRequest).filter_by(id=req_id).first()
    assert updated.status == MakeupRequestStatus.rejected
    assert db.query(CheckIn).count() == 0


def test_makeup_review_requires_manager(client, db):
    """Non-manager cannot review makeup requests."""
    _add_employee(db)
    settings = _mock_settings_liff()

    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/makeup/review", json={
            "id_token": "tok",
            "request_id": 1,
            "action": "approve",
        })

    assert resp.status_code == 403


def test_makeup_review_concurrent_approve_returns_409(client, db):
    """Second approval of the same request returns 409 and creates only one CheckIn."""
    emp = _add_employee(db)
    emp.is_manager = True
    db.commit()

    req = MakeupRequest(
        employee_id=emp.id,
        type=CheckInType.clock_in,
        requested_at=datetime.now(timezone.utc) - timedelta(hours=3),
        reason="忘記打卡",
        status=MakeupRequestStatus.pending,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    req_id = req.id

    settings = _mock_settings_liff()

    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp1 = client.post("/liff/makeup/review", json={
            "id_token": "tok", "request_id": req_id, "action": "approve",
        })
        assert resp1.status_code == 200

        # Re-anchor the SQLite in-memory session before the second call
        db.expire_all()
        _ = db.query(MakeupRequest).filter_by(id=req_id).first()

        # Simulate second concurrent reviewer hitting the same request.
        # Sequential test: pre-fetch finds status=approved → 404.
        # True concurrent race: atomic UPDATE returns 0 → 409.
        # Either way the request must NOT produce a second CheckIn.
        resp2 = client.post("/liff/makeup/review", json={
            "id_token": "tok", "request_id": req_id, "action": "approve",
        })
        assert resp2.status_code in (404, 409)

    # Exactly one CheckIn was created despite two approve attempts
    db.expire_all()
    assert db.query(CheckIn).filter_by(employee_id=emp.id).count() == 1


# ── POST /liff/checkin — clock-out guard ──────────────────────────────────────

def test_checkin_clock_out_without_clock_in_returns_422(client, db):
    """Clock-out is blocked with 422 when no clock-in exists for today."""
    _add_employee(db)
    settings = _mock_settings_liff()

    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/checkin", json={
            "type": "clock_out",
            "latitude": 25.0,
            "longitude": 121.0,
            "id_token": "tok",
        })

    assert resp.status_code == 422
    assert "上班打卡" in resp.json()["detail"]


# ── POST /liff/update_card ────────────────────────────────────────────────────

def test_update_card_sets_card_number(client, db):
    """Employee can set their card number via LIFF."""
    _add_employee(db)
    settings = _mock_settings_liff()

    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/update_card", json={"id_token": "tok", "card_number": "A1234567"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["card_number"] == "A1234567"

    emp = db.query(Employee).filter(Employee.line_user_id == LINE_UID).first()
    assert emp.card_number == "A1234567"


def test_update_card_uppercases_input(client, db):
    """Card number is stored uppercase regardless of input case."""
    _add_employee(db)
    settings = _mock_settings_liff()

    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/update_card", json={"id_token": "tok", "card_number": "ab123456"})

    assert resp.status_code == 200
    assert resp.json()["card_number"] == "AB123456"


def test_update_card_conflict_returns_409(client, db):
    """Card number already held by another employee returns 409."""
    _add_employee(db)
    # A second employee already holds the target card number
    other = Employee(
        email="other@aiotek.com.tw",
        line_user_id="Uother",
        card_number="TAKEN123",
        is_active=True,
    )
    db.add(other)
    db.flush()  # keep session transaction open so the same connection is reused

    settings = _mock_settings_liff()
    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/update_card", json={"id_token": "tok", "card_number": "TAKEN123"})

    assert resp.status_code == 409
    assert "已被其他員工使用" in resp.json()["detail"]


def test_update_card_self_update_allowed(client, db):
    """Employee can re-submit the same card number they already own (idempotent)."""
    emp = _add_employee(db)
    emp.card_number = "MINE1234"
    db.flush()  # keep session transaction open so the same connection is reused

    settings = _mock_settings_liff()
    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/update_card", json={"id_token": "tok", "card_number": "MINE1234"})

    assert resp.status_code == 200
    assert resp.json()["card_number"] == "MINE1234"


def test_update_card_invalid_format_rejected(client, db):
    """Card numbers that don't match 8 alphanumeric chars are rejected by Pydantic."""
    _add_employee(db)
    settings = _mock_settings_liff()

    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/update_card", json={"id_token": "tok", "card_number": "SHORT"})

    assert resp.status_code == 422


def test_update_card_status_includes_card_number(client, db):
    """After setting card number, /liff/status reflects it."""
    emp = _add_employee(db)
    emp.card_number = "CARD1234"
    db.flush()  # keep session transaction open so the same connection is reused

    settings = _mock_settings_liff()
    with patch("app.routers.liff.get_settings", return_value=settings), \
         patch("app.routers.liff._verify_line_token", new_callable=AsyncMock, return_value=LINE_UID):
        resp = client.post("/liff/status", json={"id_token": "tok"})

    assert resp.status_code == 200
    assert resp.json()["card_number"] == "CARD1234"
