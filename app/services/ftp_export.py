"""FTP upload utility and factory file builder for factory punch export."""
from __future__ import annotations

import ftplib
import io
import logging
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from app.models.check_in import CheckIn

logger = logging.getLogger(__name__)


def build_factory_lines(
    check_ins: list[CheckIn],
    machine_id: str,
    tz: ZoneInfo,
) -> list[str]:
    """Convert CheckIn rows to factory punch file lines (machine,card,date,time)."""
    lines = []
    for ci in check_ins:
        local_dt = ci.checked_at.astimezone(tz)
        lines.append(
            f"{machine_id},"
            f"{ci.employee.card_number},"
            f"{local_dt.strftime('%Y/%m/%d')},"
            f"{local_dt.strftime('%H:%M:%S')}"
        )
    return lines


def upload_factory_file(
    host: str,
    user: str,
    password: str,
    remote_dir: str,
    filename: str,
    content: bytes,
) -> None:
    """Upload content as filename to the factory FTP server."""
    with ftplib.FTP(host, timeout=30) as ftp:
        ftp.login(user, password)
        if remote_dir and remote_dir != "/":
            # Windows FTP servers use CP950 (Traditional Chinese); sending UTF-8 causes 451 error
            ftp.sock.sendall(f"CWD {remote_dir}\r\n".encode("cp950"))
            ftp.getresp()
        ftp.storbinary(f"STOR {filename}", io.BytesIO(content))
    logger.info("Factory FTP upload complete: %s → %s:%s", filename, host, remote_dir)
