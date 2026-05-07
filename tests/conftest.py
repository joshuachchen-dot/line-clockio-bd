"""Pytest configuration and shared fixtures.

Environment variables must be set before any `app.*` import because
`app.database` calls `get_settings()` at module level to build the engine.
"""

import os

# Provide stub values for all required settings so pydantic-settings doesn't
# error on import.  Individual tests that call settings-dependent code should
# patch `app.routers.<module>.get_settings` with a MagicMock as needed.
os.environ.setdefault("DEBUG", "true")  # disables https_only on session cookie in tests
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test-line-token")
os.environ.setdefault("LINE_CHANNEL_SECRET", "test-line-secret")
os.environ.setdefault("LIFF_ID", "test-liff-id")
os.environ.setdefault("LIFF_CHANNEL_ID", "test-liff-channel-id")
os.environ.setdefault("LIFF_CHANNEL_SECRET", "test-liff-channel-secret")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("MAILGUN_API_KEY", "test-mailgun-key")
os.environ.setdefault("MAILGUN_FROM_EMAIL", "noreply@test.example.com")
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret-key-32-chars!")
os.environ.setdefault("APP_BASE_URL", "http://localhost:8000")
os.environ.setdefault("INTERNAL_SECRET", "test-internal-secret")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture
def db():
    """In-memory SQLite session — created fresh for every test."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()  # reset pool before drop_all (rollback tests may leave pool in detached state)


@pytest.fixture
def client(db):
    """FastAPI TestClient wired to the in-memory DB session."""
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()
