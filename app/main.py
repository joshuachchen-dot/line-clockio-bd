from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.routers import webhook, liff, dashboard, jobs

_settings = get_settings()

app = FastAPI(
    title="LINE Clockio",
    docs_url="/docs" if _settings.debug else None,
    redoc_url="/redoc" if _settings.debug else None,
)

app.add_middleware(
    SessionMiddleware,
    secret_key=_settings.session_secret_key,
    https_only=not _settings.debug,  # Cloud Run terminates TLS at proxy; force https_only in prod
    max_age=8 * 3600,  # 8-hour session expiry
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(webhook.router)
app.include_router(liff.router)
app.include_router(dashboard.router)
app.include_router(jobs.router)


@app.get("/health")
def health():
    return {"status": "ok"}
