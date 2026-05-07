import hashlib
import hmac
import json
import logging
import re
import secrets
from base64 import b64encode
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, contains_eager

from app.config import get_settings
from app.database import get_db
from app.models.check_in import CheckIn, CheckInType
from app.models.email_verification import EmailVerification
from app.models.employee import CARD_NUMBER_RE, Employee
from app.services.mailgun import send_otp_email

router = APIRouter(tags=["webhook"])
logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[\w.+-]+@([\w-]+\.)+[a-zA-Z]{2,}$")
_ALLOWED_DOMAIN = "@aiotek.com.tw"
_OTP_RE = re.compile(r"^\d{6}$")
_MAX_OTP_ATTEMPTS = 5


def _verify_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    """Verify LINE webhook HMAC-SHA256 signature."""
    expected = b64encode(
        hmac.new(channel_secret.encode(), body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(expected, signature)


def _hash_otp(otp: str, line_user_id: str) -> str:
    """SHA-256 hash of OTP salted with the LINE user ID. Never store plaintext."""
    return hashlib.sha256(f"{line_user_id}:{otp}".encode()).hexdigest()


@router.post("/webhook")
async def webhook(
    request: Request,
    x_line_signature: str = Header(...),
    db: Session = Depends(get_db),
):
    body = await request.body()
    settings = get_settings()

    if not _verify_signature(body, x_line_signature, settings.line_channel_secret):
        raise HTTPException(status_code=400, detail="Invalid signature")

    payload = json.loads(body)

    for event in payload.get("events", []):
        try:
            event_type = event.get("type")

            if event_type == "follow":
                line_user_id: str = event["source"]["userId"]
                reply_token: str = event["replyToken"]
                await _handle_follow(db, line_user_id, reply_token)
                continue

            if event_type != "message":
                continue
            msg = event.get("message", {})
            if msg.get("type") != "text":
                continue

            line_user_id = event["source"]["userId"]
            reply_token = event["replyToken"]
            text: str = msg["text"].strip()

            if _EMAIL_RE.match(text):
                await _handle_email_submission(db, line_user_id, text.lower(), reply_token)
            elif _OTP_RE.match(text):
                await _handle_otp_verification(db, line_user_id, text, reply_token)
            elif text.lower().startswith("query "):
                await _handle_query(db, line_user_id, text[6:].strip(), reply_token)
            elif CARD_NUMBER_RE.fullmatch(text):
                await _handle_card_number(db, line_user_id, text.upper(), reply_token)
            elif text in ("略過", "跳過", "skip"):
                await _handle_skip(db, line_user_id, reply_token)
        except Exception:
            logger.exception("Error processing event %s", event.get("webhookEventId", ""))

    return {"status": "ok"}


# ── Binding flow ──────────────────────────────────────────────────────────────

async def _handle_follow(db: Session, line_user_id: str, reply_token: str) -> None:
    """Send onboarding instructions when an employee adds the bot as a friend."""
    # If already bound, greet them — remind about card number if not yet set
    existing = db.query(Employee).filter(
        Employee.line_user_id == line_user_id,
        Employee.is_active.is_(True),
    ).first()
    if existing:
        if not existing.card_number:
            await _reply_text(
                reply_token,
                "歡迎回來！\n\n"
                "您尚未設定員工卡號，請傳送 8 位數卡號完成設定（例：01234567）。\n"
                "卡號印在您的識別證或由 HR 提供。\n\n"
                "若無卡號請傳送「略過」。",
            )
        else:
            await _reply_text(reply_token, "歡迎回來！請使用下方選單進行打卡。")
        return

    await _reply_text(
        reply_token,
        "👋 歡迎使用 Aiotek 打卡系統！\n\n"
        "請依照以下步驟完成帳號設定：\n\n"
        "1️⃣ 直接傳送您的公司 Email（例：name@aiotek.com.tw）\n"
        "2️⃣ 系統將寄出 6 位數驗證碼至您的信箱\n"
        "3️⃣ 在此回傳驗證碼完成 Email 綁定\n"
        "4️⃣ 傳送您的 8 位數員工卡號（例：01234567）\n\n"
        "完成後即可使用下方選單上下班打卡。",
    )


async def _handle_email_submission(
    db: Session, line_user_id: str, email: str, reply_token: str
) -> None:
    # Already bound via this LINE account?
    if db.query(Employee).filter(
        Employee.line_user_id == line_user_id,
        Employee.is_active.is_(True),
    ).first():
        await _reply_text(reply_token, "您的 LINE 帳號已完成綁定，無需重複操作。")
        return

    # Enforce company email domain
    if not email.endswith(_ALLOWED_DOMAIN):
        await _reply_text(
            reply_token,
            f"只接受公司 Email（{_ALLOWED_DOMAIN}），請確認後重新輸入。",
        )
        return

    # Email already bound to a different LINE account?
    existing = db.query(Employee).filter(
        Employee.email == email,
        Employee.is_active.is_(True),
    ).first()
    if existing and existing.line_user_id:
        await _reply_text(
            reply_token,
            f"此 Email（{email}）已被其他帳號綁定，請聯繫管理員。",
        )
        return

    settings = get_settings()
    debug_mode = settings.debug and not settings.mailgun_enabled

    if not settings.mailgun_enabled and not settings.debug:
        await _reply_text(reply_token, "Email 服務尚未設定，請聯繫管理員。")
        return

    # TODO (P1): add per-LINE-UID rate limiting to prevent Mailgun spam on unbound emails

    # Invalidate any pending (unused) OTPs for this LINE UID, then issue a new one
    otp = f"{secrets.randbelow(1_000_000):06d}"
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)

    db.query(EmailVerification).filter(
        EmailVerification.line_user_id == line_user_id,
        EmailVerification.used.is_(False),
    ).update({"used": True})

    db.add(EmailVerification(
        line_user_id=line_user_id,
        email=email,
        otp_code=_hash_otp(otp, line_user_id),  # store hash, never plaintext
        expires_at=expires,
    ))
    db.commit()

    if debug_mode:
        # DEBUG only — never use in production (exposes OTP in plaintext)
        await _reply_text(
            reply_token,
            f"[DEBUG] Mailgun 未設定，驗證碼直接顯示：\n\n{otp}\n\n請在 10 分鐘內回傳此 6 位數驗證碼。",
        )
        return

    sent = await send_otp_email(email, otp)
    if sent:
        await _reply_text(
            reply_token,
            f"已將驗證碼傳送至 {email}，\n請在 10 分鐘內回傳 6 位數驗證碼。",
        )
    else:
        await _reply_text(reply_token, "Email 傳送失敗，請稍後再試或聯繫管理員。")


async def _handle_otp_verification(
    db: Session, line_user_id: str, otp_code: str, reply_token: str
) -> None:
    now = datetime.now(timezone.utc)

    # Find the most recent valid (unused, unexpired, not locked) OTP for this UID
    verification = (
        db.query(EmailVerification)
        .filter(
            EmailVerification.line_user_id == line_user_id,
            EmailVerification.used.is_(False),
            EmailVerification.expires_at > now,
            EmailVerification.failed_attempts < _MAX_OTP_ATTEMPTS,
        )
        .order_by(EmailVerification.id.desc())
        .first()
    )

    if not verification:
        await _reply_text(reply_token, "驗證碼無效或已過期，請重新傳送您的公司 Email。")
        return

    # Verify hash — increment counter on mismatch
    if _hash_otp(otp_code, line_user_id) != verification.otp_code:
        verification.failed_attempts += 1
        db.commit()
        remaining = _MAX_OTP_ATTEMPTS - verification.failed_attempts
        if remaining > 0:
            await _reply_text(reply_token, f"驗證碼錯誤，還剩 {remaining} 次機會。")
        else:
            await _reply_text(
                reply_token,
                "驗證碼嘗試次數過多，請重新傳送您的公司 Email 取得新驗證碼。",
            )
        return

    verification.used = True

    # HR-initiated path: employee record already exists (email pre-loaded) but unbound
    employee = db.query(Employee).filter(
        Employee.email == verification.email,
        Employee.is_active.is_(True),
    ).first()
    if employee:
        # Guard against race: another LINE user may have bound this email between
        # OTP issuance and verification (overwrite vulnerability fix)
        if employee.line_user_id and employee.line_user_id != line_user_id:
            db.commit()  # persist used=True so this OTP cannot be replayed
            await _reply_text(reply_token, "此 Email 已被其他 LINE 帳號綁定，請聯繫管理員。")
            return
        employee.line_user_id = line_user_id
    else:
        # Employee-initiated path: create new record now
        employee = Employee(line_user_id=line_user_id, email=verification.email)
        db.add(employee)

    # Fetch LINE display name as fallback for audit display
    display_name = await _get_line_display_name(line_user_id)
    if display_name:
        employee.display_name = display_name

    db.commit()
    await _reply_text(
        reply_token,
        "✅ 帳號綁定完成！\n\n"
        "最後一步：請傳送您的 8 位數員工卡號（例：01234567）\n"
        "卡號印在您的識別證或由 HR 提供。\n\n"
        "若目前沒有卡號，請傳送「略過」，之後可隨時在打卡應用程式的個人資料中設定。",
    )


# ── Card number setup ─────────────────────────────────────────────────────────

async def _handle_card_number(db: Session, line_user_id: str, card_number: str, reply_token: str) -> None:
    employee = db.query(Employee).filter(
        Employee.line_user_id == line_user_id,
        Employee.is_active.is_(True),
    ).first()
    if not employee:
        await _reply_text(reply_token, "請先完成帳號綁定後再設定卡號。")
        return

    # Already has a card number — block silent overwrite; direct to LIFF profile instead.
    if employee.card_number:
        await _reply_text(
            reply_token,
            f"您目前的員工卡號為：{employee.card_number}\n\n"
            "如需變更卡號，請至打卡應用程式的「個人資料」頁面進行修改。",
        )
        return

    conflict = db.query(Employee).filter(
        Employee.card_number == card_number,
        Employee.id != employee.id,
    ).first()
    if conflict:
        await _reply_text(reply_token, "此卡號已被其他員工使用，請確認後重新輸入。")
        return

    employee.card_number = card_number
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        await _reply_text(reply_token, "此卡號已被其他員工使用，請確認後重新輸入。")
        return
    await _reply_text(
        reply_token,
        f"✅ 員工卡號已設定為：{card_number}\n\n您現在可以開始使用打卡系統了！",
    )


async def _handle_skip(db: Session, line_user_id: str, reply_token: str) -> None:
    employee = db.query(Employee).filter(
        Employee.line_user_id == line_user_id,
        Employee.is_active.is_(True),
    ).first()
    if employee:
        await _reply_text(
            reply_token,
            "已略過卡號設定。您可隨時開啟打卡應用程式，在個人資料頁面補充卡號。",
        )
    else:
        await _reply_text(reply_token, "請先完成帳號綁定。請傳送您的公司 Email 開始設定。")


# ── Manager LINE query ────────────────────────────────────────────────────────

async def _handle_query(
    db: Session, line_user_id: str, month_str: str, reply_token: str
) -> None:
    # Only managers may query
    manager = (
        db.query(Employee)
        .filter(
            Employee.line_user_id == line_user_id,
            Employee.is_manager.is_(True),
            Employee.is_active.is_(True),
        )
        .first()
    )
    if not manager:
        await _reply_text(reply_token, "此功能僅限管理員使用。")
        return

    # Parse YYYY-MM with basic sanity bounds
    try:
        year, month = map(int, month_str.split("-"))
        if not (2000 <= year <= 2100 and 1 <= month <= 12):
            raise ValueError("out of range")
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        end = datetime(year + (month // 12), (month % 12) + 1, 1, tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        await _reply_text(reply_token, "格式錯誤，請輸入：query YYYY-MM（例：query 2026-04）")
        return

    check_ins = (
        db.query(CheckIn)
        .join(CheckIn.employee)
        .options(contains_eager(CheckIn.employee))
        .filter(
            CheckIn.checked_at >= start,
            CheckIn.checked_at < end,
            Employee.is_active.is_(True),
        )
        .order_by(Employee.full_name, CheckIn.checked_at)
        .all()
    )

    if not check_ins:
        await _reply_text(reply_token, f"{month_str} 無任何打卡紀錄。")
        return

    settings = get_settings()
    tz = ZoneInfo(settings.timezone)
    summary: dict[str, dict] = defaultdict(
        lambda: {"days": set(), "clock_ins": 0, "clock_outs": 0}
    )
    for ci in check_ins:
        emp = ci.employee
        name = emp.full_name or emp.display_name or emp.email
        summary[name]["days"].add(ci.checked_at.astimezone(tz).date())
        if ci.type == CheckInType.clock_in:
            summary[name]["clock_ins"] += 1
        else:
            summary[name]["clock_outs"] += 1

    lines = [f"📊 {month_str} 出勤摘要", ""]
    for name, data in sorted(summary.items()):
        lines += [
            f"👤 {name}",
            f"   出勤天數：{len(data['days'])} 天",
            f"   上班：{data['clock_ins']} 次　下班：{data['clock_outs']} 次",
            "",
        ]

    msg = "\n".join(lines).rstrip()
    if len(msg) > 4900:
        msg = msg[:4900] + "\n⋯（請至後台查看完整紀錄）"

    await _reply_text(reply_token, msg)


# ── LINE API helpers ──────────────────────────────────────────────────────────

async def _reply_text(reply_token: str, text: str) -> None:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=5.0) as client:
        await client.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={"Authorization": f"Bearer {settings.line_channel_access_token}"},
            json={"replyToken": reply_token, "messages": [{"type": "text", "text": text}]},
        )


async def _get_line_display_name(line_user_id: str) -> str | None:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"https://api.line.me/v2/bot/profile/{line_user_id}",
            headers={"Authorization": f"Bearer {settings.line_channel_access_token}"},
        )
    return resp.json().get("displayName") if resp.status_code == 200 else None
