"""FTP upload utility for factory punch export."""
from __future__ import annotations

import ftplib
import io
import logging

logger = logging.getLogger(__name__)


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
            ftp.cwd(remote_dir)
        ftp.storbinary(f"STOR {filename}", io.BytesIO(content))
    logger.info("Factory FTP upload complete: %s → %s:%s", filename, host, remote_dir)
