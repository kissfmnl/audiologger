import logging
import secrets
import time
from collections import defaultdict, deque
from threading import Lock
from urllib.parse import urlparse

from fastapi import Request

logger = logging.getLogger(__name__)

SESSION_CSRF_KEY = "contact_csrf"
SESSION_FORM_ISSUED_KEY = "contact_form_issued_at"
HONEYPOT_FIELD = "company_website"

MIN_FORM_SECONDS = 3
MAX_FORM_AGE_SECONDS = 60 * 60
MAX_SUBMISSIONS_PER_HOUR = 5
MAX_FIELD_LENGTH = 500

_rate_lock = Lock()
_rate_buckets: dict[str, deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def issue_contact_form(request: Request) -> str:
    token = secrets.token_urlsafe(32)
    request.session[SESSION_CSRF_KEY] = token
    request.session[SESSION_FORM_ISSUED_KEY] = time.time()
    return token


def _rate_limit_ok(ip: str) -> bool:
    now = time.time()
    cutoff = now - 3600
    with _rate_lock:
        bucket = _rate_buckets[ip]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= MAX_SUBMISSIONS_PER_HOUR:
            return False
        bucket.append(now)
        return True


def _valid_stream_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def validate_contact_submission(
    request: Request,
    *,
    csrf_token: str,
    honeypot: str,
    name: str,
    email: str,
    station_name: str,
    stream_url: str,
    message: str,
) -> str | None:
    """Return error message when blocked, None when allowed."""
    ip = _client_ip(request)

    if honeypot.strip():
        logger.warning("Contact form honeypot triggered from %s", ip)
        return "Je aanvraag kon niet worden verwerkt."

    expected = request.session.get(SESSION_CSRF_KEY)
    if not expected or not csrf_token or not secrets.compare_digest(csrf_token, expected):
        logger.warning("Contact form CSRF mismatch from %s", ip)
        return "Je sessie is verlopen. Vernieuw de pagina en probeer opnieuw."

    issued_at = request.session.get(SESSION_FORM_ISSUED_KEY)
    if not isinstance(issued_at, (int, float)):
        return "Je sessie is verlopen. Vernieuw de pagina en probeer opnieuw."

    elapsed = time.time() - issued_at
    if elapsed < MIN_FORM_SECONDS:
        logger.warning("Contact form submitted too fast (%.1fs) from %s", elapsed, ip)
        return "Je aanvraag kon niet worden verwerkt."

    if elapsed > MAX_FORM_AGE_SECONDS:
        return "Het formulier is verlopen. Vernieuw de pagina en probeer opnieuw."

    if not _rate_limit_ok(ip):
        logger.warning("Contact form rate limit exceeded for %s", ip)
        return "Te veel aanvragen. Probeer het later opnieuw."

    if len(name) > MAX_FIELD_LENGTH or len(email) > MAX_FIELD_LENGTH:
        return "Een of meer velden zijn te lang."

    if len(station_name) > MAX_FIELD_LENGTH or len(stream_url) > MAX_FIELD_LENGTH:
        return "Een of meer velden zijn te lang."

    if len(message) > 2000:
        return "Opmerking is te lang."

    if not _valid_stream_url(stream_url):
        return "Vul een geldige stream-URL in (http of https)."

    # Rotate token after successful validation (single use).
    request.session.pop(SESSION_CSRF_KEY, None)
    request.session.pop(SESSION_FORM_ISSUED_KEY, None)
    return None
