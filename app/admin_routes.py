import base64
import re
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from app.admin_auth import is_authenticated, login, logout
from app.database import BASE_DIR, get_session
from app.scheduler import get_scheduler_jobs, reload_scheduler
from app.stations import (
    create_station,
    delete_station,
    format_schedule_label,
    get_station_model,
    load_stations,
    save_station_logo,
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


@router.get("/login", response_class=HTMLResponse)
def admin_login_page(request: Request, error: str | None = None):
    if is_authenticated(request):
        return RedirectResponse(url="/admin", status_code=303)
    show_error = error == "1" or request.query_params.get("error") == "1"
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
        return RedirectResponse(url="/admin", status_code=303)
    return RedirectResponse(url="/admin/login?error=1", status_code=303)


@router.post("/logout")
def admin_logout(request: Request):
    logout(request)
    return RedirectResponse(url="/admin/login", status_code=303)


@router.get("", response_class=HTMLResponse)
def admin_dashboard(request: Request, session: Session = Depends(get_session)):
    redirect = admin_redirect_if_needed(request)
    if redirect:
        return redirect

    stations = load_stations()
    for station in stations:
        station["schedule_label"] = format_schedule_label(station["schedule_hours"])

    return templates.TemplateResponse(
        request,
        "admin/index.html",
        {
            "stations": stations,
            "scheduler_jobs": get_scheduler_jobs(),
            "date_label": format_dutch_date(),
            "active_nav": "stations",
        },
    )


@router.post("/stations")
def admin_create_station(
    request: Request,
    session: Session = Depends(get_session),
    station_id: str = Form(...),
    name: str = Form(...),
    country: str = Form(...),
    url: str = Form(...),
    flag: str = Form(default=""),
    schedule_mode: str = Form(default="hourly"),
    schedule_hours: list[str] = Form(default=[]),
    active: str | None = Form(default=None),
    logo_data: str = Form(default=""),
):
    redirect = admin_redirect_if_needed(request)
    if redirect:
        return redirect

    hours = "*" if schedule_mode == "hourly" else schedule_hours

    try:
        create_station(
            session,
            station_id=station_id.strip().lower(),
            name=name,
            country=country,
            url=url,
            schedule_hours=hours,
            flag=flag or None,
            active=active == "on",
        )
        if logo_data:
            _save_logo_from_data_url(station_id.strip().lower(), logo_data)
        reload_scheduler()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(url="/admin", status_code=303)


@router.post("/stations/{station_id}/edit")
def admin_update_station(
    request: Request,
    station_id: str,
    session: Session = Depends(get_session),
    name: str = Form(...),
    country: str = Form(...),
    url: str = Form(...),
    flag: str = Form(default=""),
    schedule_mode: str = Form(default="hourly"),
    schedule_hours: list[str] = Form(default=[]),
    active: str | None = Form(default=None),
    logo_data: str = Form(default=""),
):
    redirect = admin_redirect_if_needed(request)
    if redirect:
        return redirect

    hours = "*" if schedule_mode == "hourly" else schedule_hours

    try:
        update_station(
            session,
            station_id=station_id,
            name=name,
            country=country,
            url=url,
            schedule_hours=hours,
            flag=flag or None,
            active=active == "on",
        )
        if logo_data:
            _save_logo_from_data_url(station_id, logo_data)
        reload_scheduler()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(url="/admin", status_code=303)


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
        reload_scheduler()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return RedirectResponse(url="/admin", status_code=303)


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

    station = get_station_model(session, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Zender niet gevonden")

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

    return RedirectResponse(url="/admin", status_code=303)


def _decode_data_url(data_url: str) -> bytes:
    match = re.match(r"^data:image/(jpeg|jpg|png);base64,(.+)$", data_url, re.DOTALL)
    if not match:
        raise ValueError("Ongeldig logo-formaat")
    return base64.b64decode(match.group(2))


def _save_logo_from_data_url(station_id: str, data_url: str) -> None:
    save_station_logo(station_id, _decode_data_url(data_url))
