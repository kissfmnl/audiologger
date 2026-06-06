import logging
import threading
from contextlib import asynccontextmanager
from urllib.parse import quote
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.database import (
    BASE_DIR,
    RECORDINGS_DIR,
    get_recording_by_id,
    get_recordings,
    get_session,
    init_db,
)
from app.archive_view import build_hour_slots
from app.convert_recordings import convert_wav_recordings
from app.editor import trim_recording
from app.peaks import estimate_duration, read_peaks_fast, warm_missing_peaks
from app.recorder import get_partial_path_for_hour, get_partial_recording_path
from app.scheduler import setup_scheduler, shutdown_scheduler
from app.contact_protection import HONEYPOT_FIELD, issue_contact_form, validate_contact_submission
from app.site_settings import add_stream_request, load_site_settings
from app.stations import load_stations, get_station_by_id
from app.admin_auth import get_session_middleware_kwargs
from app.admin_routes import router as admin_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

DUTCH_WEEKDAYS = (
    "Maandag",
    "Dinsdag",
    "Woensdag",
    "Donderdag",
    "Vrijdag",
    "Zaterdag",
    "Zondag",
)
DUTCH_WEEKDAYS_SHORT = ("zo", "ma", "di", "wo", "do", "vr", "za")
DUTCH_MONTHS = (
    "Jan",
    "Feb",
    "Mrt",
    "Apr",
    "Mei",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Okt",
    "Nov",
    "Dec",
)


def filter_stations_by_country(stations: list[dict], country: str | None) -> list[dict]:
    if country and country.upper() != "ALL":
        return [s for s in stations if s["country"].upper() == country.upper()]
    return stations


def station_today(station: dict) -> date:
    tz = ZoneInfo(station.get("timezone", "Europe/Amsterdam"))
    return datetime.now(tz).date()


def format_date_tab_label(day: date, today: date) -> str:
    month = DUTCH_MONTHS[day.month - 1]
    if day == today:
        return f"Vandaag {day.day:02d} {month}"
    return f"{DUTCH_WEEKDAYS[day.weekday()]} {day.day:02d} {month}"


def format_date_tab_short_label(day: date, today: date) -> str:
    if day == today:
        return "Vandaag"
    return f"{DUTCH_WEEKDAYS_SHORT[day.weekday()]} {day.day:02d}"


def station_url(
    station_id: str,
    day: date | str | None = None,
    country: str | None = None,
    *,
    today: date | None = None,
) -> str:
    url = f"/station/{station_id}"
    if day is not None:
        day_iso = day.isoformat() if isinstance(day, date) else day
        if today is None or day_iso != today.isoformat():
            url = f"{url}/{day_iso}"
    if country and country.upper() != "ALL":
        url = f"{url}?country={country.upper()}"
    return url


def build_date_tabs(station: dict, days: int = 7) -> list[dict]:
    today = station_today(station)
    tabs = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        tabs.append(
            {
                "date": day.isoformat(),
                "label": format_date_tab_short_label(day, today),
                "title": format_date_tab_label(day, today),
            }
        )
    return tabs


def _audio_path_for_slot(station: dict, selected_date: str, slot: dict) -> Path | None:
    recording = slot.get("recording")
    if recording:
        return _recording_audio_path(recording)
    hour_start = datetime.fromisoformat(f"{selected_date}T{slot['hour']:02d}:00:00")
    return get_partial_path_for_hour(station, hour_start)


def _slot_duration(slot: dict, audio_path: Path | None) -> float:
    recording = slot.get("recording")
    if recording and recording.duration_seconds:
        return float(recording.duration_seconds)
    if audio_path and audio_path.exists():
        return estimate_duration(audio_path)
    return 3600.0


def _build_peaks_bootstrap(
    station: dict,
    selected_date: str,
    hour_slots: list[dict],
) -> dict[str, dict]:
    """Metadata only — peaks render instantly client-side, then upgrade via API."""
    bootstrap: dict[str, dict] = {}
    for slot in hour_slots:
        if not slot.get("playable") or not slot.get("peaks_url"):
            continue

        audio_path = _audio_path_for_slot(station, selected_date, slot)
        entry = {
            "duration": _slot_duration(slot, audio_path),
            "ready": False,
            "precise": False,
            "audio_url": slot["audio_url"],
            "peaks_url": slot["peaks_url"],
            "is_live": slot["status"] == "recording",
            "title": f"{station['name']} · {slot['label']}",
            "recording_id": slot.get("recording_id"),
        }
        bootstrap[slot["peaks_url"]] = entry
    return bootstrap


def build_station_page_context(
    station: dict,
    session: Session,
    country: str | None,
    date_filter: str | None,
) -> dict:
    all_stations = load_stations(active_only=False)
    countries = sorted({s["country"] for s in all_stations})
    selected_country = country or "ALL"
    stations = filter_stations_by_country(all_stations, selected_country)

    today = station_today(station)
    selected_date = date_filter or today.isoformat()

    hour_slots = build_hour_slots(station, selected_date, session)

    return {
        "station": station,
        "stations": stations,
        "countries": countries,
        "selected_country": selected_country,
        "selected_date": selected_date,
        "today": today,
        "date_tabs": build_date_tabs(station),
        "hour_slots": hour_slots,
        "peaks_bootstrap": _build_peaks_bootstrap(station, selected_date, hour_slots),
    }


templates.env.globals["station_url"] = station_url


class TrimRequest(BaseModel):
    recording_id: int
    start_sec: float = Field(ge=0)
    end_sec: float = Field(gt=0)


def _run_recording_conversion() -> None:
    try:
        convert_wav_recordings()
    except Exception:
        logger.exception("Background recording conversion failed")


def _run_peaks_warmup() -> None:
    try:
        warm_missing_peaks(RECORDINGS_DIR)
    except Exception:
        logger.exception("Background peaks warmup failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    setup_scheduler()
    threading.Thread(target=_run_recording_conversion, daemon=True).start()
    threading.Thread(target=_run_peaks_warmup, daemon=True).start()
    logger.info("AudioLogger started")
    yield
    shutdown_scheduler()
    logger.info("AudioLogger stopped")


class SiteSettingsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request.state.site = load_site_settings()
        return await call_next(request)


app = FastAPI(title="AudioLogger", lifespan=lifespan)
app.add_middleware(SiteSettingsMiddleware)
app.add_middleware(SessionMiddleware, **get_session_middleware_kwargs())
app.include_router(admin_router)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def recording_to_dict(recording) -> dict:
    file_path = Path(recording.file_path)
    filename = file_path.name if file_path.name else ""
    return {
        "id": recording.id,
        "station_id": recording.station_id,
        "station_name": recording.station_name,
        "country": recording.country,
        "start_time": recording.start_time.isoformat(),
        "end_time": recording.end_time.isoformat(),
        "duration_seconds": recording.duration_seconds,
        "file_path": recording.file_path,
        "filename": filename,
        "file_size_mb": recording.file_size_mb,
        "status": recording.status,
        "peaks_file": recording.peaks_file,
    }


@app.get("/contact", response_class=HTMLResponse)
def contact_page(
    request: Request,
    sent: int = Query(default=0),
    error: str = Query(default=""),
):
    csrf_token = issue_contact_form(request)
    return templates.TemplateResponse(
        request,
        "contact.html",
        {
            "sent": bool(sent),
            "error": error,
            "csrf_token": csrf_token,
            "honeypot_field": HONEYPOT_FIELD,
        },
    )


@app.post("/contact")
def contact_submit(
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    email: str = Form(default=""),
    station_name: str = Form(...),
    stream_url: str = Form(...),
    message: str = Form(default=""),
    company_website: str = Form(default=""),
):
    blocked = validate_contact_submission(
        request,
        csrf_token=csrf_token,
        honeypot=company_website,
        name=name.strip(),
        email=email.strip(),
        station_name=station_name.strip(),
        stream_url=stream_url.strip(),
        message=message.strip(),
    )
    if blocked:
        return RedirectResponse(url=f"/contact?error={quote(blocked)}", status_code=303)

    add_stream_request(
        {
            "name": name.strip(),
            "email": email.strip(),
            "station_name": station_name.strip(),
            "stream_url": stream_url.strip(),
            "message": message.strip(),
        }
    )
    return RedirectResponse(url="/contact?sent=1", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    country: str | None = Query(default=None),
):
    stations = filter_stations_by_country(load_stations(active_only=False), country)
    if not stations:
        return templates.TemplateResponse(request, "dashboard.html", {})

    first = stations[0]
    url = station_url(first["id"], today=station_today(first), country=country)
    return RedirectResponse(url=url, status_code=302)


@app.get("/station/{station_id}", response_class=HTMLResponse)
@app.get("/station/{station_id}/{path_date}", response_class=HTMLResponse)
def station_page(
    request: Request,
    station_id: str,
    path_date: str | None = None,
    legacy_date: str | None = Query(default=None, alias="date"),
    country: str | None = Query(default=None),
    session: Session = Depends(get_session),
):
    station = get_station_by_id(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")

    today = station_today(station)
    selected_country = country or "ALL"

    if legacy_date is not None and path_date is None:
        try:
            legacy_day = date.fromisoformat(legacy_date)
        except ValueError:
            raise HTTPException(status_code=404, detail="Invalid date")
        url = station_url(station_id, legacy_day, selected_country, today=today)
        return RedirectResponse(url=url, status_code=301)

    date_filter: str | None = None
    if path_date is not None:
        try:
            date_filter = date.fromisoformat(path_date).isoformat()
        except ValueError:
            raise HTTPException(status_code=404, detail="Invalid date")

    filtered = filter_stations_by_country(load_stations(active_only=False), selected_country)
    if filtered and station_id not in {s["id"] for s in filtered}:
        target = filtered[0]["id"]
        target_today = station_today(target)
        day = date_filter or today.isoformat()
        url = station_url(target, day, selected_country, today=target_today)
        return RedirectResponse(url=url, status_code=302)

    try:
        context = build_station_page_context(station, session, selected_country, date_filter)
    except Exception as exc:
        logger.exception("Station page failed for %s: %s", station_id, exc)
        raise HTTPException(status_code=500, detail="Kon deze datum niet laden") from exc

    return templates.TemplateResponse(request, "station.html", context)


@app.get("/player/{recording_id}", response_class=HTMLResponse)
def player_page(
    request: Request,
    recording_id: int,
    session: Session = Depends(get_session),
):
    recording = get_recording_by_id(session, recording_id)
    if not recording:
        raise HTTPException(status_code=404, detail="Recording not found")

    if recording.status not in ("completed", "recording"):
        raise HTTPException(status_code=400, detail="Recording is not available for playback")

    if recording.status == "completed":
        file_path = Path(recording.file_path)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Recording file not found on disk")
        audio_url = f"/recordings/{file_path.name}"
    else:
        partial = get_partial_recording_path(recording)
        if not partial:
            raise HTTPException(status_code=404, detail="Partial recording not available yet")
        audio_url = f"/recordings/live/{recording.id}"

    station = get_station_by_id(recording.station_id)
    rec_day = recording.start_time.date() if recording.start_time else None
    today = station_today(station) if station else date.today()
    back_url = station_url(recording.station_id, rec_day, today=today) if station else "/"

    return templates.TemplateResponse(
        request,
        "player.html",
        {
            "recording": recording,
            "station": station,
            "audio_url": audio_url,
            "is_live": recording.status == "recording",
            "back_url": back_url,
        },
    )


def _recording_audio_path(recording) -> Path | None:
    if recording.status == "completed":
        path = Path(recording.file_path)
        return path if path.exists() else None
    return get_partial_recording_path(recording)


def _recording_audio_url(recording) -> str:
    if recording.status == "completed":
        return f"/recordings/{Path(recording.file_path).name}"
    return f"/recordings/live/{recording.id}"


@app.get("/api/peaks/{recording_id}")
def api_peaks(
    recording_id: int,
    wait: float = Query(default=0, ge=0, le=10),
    session: Session = Depends(get_session),
):
    recording = get_recording_by_id(session, recording_id)
    if not recording or recording.status not in ("completed", "recording"):
        raise HTTPException(status_code=404, detail="Recording not found")

    audio_path = _recording_audio_path(recording)
    if not audio_path:
        raise HTTPException(status_code=404, detail="Audio file not found")

    peak_data = read_peaks_fast(audio_path, max_wait=wait)
    payload = {
        "peaks": peak_data["peaks"],
        "duration": peak_data["duration"],
        "ready": peak_data["ready"],
        "precise": peak_data["precise"],
        "audio_url": _recording_audio_url(recording),
        "is_live": recording.status == "recording",
        "title": f"{recording.station_name} · {recording.start_time.strftime('%d-%m-%Y %H:%M')}",
        "recording_id": recording.id,
    }
    cache_header = (
        {"Cache-Control": "no-store"}
        if recording.status == "recording"
        else {"Cache-Control": "public, max-age=86400"}
    )
    return JSONResponse(content=payload, headers=cache_header)


@app.get("/api/peaks/hour/{station_id}")
def api_peaks_hour(
    station_id: str,
    date: str = Query(...),
    hour: int = Query(..., ge=0, le=23),
    wait: float = Query(default=0, ge=0, le=10),
    session: Session = Depends(get_session),
):
    station = get_station_by_id(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")

    hour_start = datetime.fromisoformat(f"{date}T{hour:02d}:00:00")
    audio_path = get_partial_path_for_hour(station, hour_start)
    if not audio_path:
        raise HTTPException(status_code=404, detail="Audio file not found")

    peak_data = read_peaks_fast(audio_path, max_wait=wait)
    return JSONResponse(
        content={
            "peaks": peak_data["peaks"],
            "duration": peak_data["duration"],
            "ready": peak_data["ready"],
            "precise": peak_data["precise"],
            "audio_url": f"/recordings/live-hour/{station_id}?date={date}&hour={hour}",
            "is_live": True,
            "title": f"{station['name']} · {hour_start.strftime('%d-%m-%Y %H:%M')}",
            "recording_id": None,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/recordings")
def api_recordings(
    station_id: str | None = Query(default=None),
    date: str | None = Query(default=None),
    session: Session = Depends(get_session),
):
    recordings = get_recordings(session, station_id=station_id, date_filter=date)
    return [recording_to_dict(r) for r in recordings]


@app.post("/api/trim")
def api_trim(body: TrimRequest, session: Session = Depends(get_session)):
    recording = get_recording_by_id(session, body.recording_id)
    if not recording:
        raise HTTPException(status_code=404, detail="Recording not found")

    if recording.status != "completed":
        raise HTTPException(status_code=400, detail="Cannot trim a failed recording")

    input_path = Path(recording.file_path)
    if not input_path.exists():
        raise HTTPException(status_code=404, detail="Recording file not found on disk")

    try:
        output_path = trim_recording(input_path, body.start_sec, body.end_sec)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return FileResponse(
        path=output_path,
        media_type="audio/mpeg",
        filename=output_path.name,
        headers={"Content-Disposition": f'attachment; filename="{output_path.name}"'},
    )


@app.get("/logos/{filename}")
def serve_logo(filename: str):
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    from app.database import LOGOS_DIR

    file_path = LOGOS_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Logo not found")

    return FileResponse(path=file_path, media_type="image/jpeg")


@app.get("/recordings/live/{recording_id}")
def serve_live_recording(recording_id: int, session: Session = Depends(get_session)):
    recording = get_recording_by_id(session, recording_id)
    if not recording or recording.status != "recording":
        raise HTTPException(status_code=404, detail="Live recording not found")

    file_path = get_partial_recording_path(recording)
    if not file_path or not file_path.exists():
        raise HTTPException(status_code=404, detail="Partial recording not available yet")

    return FileResponse(
        path=file_path,
        media_type="audio/mpeg",
        filename=file_path.name,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/recordings/live-hour/{station_id}")
def serve_live_hour(
    station_id: str,
    date: str = Query(...),
    hour: int = Query(..., ge=0, le=23),
):
    station = get_station_by_id(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")

    hour_start = datetime.fromisoformat(f"{date}T{hour:02d}:00:00")
    file_path = get_partial_path_for_hour(station, hour_start)
    if not file_path or not file_path.exists():
        raise HTTPException(status_code=404, detail="Partial recording not available yet")

    return FileResponse(
        path=file_path,
        media_type="audio/mpeg",
        filename=file_path.name,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/recordings/{filename}")
def serve_recording(filename: str):
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    file_path = RECORDINGS_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        path=file_path,
        media_type="audio/mpeg",
        filename=filename,
    )
