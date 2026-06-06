from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from app.admin_auth import is_authenticated, login, logout
from app.database import BASE_DIR, get_session, get_storage_status
from app.logging_status import get_logging_overview
from app.scheduler import (
    cancel_first_recording,
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
    get_timezone_groups,
    load_stations,
    update_station,
)

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


DUTCH_DAYS = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
DUTCH_MONTHS = [
    "januari", "februari", "maart", "april", "mei", "juni",
    "juli", "augustus", "september", "oktober", "november", "december",
]


def format_dutch_date(dt: datetime | None = None) -> str:
    dt = dt or datetime.now()
    return f"{DUTCH_DAYS[dt.weekday()]} {dt.day} {DUTCH_MONTHS[dt.month - 1]} {dt.year}"


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


@router.get("/logging", response_class=HTMLResponse)
def admin_logging_status(request: Request):
    redirect = admin_redirect_if_needed(request)
    if redirect:
        return redirect

    overview = get_logging_overview()
    return templates.TemplateResponse(
        request,
        "admin/logging.html",
        {
            "overview": overview,
            "date_label": format_dutch_date(),
            "active_nav": "logging",
            "storage": get_storage_status(),
        },
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
