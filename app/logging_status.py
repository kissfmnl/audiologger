from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app.database import engine
from app.models import Recording
from app.peaks import get_audio_duration
from app.scheduler import scheduler
from app.stations import load_stations, retention_label, should_record_station

HOUR_DURATION_TARGET = 3600
HOUR_DURATION_TOLERANCE = 45


def skip_reason(station: dict) -> str | None:
    if not station.get("active"):
        return "Zender uitgeschakeld"

    if not station.get("is_event"):
        return None

    start_raw = station.get("event_start_date")
    end_raw = station.get("event_end_date")
    if not start_raw or not end_raw:
        return "Event zonder start- of einddatum"

    tz = ZoneInfo(station.get("timezone", "Europe/Amsterdam"))
    today = datetime.now(tz).date()
    start = date.fromisoformat(start_raw)
    end = date.fromisoformat(end_raw)

    if today < start:
        return f"Event start op {start.strftime('%d-%m-%Y')}"
    if today > end:
        return f"Event afgelopen op {end.strftime('%d-%m-%Y')}"
    return None


def _recording_duration_seconds(recording: Recording) -> int:
    if recording.duration_seconds and recording.duration_seconds > 0:
        return recording.duration_seconds
    path = Path(recording.file_path)
    if path.exists() and path.stat().st_size > 0:
        return int(get_audio_duration(path))
    return 0


def get_hour_duration_audit(days_back: int = 3) -> dict:
    """Controleer of afgeronde uur-opnames ~60 minuten duren."""
    tz = ZoneInfo("Europe/Amsterdam")
    cutoff = datetime.now(tz).replace(tzinfo=None) - timedelta(days=days_back)

    with Session(engine) as session:
        recordings = session.exec(
            select(Recording)
            .where(
                Recording.status == "completed",
                Recording.start_time >= cutoff,
            )
            .order_by(Recording.start_time.desc())
        ).all()

    issues = []
    ok_count = 0

    for recording in recordings:
        duration = _recording_duration_seconds(recording)
        delta = duration - HOUR_DURATION_TARGET

        if abs(delta) <= HOUR_DURATION_TOLERANCE:
            ok_count += 1
            continue

        if delta > 0:
            problem = "too_long"
            label = f"{duration // 60}m {duration % 60}s — te lang"
        else:
            problem = "too_short"
            label = f"{duration // 60}m {duration % 60}s — te kort"

        issues.append(
            {
                "station_name": recording.station_name,
                "station_id": recording.station_id,
                "start_time": recording.start_time.strftime("%d-%m-%Y %H:%M"),
                "duration_seconds": duration,
                "problem": problem,
                "label": label,
            }
        )

    return {
        "checked": len(recordings),
        "ok_count": ok_count,
        "issue_count": len(issues),
        "issues": issues[:25],
        "days_back": days_back,
        "target_minutes": HOUR_DURATION_TARGET // 60,
    }


def get_logging_overview() -> dict:
    stations = load_stations()

    with Session(engine) as session:
        recordings = session.exec(
            select(Recording).order_by(Recording.start_time.desc())
        ).all()

    last_by_station: dict[str, Recording] = {}
    for recording in recordings:
        if recording.station_id not in last_by_station:
            last_by_station[recording.station_id] = recording

    rows = []
    logging_count = 0
    skipped_count = 0
    issue_count = 0

    for station in stations:
        station_id = station["id"]
        will_log = should_record_station(station)
        reason = skip_reason(station)
        job = scheduler.get_job(f"record_{station_id}") if scheduler.running else None
        next_run = job.next_run_time if job else None
        last = last_by_station.get(station_id)

        if will_log:
            logging_count += 1
            state = "logging"
            detail = "Actief — opname elk heel uur"
            if not job:
                state = "warning"
                detail = "Zou moeten loggen, maar geen scheduler-job gevonden"
                issue_count += 1
            elif last and last.status == "failed":
                state = "error"
                detail = f"Laatste opname mislukt ({last.start_time.strftime('%d-%m-%Y %H:%M')})"
                issue_count += 1
            elif not last:
                state = "pending"
                detail = "Nog geen opnames — wacht op volgend heel uur"
        else:
            skipped_count += 1
            state = "skipped"
            detail = reason or "Wordt niet opgenomen"

        rows.append(
            {
                "station": station,
                "state": state,
                "detail": detail,
                "retention_label": retention_label(station),
                "will_log": will_log,
                "scheduled": job is not None,
                "next_run": next_run.strftime("%d-%m-%Y %H:%M") if next_run else "—",
                "last_time": last.start_time.strftime("%d-%m-%Y %H:%M") if last else "—",
                "last_status": last.status if last else None,
            }
        )

    rows.sort(key=lambda row: (0 if row["state"] in ("error", "warning") else 1, row["station"]["name"]))

    return {
        "rows": rows,
        "logging_count": logging_count,
        "skipped_count": skipped_count,
        "issue_count": issue_count,
        "scheduler_running": scheduler.running,
        "total": len(stations),
    }
