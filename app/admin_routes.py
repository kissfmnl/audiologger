from datetime import datetime
from zoneinfo import ZoneInfo
import base64
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from app.admin_auth import is_authenticated, login, logout
from app.database import BASE_DIR, get_session, get_storage_status
from app.logging_status import get_logging_overview
from app.retention import (
    cleanup_expired_recordings,
    get_archive_stats,
    preview_purge,
    purge_recordings,
)
from app.scheduler import (
    cancel_first_recording,
    hard_refresh_recordings,
    reload_scheduler,
    schedule_first_recording,
)
from app.site_settings import load_site_settings, load_stream_requests, save_site_settings
from app.stations import (
    COUNTRIES,
    DEFAULT_EVENT_RETENTION_DAYS,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_TIMEZONE,
    create_station,
    delete_station,
    get_station_model,
    get_timezone_groups,
    load_stations,
    save_station_logo,
    update_station,
)

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class LogoUploadBody(BaseModel):
    logo_data: str


DUTCH_DAYS = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
DUTCH_MONTHS = [
    "januari", "februari", "maart", "april", "mei", "juni",
    "juli", "augustus", "september", "oktober", "november", "december",
]

ADMIN_TZ = ZoneInfo(DEFAULT_TIMEZONE)


def admin_now() -> datetime:
    return datetime.now(ADMIN_TZ)


def format_dutch_date(dt: datetime | None = None) -> str:
    dt = dt or admin_now()
    if dt.tzinfo is not None:
        dt = dt.astimezone(ADMIN_TZ)
    return f"{DUTCH_DAYS[dt.weekday()]} {dt.day} {DUTCH_MONTHS[dt.month - 1]} {dt.year}"


def _decode_data_url(data_url: str) -> bytes:
    match = re.match(r"^data:image/(jpeg|jpg|png|webp);base64,(.+)$", data_url, re.DOTALL)
    if not match:
        raise ValueError("Ongeldig logo-formaat")
    return base64.b64decode(match.group(2))


def _save_logo_from_data_url(station_id: str, data_url: str) -> None:
    save_station_logo(station_id, _decode_data_url(data_url))


def admin_redirect_if_needed(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    return None


def admin_url(
    focus: str | None = None,
    notice: str | None = None,
    first: str | None = None,
) -> str:
    params = []
    if focus:
        params.append(f"focus={focus}")
    if notice:
        params.append(f"notice={notice}")
    if first:
        params.append(f"first={first}")
    if not params:
        return "/admin"
    return f"/admin?{'&'.join(params)}"


@router.get("/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/admin/logging", status_code=303)
    show_error = request.query_params.get("error") == "1"
    return templates.TemplateResponse(
        request,
        "admin/login.html",
        {"error": "Ongeldige gebruikersnaam of wachtwoord" if show_error else None},
    )


@router.post("/login")
def admin_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if login(request, username, password):
        return RedirectResponse(url="/admin/logging", status_code=303)
    return RedirectResponse(url="/admin/login?error=1", status_code=303)


@router.post("/logout")
def admin_logout(request: Request):
    logout(request)
    return RedirectResponse(url="/admin/login", status_code=303)


@router.post("/recovery")
def admin_recovery(request: Request):
    redirect = admin_redirect_if_needed(request)
    if redirect:
        return redirect

    hard_refresh_recordings()
    return RedirectResponse(url="/admin/logging?notice=recovery", status_code=303)


@router.get("/logging", response_class=HTMLResponse)
def admin_logging_status(request: Request):
    redirect = admin_redirect_if_needed(request)
    if redirect:
        return redirect

    overview = get_logging_overview()
    recovery_job = None
    from app.scheduler import scheduler

    if scheduler.running:
        recovery_job = scheduler.get_job("hourly_recording_recovery")

    next_recovery = "—"
    if recovery_job and recovery_job.next_run_time:
        next_recovery = recovery_job.next_run_time.strftime("%H:%M:%S")

    return templates.TemplateResponse(
        request,
        "admin/logging.html",
        {
            "overview": overview,
            "date_label": format_dutch_date(),
            "active_nav": "logging",
            "storage": get_storage_status(),
            "next_recovery": next_recovery,
            "notice": request.query_params.get("notice", ""),
        },
    )


@router.get("/storage", response_class=HTMLResponse)
def admin_storage(request: Request):
    redirect = admin_redirect_if_needed(request)
    if redirect:
        return redirect

    stats = get_archive_stats()
    return templates.TemplateResponse(
        request,
        "admin/storage.html",
        {
            "stats": stats,
            "storage": get_storage_status(),
            "preview_except_today": preview_purge("except_today"),
            "date_label": format_dutch_date(),
            "active_nav": "storage",
            "notice": request.query_params.get("notice", ""),
            "purge_deleted": request.query_params.get("deleted", ""),
            "purge_freed_mb": request.query_params.get("freed", ""),
            "error_message": request.query_params.get("error", ""),
        },
    )


@router.post("/storage/purge")
def admin_storage_purge(
    request: Request,
    mode: str = Form(...),
    confirm: str = Form(...),
    older_than_days: str = Form(default="7"),
    before_date: str = Form(default=""),
):
    redirect = admin_redirect_if_needed(request)
    if redirect:
        return redirect

    if confirm.strip().upper() != "VERWIJDEREN":
        return RedirectResponse(
            url="/admin/storage?notice=error&error=Bevestiging%20moet%20VERWIJDEREN%20zijn",
            status_code=303,
        )

    allowed = {"all", "except_today", "older_than_days", "before_date"}
    if mode not in allowed:
        return RedirectResponse(
            url="/admin/storage?notice=error&error=Ongeldige%20modus",
            status_code=303,
        )

    days = None
    if mode == "older_than_days":
        try:
            days = max(1, int(older_than_days))
        except ValueError:
            return RedirectResponse(
                url="/admin/storage?notice=error&error=Ongeldig%20aantal%20dagen",
                status_code=303,
            )

    before = None
    if mode == "before_date":
        if not before_date.strip():
            return RedirectResponse(
                url="/admin/storage?notice=error&error=Kies%20een%20datum",
                status_code=303,
            )
        try:
            before = datetime.fromisoformat(before_date).date()
        except ValueError:
            return RedirectResponse(
                url="/admin/storage?notice=error&error=Ongeldige%20datum",
                status_code=303,
            )

    result = purge_recordings(mode, older_than_days=days, before_date=before)
    return RedirectResponse(
        url=f"/admin/storage?notice=purged&deleted={result['deleted']}&freed={result['freed_mb']}",
        status_code=303,
    )


@router.post("/storage/retention")
def admin_storage_retention(request: Request):
    redirect = admin_redirect_if_needed(request)
    if redirect:
        return redirect

    result = cleanup_expired_recordings()
    return RedirectResponse(
        url=f"/admin/storage?notice=retention&deleted={result['deleted']}",
        status_code=303,
    )


@router.get("", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    redirect = admin_redirect_if_needed(request)
    if redirect:
        return redirect

    return templates.TemplateResponse(
        request,
        "admin/index.html",
        {
            "stations": load_stations(),
            "countries": COUNTRIES,
            "timezone_groups": get_timezone_groups(),
            "default_timezone": DEFAULT_TIMEZONE,
            "default_retention_days": DEFAULT_RETENTION_DAYS,
            "default_event_retention_days": DEFAULT_EVENT_RETENTION_DAYS,
            "date_label": format_dutch_date(),
            "active_nav": "stations",
            "focus_id": request.query_params.get("focus", ""),
            "notice": request.query_params.get("notice", ""),
            "first_recording": request.query_params.get("first", ""),
            "storage": get_storage_status(),
        },
    )


@router.post("/stations")
def admin_create_station(
    request: Request,
    session: Session = Depends(get_session),
    station_id: str = Form(...),
    name: str = Form(...),
    country: str = Form(default="NL"),
    timezone: str = Form(default=DEFAULT_TIMEZONE),
    url: str = Form(...),
    is_event: str | None = Form(default=None),
    event_start_date: str = Form(default=""),
    event_end_date: str = Form(default=""),
    retention_days: str = Form(default=""),
    active: str | None = Form(default=None),
    logo_data: str = Form(default=""),
):
    redirect = admin_redirect_if_needed(request)
    if redirect:
        return redirect

    station_id = station_id.strip().lower()
    first_run = None
    try:
        create_station(
            session,
            station_id=station_id,
            name=name,
            country=country,
            timezone=timezone,
            url=url,
            is_event=is_event == "on",
            event_start_date=event_start_date or None,
            event_end_date=event_end_date or None,
            retention_days=retention_days or None,
            active=active == "on",
        )
        if logo_data:
            _save_logo_from_data_url(station_id, logo_data)
        reload_scheduler()
        first_run = schedule_first_recording(station_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(
        url=admin_url(
            station_id,
            notice="added",
            first=first_run.strftime("%H:%M") if first_run else None,
        ),
        status_code=303,
    )


@router.post("/stations/{station_id}/edit")
def admin_update_station(
    request: Request,
    station_id: str,
    session: Session = Depends(get_session),
    name: str = Form(...),
    country: str = Form(default="NL"),
    timezone: str = Form(default=DEFAULT_TIMEZONE),
    url: str = Form(...),
    is_event: str | None = Form(default=None),
    event_start_date: str = Form(default=""),
    event_end_date: str = Form(default=""),
    retention_days: str = Form(default=""),
    active: str | None = Form(default=None),
    logo_data: str = Form(default=""),
):
    redirect = admin_redirect_if_needed(request)
    if redirect:
        return redirect

    try:
        update_station(
            session,
            station_id=station_id,
            name=name,
            country=country,
            timezone=timezone,
            url=url,
            is_event=is_event == "on",
            event_start_date=event_start_date or None,
            event_end_date=event_end_date or None,
            retention_days=retention_days or None,
            active=active == "on",
        )
        if logo_data:
            _save_logo_from_data_url(station_id, logo_data)
        reload_scheduler()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(url=admin_url(notice="saved"), status_code=303)


@router.post("/stations/{station_id}/delete")
def admin_delete_station(
    request: Request,
    station_id: str,
    session: Session = Depends(get_session),
):
    redirect = admin_redirect_if_needed(request)
    if redirect:
        return redirect

    try:
        delete_station(session, station_id)
        cancel_first_recording(station_id)
        reload_scheduler()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if request.headers.get("X-Requested-With") == "fetch":
        return JSONResponse({"ok": True, "id": station_id})

    return RedirectResponse(url=admin_url(notice="deleted"), status_code=303)


@router.post("/stations/{station_id}/logo")
async def admin_upload_logo(
    request: Request,
    station_id: str,
    session: Session = Depends(get_session),
    logo: UploadFile | None = None,
    logo_data: str = Form(default=""),
):
    redirect = admin_redirect_if_needed(request)
    if redirect:
        return redirect

    if not get_station_model(session, station_id):
        raise HTTPException(status_code=404, detail="Zender niet gevonden")

    if not logo_data and request.headers.get("content-type", "").startswith("application/json"):
        body = LogoUploadBody.model_validate(await request.json())
        logo_data = body.logo_data

    try:
        if logo_data:
            image_bytes = _decode_data_url(logo_data)
        elif logo:
            image_bytes = await logo.read()
        else:
            raise ValueError("Geen logo ontvangen")
        save_station_logo(station_id, image_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if request.headers.get("X-Requested-With") == "fetch":
        return JSONResponse({"ok": True, "logo_url": f"/logos/{station_id}.jpg"})

    return RedirectResponse(url=admin_url(notice="logo"), status_code=303)


@router.get("/website", response_class=HTMLResponse)
def admin_website(request: Request):
    redirect = admin_redirect_if_needed(request)
    if redirect:
        return redirect

    return templates.TemplateResponse(
        request,
        "admin/website.html",
        {
            "site": load_site_settings(),
            "requests": load_stream_requests(),
            "date_label": format_dutch_date(),
            "active_nav": "website",
            "notice": request.query_params.get("notice", ""),
            "storage": get_storage_status(),
        },
    )


@router.post("/website")
def admin_website_save(
    request: Request,
    footer_text: str = Form(...),
    footer_link_url: str = Form(default=""),
    footer_link_label: str = Form(default=""),
):
    redirect = admin_redirect_if_needed(request)
    if redirect:
        return redirect

    save_site_settings(
        {
            "footer_text": footer_text.strip(),
            "footer_link_url": footer_link_url.strip() or "/contact",
            "footer_link_label": footer_link_label.strip() or "Stream aanvragen",
        }
    )
    return RedirectResponse(url="/admin/website?notice=saved", status_code=303)
