"""Tests for app/routers/webhook.py."""

import hashlib
import hmac
from base64 import b64encode
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.check_in import CheckIn, CheckInType
from app.models.email_verification import EmailVerification
from app.models.employee import Employee
from app.routers.webhook import (
    _handle_card_number,
    _handle_email_submission,
    _handle_follow,
    _handle_otp_verification,
    _handle_query,
    _hash_otp,
    _verify_signature,
)

LINE_UID = "Uabc1234567890abcdef"
EMAIL = "alice@aiotek.com.tw"
TOKEN = "reply-token"


def _make_sig(body: bytes, secret: str) -> str:
    return b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def _add_otp(
    db, line_user_id: str, email: str, code: str = "123456", failed_attempts: int = 0
) -> EmailVerification:
    ev = EmailVerification(
        line_user_id=line_user_id,
        email=email,
        otp_code=_hash_otp(code, line_user_id),  # store hash, matching production behaviour
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        used=False,
        failed_attempts=failed_attempts,
    )
    db.add(ev)
    db.commit()
    return ev


def _mock_settings(tz: str = "Asia/Taipei") -> MagicMock:
    s = MagicMock()
    s.timezone = tz
    return s


# ── _verify_signature ─────────────────────────────────────────────────────────

def test_verify_sig_valid():
    body = b'{"events":[]}'
    secret = "channel-secret"
    assert _verify_signature(body, _make_sig(body, secret), secret) is True


def test_verify_sig_wrong_signature():
    assert _verify_signature(b'body', "wrong==", "channel-secret") is False


def test_verify_sig_tampered_body():
    secret = "channel-secret"
    sig = _make_sig(b'original-body', secret)
    assert _verify_signature(b'tampered-body', sig, secret) is False


# ── _handle_email_submission ──────────────────────────────────────────────────

async def test_email_already_bound(db):
    """LINE UID already bound to an employee → no OTP issued."""
    db.add(Employee(email=EMAIL, line_user_id=LINE_UID))
    db.commit()

    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply, \
         patch("app.routers.webhook.send_otp_email", new_callable=AsyncMock) as mock_send:
        await _handle_email_submission(db, LINE_UID, EMAIL, TOKEN)

    mock_send.assert_not_called()
    assert "已完成綁定" in mock_reply.call_args[0][1]


async def test_email_bound_to_different_uid(db):
    """Email already bound to a different LINE UID → conflict reply, no OTP."""
    db.add(Employee(email=EMAIL, line_user_id="U-someone-else"))
    db.commit()

    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply, \
         patch("app.routers.webhook.send_otp_email", new_callable=AsyncMock) as mock_send:
        await _handle_email_submission(db, LINE_UID, EMAIL, TOKEN)

    mock_send.assert_not_called()
    assert "已被其他帳號綁定" in mock_reply.call_args[0][1]


async def test_email_submission_otp_sent(db):
    """Unbound email → OTP row written and email dispatched."""
    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply, \
         patch("app.routers.webhook.send_otp_email", new_callable=AsyncMock, return_value=True) as mock_send:
        await _handle_email_submission(db, LINE_UID, EMAIL, TOKEN)

    mock_send.assert_called_once()
    assert mock_send.call_args[0][0] == EMAIL
    assert "驗證碼傳送至" in mock_reply.call_args[0][1]


async def test_email_submission_mailgun_failure(db):
    """Mailgun request fails → failure reply sent; OTP row still persisted."""
    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply, \
         patch("app.routers.webhook.send_otp_email", new_callable=AsyncMock, return_value=False):
        await _handle_email_submission(db, LINE_UID, EMAIL, TOKEN)

    assert "傳送失敗" in mock_reply.call_args[0][1]


async def test_email_wrong_domain_rejected(db):
    """Email with a non-company domain is rejected before any OTP is issued."""
    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply, \
         patch("app.routers.webhook.send_otp_email", new_callable=AsyncMock) as mock_send:
        await _handle_email_submission(db, LINE_UID, "alice@gmail.com", TOKEN)

    mock_send.assert_not_called()
    assert "@aiotek.com.tw" in mock_reply.call_args[0][1]


# ── _handle_otp_verification ──────────────────────────────────────────────────

async def test_otp_no_matching_record(db):
    """No matching (unused, unexpired) OTP → invalid reply."""
    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply, \
         patch("app.routers.webhook._get_line_display_name", new_callable=AsyncMock, return_value=None):
        await _handle_otp_verification(db, LINE_UID, "000000", TOKEN)

    assert "驗證碼無效" in mock_reply.call_args[0][1]


async def test_otp_hr_initiated_binds_employee(db):
    """HR-pre-loaded employee (no line_user_id) is bound on OTP success."""
    db.add(Employee(email=EMAIL, line_user_id=None))
    db.commit()
    _add_otp(db, LINE_UID, EMAIL)

    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply, \
         patch("app.routers.webhook._get_line_display_name", new_callable=AsyncMock, return_value="Alice"):
        await _handle_otp_verification(db, LINE_UID, "123456", TOKEN)

    assert "綁定完成" in mock_reply.call_args[0][1]
    emp = db.query(Employee).filter(Employee.email == EMAIL).first()
    assert emp.line_user_id == LINE_UID


async def test_otp_employee_initiated_creates_record(db):
    """No pre-existing employee → new Employee row created during verification."""
    _add_otp(db, LINE_UID, EMAIL)

    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply, \
         patch("app.routers.webhook._get_line_display_name", new_callable=AsyncMock, return_value=None):
        await _handle_otp_verification(db, LINE_UID, "123456", TOKEN)

    assert "綁定完成" in mock_reply.call_args[0][1]
    emp = db.query(Employee).filter(Employee.line_user_id == LINE_UID).first()
    assert emp is not None
    assert emp.email == EMAIL


async def test_otp_race_condition_blocks_second_uid(db):
    """Race: another UID bound the email between OTP issue and verify.

    The losing UID must be rejected AND the OTP must be marked used
    to prevent replay attacks.
    """
    other_uid = "U-raced-ahead"
    db.add(Employee(email=EMAIL, line_user_id=other_uid))
    db.commit()
    _add_otp(db, LINE_UID, EMAIL)

    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply, \
         patch("app.routers.webhook._get_line_display_name", new_callable=AsyncMock, return_value=None):
        await _handle_otp_verification(db, LINE_UID, "123456", TOKEN)

    assert "已被其他 LINE 帳號綁定" in mock_reply.call_args[0][1]
    ev = db.query(EmailVerification).filter(EmailVerification.email == EMAIL).first()
    assert ev.used is True  # OTP must be invalidated to prevent replay


async def test_otp_same_uid_idempotent(db):
    """Submitting an OTP for an email already bound to the same UID succeeds."""
    db.add(Employee(email=EMAIL, line_user_id=LINE_UID))
    db.commit()
    _add_otp(db, LINE_UID, EMAIL)

    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply, \
         patch("app.routers.webhook._get_line_display_name", new_callable=AsyncMock, return_value=None):
        await _handle_otp_verification(db, LINE_UID, "123456", TOKEN)

    assert "綁定完成" in mock_reply.call_args[0][1]


async def test_otp_wrong_code_increments_counter(db):
    """Wrong OTP increments failed_attempts and shows remaining tries."""
    _add_otp(db, LINE_UID, EMAIL)

    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply, \
         patch("app.routers.webhook._get_line_display_name", new_callable=AsyncMock, return_value=None):
        await _handle_otp_verification(db, LINE_UID, "000000", TOKEN)  # wrong code

    ev = db.query(EmailVerification).filter(EmailVerification.email == EMAIL).first()
    assert ev.failed_attempts == 1
    assert "錯誤" in mock_reply.call_args[0][1]


async def test_otp_locked_after_max_attempts(db):
    """After 5 failed attempts the OTP is locked and no further verification allowed."""
    _add_otp(db, LINE_UID, EMAIL, failed_attempts=4)  # one attempt away from lockout

    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply, \
         patch("app.routers.webhook._get_line_display_name", new_callable=AsyncMock, return_value=None):
        await _handle_otp_verification(db, LINE_UID, "000000", TOKEN)  # wrong — triggers lockout

    ev = db.query(EmailVerification).filter(EmailVerification.email == EMAIL).first()
    assert ev.failed_attempts == 5
    assert "次數過多" in mock_reply.call_args[0][1]

    # Subsequent attempt with the CORRECT code must also fail (record now excluded by filter)
    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply2, \
         patch("app.routers.webhook._get_line_display_name", new_callable=AsyncMock, return_value=None):
        await _handle_otp_verification(db, LINE_UID, "123456", TOKEN)

    assert "驗證碼無效" in mock_reply2.call_args[0][1]


# ── _handle_query ─────────────────────────────────────────────────────────────

async def test_query_non_manager_rejected(db):
    """Non-manager employee → access denied."""
    db.add(Employee(email=EMAIL, line_user_id=LINE_UID, is_manager=False, is_active=True))
    db.commit()

    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply:
        await _handle_query(db, LINE_UID, "2026-04", TOKEN)

    assert "僅限管理員" in mock_reply.call_args[0][1]


async def test_query_invalid_month_format(db):
    """Malformed month string → format error reply."""
    db.add(Employee(email=EMAIL, line_user_id=LINE_UID, is_manager=True, is_active=True))
    db.commit()

    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply:
        await _handle_query(db, LINE_UID, "not-a-date", TOKEN)

    assert "格式錯誤" in mock_reply.call_args[0][1]


async def test_query_year_out_of_range(db):
    """Year < 2000 → out-of-range guard triggers format error reply."""
    db.add(Employee(email=EMAIL, line_user_id=LINE_UID, is_manager=True, is_active=True))
    db.commit()

    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply:
        await _handle_query(db, LINE_UID, "1999-12", TOKEN)

    assert "格式錯誤" in mock_reply.call_args[0][1]


async def test_query_no_check_ins(db):
    """Valid manager, valid period, no records → no-records reply."""
    db.add(Employee(email=EMAIL, line_user_id=LINE_UID, is_manager=True, is_active=True))
    db.commit()

    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply:
        await _handle_query(db, LINE_UID, "2020-01", TOKEN)

    assert "無任何打卡紀錄" in mock_reply.call_args[0][1]


async def test_query_summary_with_check_ins(db):
    """Manager queries a month with real check-in data → formatted summary returned."""
    manager = Employee(
        email="manager@example.com", line_user_id=LINE_UID,
        is_manager=True, is_active=True, full_name="Manager Bob",
    )
    worker = Employee(
        email=EMAIL, line_user_id="U-worker",
        is_manager=False, is_active=True, full_name="Alice",
    )
    db.add_all([manager, worker])
    db.commit()
    db.refresh(worker)

    # Use UTC times that both fall on 2026-04-01 in Asia/Taipei (UTC+8):
    #   01:00 UTC = 09:00 Asia/Taipei  (clock-in)
    #   09:00 UTC = 17:00 Asia/Taipei  (clock-out)
    db.add(CheckIn(
        employee_id=worker.id,
        type=CheckInType.clock_in,
        latitude=25.033,
        longitude=121.565,
        ip_address="127.0.0.1",
        checked_at=datetime(2026, 4, 1, 1, 0, tzinfo=timezone.utc),
    ))
    db.add(CheckIn(
        employee_id=worker.id,
        type=CheckInType.clock_out,
        latitude=25.033,
        longitude=121.565,
        ip_address="127.0.0.1",
        checked_at=datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc),
    ))
    db.commit()

    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply, \
         patch("app.routers.webhook.get_settings", return_value=_mock_settings()):
        await _handle_query(db, LINE_UID, "2026-04", TOKEN)

    reply = mock_reply.call_args[0][1]
    assert "2026-04 出勤摘要" in reply
    assert "Alice" in reply
    assert "出勤天數：1" in reply
    assert "上班：1" in reply
    assert "下班：1" in reply


# ── _handle_follow ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_follow_unbound_sends_onboarding(db):
    """New employee (not yet bound) receives onboarding instructions."""
    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply:
        await _handle_follow(db, LINE_UID, TOKEN)

    reply = mock_reply.call_args[0][1]
    assert "歡迎" in reply
    assert "Email" in reply or "email" in reply.lower()


@pytest.mark.asyncio
async def test_follow_already_bound_sends_welcome_back(db):
    """Employee who is already bound and re-follows receives a welcome-back message."""
    employee = Employee(line_user_id=LINE_UID, email=EMAIL, is_active=True)
    db.add(employee)
    db.commit()

    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply:
        await _handle_follow(db, LINE_UID, TOKEN)

    reply = mock_reply.call_args[0][1]
    assert "歡迎" in reply
    # Should NOT re-show onboarding instructions
    assert "驗證碼" not in reply


# ── _handle_card_number ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_card_number_sets_for_unset_employee(db):
    """An employee without a card number can set one via LINE message."""
    db.add(Employee(email=EMAIL, line_user_id=LINE_UID, is_active=True))
    db.commit()

    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply:
        await _handle_card_number(db, LINE_UID, "A1234567", TOKEN)

    emp = db.query(Employee).filter(Employee.line_user_id == LINE_UID).first()
    assert emp.card_number == "A1234567"
    assert "✅" in mock_reply.call_args[0][1]


@pytest.mark.asyncio
async def test_card_number_blocks_overwrite_when_already_set(db):
    """Employee with an existing card number cannot overwrite it via LINE — directed to LIFF."""
    db.add(Employee(email=EMAIL, line_user_id=LINE_UID, card_number="OLD12345", is_active=True))
    db.commit()

    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply:
        await _handle_card_number(db, LINE_UID, "NEW12345", TOKEN)

    emp = db.query(Employee).filter(Employee.line_user_id == LINE_UID).first()
    # Card number must NOT be changed
    assert emp.card_number == "OLD12345"
    reply = mock_reply.call_args[0][1]
    assert "OLD12345" in reply
    assert "個人資料" in reply


@pytest.mark.asyncio
async def test_card_number_conflict_with_other_employee(db):
    """Card number already held by another employee → conflict reply, no change."""
    db.add(Employee(email=EMAIL, line_user_id=LINE_UID, is_active=True))
    db.add(Employee(email="other@aiotek.com.tw", line_user_id="Uother", card_number="TAKEN123", is_active=True))
    db.commit()

    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply:
        await _handle_card_number(db, LINE_UID, "TAKEN123", TOKEN)

    emp = db.query(Employee).filter(Employee.line_user_id == LINE_UID).first()
    assert emp.card_number is None
    assert "已被其他員工使用" in mock_reply.call_args[0][1]


@pytest.mark.asyncio
async def test_card_number_unbound_user_gets_error(db):
    """User with no bound employee record cannot set a card number."""
    with patch("app.routers.webhook._reply_text", new_callable=AsyncMock) as mock_reply:
        await _handle_card_number(db, "Ughost", "A1234567", TOKEN)

    assert "帳號綁定" in mock_reply.call_args[0][1]
