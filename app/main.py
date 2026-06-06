import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.database import (
    BASE_DIR,
    RECORDINGS_DIR,
    get_global_stats,
    get_recording_by_id,
    get_recordings,
    get_session,
    get_station_stats,
    init_db,
)
from app.convert_recordings import convert_wav_recordings
from app.editor import trim_recording
from app.scheduler import setup_scheduler, shutdown_scheduler
from app.stations import load_stations, get_station_by_id
from app.admin_auth import get_session_middleware_kwargs
from app.admin_routes import router as admin_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class TrimRequest(BaseModel):
    recording_id: int
    start_sec: float = Field(ge=0)
    end_sec: float = Field(gt=0)


def _run_recording_conversion() -> None:
    try:
        convert_wav_recordings()
    except Exception:
        logger.exception("Background recording conversion failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    setup_scheduler()
    threading.Thread(target=_run_recording_conversion, daemon=True).start()
    logger.info("AudioLogger started")
    yield
    shutdown_scheduler()
    logger.info("AudioLogger stopped")


app = FastAPI(title="AudioLogger", lifespan=lifespan)
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
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    country: str | None = Query(default=None),
    session: Session = Depends(get_session),
):
    stations = load_stations(active_only=False)

    if country and country.upper() != "ALL":
        stations = [s for s in stations if s["country"].upper() == country.upper()]

    station_cards = []
    for station in stations:
        stats = get_station_stats(session, station["id"])
        last = stats["last_recording"]
        station_cards.append(
            {
                **station,
                "total_recordings": stats["total_recordings"],
                "total_storage_mb": stats["total_storage_mb"],
                "last_recording_date": last.start_time.strftime("%d-%m-%Y %H:%M")
                if last
                else "Geen opnames",
            }
        )

    global_stats = get_global_stats(session)
    all_stations = load_stations(active_only=False)
    countries = sorted({s["country"] for s in all_stations})

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "stations": station_cards,
            "global_stats": global_stats,
            "countries": countries,
            "selected_country": country or "ALL",
        },
    )


@app.get("/station/{station_id}", response_class=HTMLResponse)
def station_page(
    request: Request,
    station_id: str,
    date: str | None = Query(default=None),
    session: Session = Depends(get_session),
):
    station = get_station_by_id(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")

    recordings = get_recordings(session, station_id=station_id, date_filter=date)
    stats = get_station_stats(session, station_id)

    return templates.TemplateResponse(
        request,
        "station.html",
        {
            "station": station,
            "recordings": recordings,
            "stats": stats,
            "date_filter": date or "",
        },
    )


@app.get("/player/{recording_id}", response_class=HTMLResponse)
def player_page(
    request: Request,
    recording_id: int,
    session: Session = Depends(get_session),
):
    recording = get_recording_by_id(session, recording_id)
    if not recording:
        raise HTTPException(status_code=404, detail="Recording not found")

    if recording.status != "completed":
        raise HTTPException(status_code=400, detail="Recording is not available for playback")

    file_path = Path(recording.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Recording file not found on disk")

    station = get_station_by_id(recording.station_id)

    return templates.TemplateResponse(
        request,
        "player.html",
        {
            "recording": recording,
            "station": station,
            "audio_url": f"/recordings/{file_path.name}",
        },
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
