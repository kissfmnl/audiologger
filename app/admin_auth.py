import hashlib
import os
import secrets

from fastapi import Depends, HTTPException, Request, status
from starlette.middleware.sessions import SessionMiddleware

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "ferryoomen")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "ferryoomen")
ADMIN_SECRET = os.getenv(
    "ADMIN_SECRET",
    hashlib.sha256(f"{ADMIN_USERNAME}:{ADMIN_PASSWORD}:audiologger".encode()).hexdigest(),
)
SESSION_KEY = "admin_authenticated"


def get_session_middleware_kwargs() -> dict:
    return {
        "secret_key": ADMIN_SECRET,
        "session_cookie": "audiologger_admin",
        "max_age": 60 * 60 * 24 * 7,
        "same_site": "lax",
        "https_only": os.getenv("ADMIN_HTTPS_ONLY", "false").lower() == "true",
    }


def verify_credentials(username: str, password: str) -> bool:
    return (
        secrets.compare_digest(username.strip(), ADMIN_USERNAME)
        and secrets.compare_digest(password, ADMIN_PASSWORD)
    )


def login(request: Request, username: str, password: str) -> bool:
    if verify_credentials(username, password):
        request.session[SESSION_KEY] = True
        request.session["admin_user"] = ADMIN_USERNAME
        return True
    return False


def logout(request: Request) -> None:
    request.session.clear()


def is_authenticated(request: Request) -> bool:
    return request.session.get(SESSION_KEY) is True


def require_admin(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/admin/login"},
        )
