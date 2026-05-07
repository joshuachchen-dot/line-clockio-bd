from __future__ import annotations

import datetime
import re
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base

# Single source of truth for card number format — imported by routers and webhook
CARD_NUMBER_RE = re.compile(r'^[0-9A-Za-z]{8}$')

if TYPE_CHECKING:
    from app.models.check_in import CheckIn


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    # HR-assigned fields — may be NULL when employee self-registers first
    # UNIQUE + nullable: PostgreSQL (our target DB) allows multiple NULL values in a
    # UNIQUE column (NULL ≠ NULL per SQL standard), so this is safe.
    employee_number: Mapped[Optional[str]] = mapped_column(String(20), unique=True, nullable=True, index=True)
    card_number: Mapped[Optional[str]] = mapped_column(String(8), unique=True, nullable=True, index=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # LINE UID — NULL when HR pre-loads record before employee completes binding
    line_user_id: Mapped[Optional[str]] = mapped_column(String(50), unique=True, nullable=True, index=True)
    # TODO (P1): enforce lowercase at DB layer via CHECK (email = LOWER(email))
    email: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(100))  # LINE profile name (fallback)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_manager: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )

    check_ins: Mapped[List["CheckIn"]] = relationship("CheckIn", back_populates="employee")
