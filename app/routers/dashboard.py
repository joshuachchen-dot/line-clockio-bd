import asyncio
import csv
import hmac
import io
import logging
import secrets
from datetime import datetime
from typing import Annotated
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import BeforeValidator
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.check_in import CheckIn, CheckInType
from app.models.employee import CARD_NUMBER_RE, Employee
from app.services.checkin_query import build_checkin_query
from app.services.ftp_export import build_factory_lines
from app.services.mailgun import send_invitation_email

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)

_LINE_AUTH_URL = "https://access.line.me/oauth2/v2.1/authorize"
_LINE_TOKEN_URL = "https://api.line.me/oauth2/v2.1/token"
_LINE_VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"
_MAX_IMPORT_BYTES = 5 * 1024 * 1024  # 5 MB

# HTML forms send "" for unselected optional int fields; treat that as None.
_OptIntQ = Annotated[int | None, BeforeValidator(lambda v: None if v == "" else v)]


def _redirect_uri() -> str:
    return f"{get_settings().app_base_url}/dashboard/callback"


def _is_manager(request: Request) -> bool:
    """Return True if the request has a valid manager session."""
    return bool(request.session.get("manager_id"))


def _get_csrf_token(request: Request) -> str:
    """Return (and lazily create) a per-session CSRF token."""
    if "csrf_token" not in request.session:
        request.session["csrf_token"] = secrets.token_hex(32)
    return request.session["csrf_token"]


def _csrf_ok(request: Request, token: str) -> bool:
    """Constant-time comparison of submitted token against session token."""
    expected = request.session.get("csrf_token", "")
    return bool(expected and hmac.compare_digest(expected, token))


def _csv_safe(value: str | None) -> str:
    """Prevent CSV formula injection for Excel (prefix dangerous leading chars with ')."""
    s = "" if value is None else str(value)
    return ("'" + s) if s and s[0] in ("=", "+", "-", "@", "\t", "\r") else s


# ── Auth ──────────────────────────────────────────────────────────────────────

@router.get("/login")
async def login(request: Request, error: str | None = None):
    if _is_manager(request):
        return RedirectResponse("/dashboard/")
    return templates.TemplateResponse(request, "dashboard/login.html", {"error": error})


@router.get("/login/line")
async def login_line(request: Request):
    """Redirect manager to LINE Login authorization page."""
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    params = urlencode({
        "response_type": "code",
        "client_id": get_settings().liff_channel_id,
        "redirect_uri": _redirect_uri(),
        "state": state,
        "scope": "openid profile",
    })
    return RedirectResponse(f"{_LINE_AUTH_URL}?{params}")


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    """Handle LINE Login OAuth callback, verify identity, and create session."""
    if error:
        return RedirectResponse(f"/dashboard/login?error={quote(error)}")

    stored_state = request.session.pop("oauth_state", None)
    if not state or state != stored_state:
        return RedirectResponse("/dashboard/login?error=invalid_state")

    if not code:
        return RedirectResponse("/dashboard/login?error=missing_code")

    settings = get_settings()

    async with httpx.AsyncClient(timeout=10.0) as client:
        token_resp = await client.post(
            _LINE_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _redirect_uri(),
                "client_id": settings.liff_channel_id,
                "client_secret": settings.liff_channel_secret,
            },
        )
        if token_resp.status_code != 200:
            return RedirectResponse("/dashboard/login?error=token_exchange_failed")

        id_token = token_resp.json().get("id_token")
        if not id_token:
            return RedirectResponse("/dashboard/login?error=missing_id_token")

        verify_resp = await client.post(
            _LINE_VERIFY_URL,
            data={"id_token": id_token, "client_id": settings.liff_channel_id},
        )
    if verify_resp.status_code != 200:
        return RedirectResponse("/dashboard/login?error=token_verify_failed")

    line_user_id = verify_resp.json().get("sub")
    if not line_user_id:
        return RedirectResponse("/dashboard/login?error=missing_sub")

    employee = (
        db.query(Employee)
        .filter(
            Employee.line_user_id == line_user_id,
            Employee.is_manager.is_(True),
            Employee.is_active.is_(True),
        )
        .first()
    )
    if not employee:
        return RedirectResponse("/dashboard/login?error=not_a_manager")

    request.session["manager_id"] = employee.id
    request.session["manager_line_id"] = line_user_id
    return RedirectResponse("/dashboard/")


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/dashboard/login")


# ── Attendance list ───────────────────────────────────────────────────────────

@router.get("/")
async def dashboard_home(
    request: Request,
    db: Session = Depends(get_db),
    employee_id: _OptIntQ = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    if not _is_manager(request):
        return RedirectResponse("/dashboard/login")

    settings = get_settings()
    tz = ZoneInfo(settings.timezone)

    all_employees = (
        db.query(Employee)
        .filter(Employee.is_active.is_(True))
        .order_by(Employee.full_name, Employee.display_name)
        .all()
    )

    check_ins = (
        build_checkin_query(db, tz, employee_id, date_from, date_to)
        .order_by(CheckIn.checked_at.desc())
        .limit(500)
        .all()
    )

    return templates.TemplateResponse(
        request,
        "dashboard/index.html",
        {
            "employees": all_employees,
            "check_ins": check_ins,
            "tz": tz,
            "filters": {
                "employee_id": employee_id,
                "date_from": date_from or "",
                "date_to": date_to or "",
            },
        },
    )


# ── CSV export ────────────────────────────────────────────────────────────────

@router.get("/export")
async def export_csv(
    request: Request,
    db: Session = Depends(get_db),
    employee_id: _OptIntQ = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    if not _is_manager(request):
        return RedirectResponse("/dashboard/login")

    settings = get_settings()
    tz = ZoneInfo(settings.timezone)

    check_ins = (
        build_checkin_query(db, tz, employee_id, date_from, date_to)
        .order_by(CheckIn.checked_at.asc())
        .all()
    )
    exported_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

    # Use StringIO then encode to utf-8-sig bytes so the BOM is actually written
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "員工編號", "姓名", "Email", "員工卡號",
        "打卡類型", "打卡時間(UTC+8)",
        "GPS緯度", "GPS經度", "IP位址",
        "匯出時間",
    ])
    for ci in check_ins:
        emp = ci.employee
        writer.writerow([
            _csv_safe(emp.employee_number),
            _csv_safe(emp.full_name or emp.display_name),
            _csv_safe(emp.email),
            _csv_safe(emp.card_number),
            "上班打卡" if ci.type == CheckInType.clock_in else "下班打卡",
            ci.checked_at.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S"),
            ci.latitude,
            ci.longitude,
            _csv_safe(ci.ip_address),
            exported_at,
        ])

    # Encode to utf-8-sig so Excel on Windows receives the BOM byte sequence
    csv_bytes = output.getvalue().encode("utf-8-sig")
    filename = f"attendance_{datetime.now(tz).strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([csv_bytes]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Employee binding status ───────────────────────────────────────────────────

@router.get("/employees")
async def employee_list(
    request: Request,
    db: Session = Depends(get_db),
    imported: int | None = None,
    created: int | None = None,
    updated: int | None = None,
    errors: int | None = None,
):
    if not _is_manager(request):
        return RedirectResponse("/dashboard/login")

    employees = (
        db.query(Employee)
        .order_by(
            Employee.employee_number.is_(None),  # NULLs last
            Employee.employee_number,
            Employee.email,
        )
        .all()
    )
    return templates.TemplateResponse(
        request,
        "dashboard/employees.html",
        {
            "employees": employees,
            "csrf_token": _get_csrf_token(request),
            "import_result": {
                "shown": imported == 1,
                "created": created or 0,
                "updated": updated or 0,
                "errors": errors or 0,
            },
        },
    )


# ── HR batch import ───────────────────────────────────────────────────────────

@router.post("/import")
async def hr_import(
    request: Request,
    file: UploadFile = File(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    if not _is_manager(request):
        return RedirectResponse("/dashboard/login")

    if not _csrf_ok(request, csrf_token):
        return RedirectResponse("/dashboard/employees?error=csrf", status_code=303)

    content = await file.read(_MAX_IMPORT_BYTES + 1)
    if len(content) > _MAX_IMPORT_BYTES:
        return RedirectResponse("/dashboard/employees?error=file_too_large", status_code=303)

    try:
        text = content.decode("utf-8-sig")  # handles BOM from Excel exports
    except UnicodeDecodeError:
        text = content.decode("big5", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    created = updated = errors = 0
    to_invite: list[tuple[str, str]] = []  # (email, name) for new employees

    for row in reader:
        emp_no = (row.get("員工編號") or row.get("employee_number") or "").strip()
        full_name = (row.get("姓名") or row.get("full_name") or "").strip()
        email = (row.get("Email") or row.get("email") or "").strip().lower()
        raw_card = (row.get("員工卡號") or row.get("card_number") or "").strip()
        if raw_card:
            raw_card = raw_card.upper()
            card_no = raw_card if CARD_NUMBER_RE.fullmatch(raw_card) else None
        else:
            card_no = None

        if not email:
            errors += 1
            continue

        existing = db.query(Employee).filter(Employee.email == email).first()
        if existing:
            if emp_no:
                existing.employee_number = emp_no
            if full_name:
                existing.full_name = full_name
            if card_no:
                existing.card_number = card_no
            updated += 1
        else:
            db.add(Employee(
                employee_number=emp_no or None,
                card_number=card_no,
                full_name=full_name or None,
                email=email,
            ))
            to_invite.append((email, full_name or email))
            created += 1

    # Commit all DB changes first — emails sent after so a rollback never orphans sent mail
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        logger.warning("HR import aborted — duplicate key in batch: %s", exc.orig)
        # Rollback discards the entire batch — report 0 created/updated so the
        # manager knows nothing was written, not the pre-rollback in-memory counts.
        return RedirectResponse(
            f"/dashboard/employees?imported=1&created=0&updated=0&errors={errors + 1}",
            status_code=303,
        )

    # Send invitation emails concurrently (max 5 in parallel) to avoid Cloud Run timeout
    sem = asyncio.Semaphore(5)

    async def _send_one(inv_email: str, inv_name: str) -> None:
        async with sem:
            await send_invitation_email(inv_email, inv_name)

    await asyncio.gather(*[_send_one(e, n) for e, n in to_invite])

    return RedirectResponse(
        f"/dashboard/employees?imported=1&created={created}&updated={updated}&errors={errors}",
        status_code=303,
    )


# ── Re-send invitation ────────────────────────────────────────────────────────

@router.post("/employees/{emp_id}/invite")
async def resend_invite(
    emp_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    if not _is_manager(request):
        return RedirectResponse("/dashboard/login")

    if not _csrf_ok(request, csrf_token):
        return RedirectResponse("/dashboard/employees?error=csrf", status_code=303)

    employee = db.query(Employee).filter(Employee.id == emp_id).first()
    if employee and not employee.line_user_id:
        await send_invitation_email(
            employee.email,
            employee.full_name or employee.display_name or employee.email,
        )
    return RedirectResponse("/dashboard/employees", status_code=303)


# ── Factory punch export ──────────────────────────────────────────────────────

@router.get("/export/factory")
async def export_factory(
    request: Request,
    db: Session = Depends(get_db),
    employee_id: _OptIntQ = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    if not _is_manager(request):
        return RedirectResponse("/dashboard/login")

    settings = get_settings()
    tz = ZoneInfo(settings.timezone)

    # Default to today when no date range is provided to prevent accidental full-DB dumps.
    today = datetime.now(tz).strftime("%Y-%m-%d")
    date_from = date_from or today
    date_to = date_to or today

    check_ins = (
        build_checkin_query(db, tz, employee_id, date_from, date_to)
        .filter(Employee.card_number.isnot(None))
        .order_by(CheckIn.checked_at.asc())
        .all()
    )

    lines = build_factory_lines(check_ins, settings.factory_machine_id, tz)
    content = "\n".join(lines) + ("\n" if lines else "")
    filename = f"factory_{datetime.now(tz).strftime('%Y%m%d_%H%M%S')}.txt"
    return StreamingResponse(
        iter([content.encode("utf-8")]),
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
