from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app.database import engine
from app.models import Recording
from app.scheduler import scheduler
from app.stations import load_stations, should_record_station


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

    items = []
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

        items.append(
            {
                "station": station,
                "state": state,
                "detail": detail,
                "will_log": will_log,
                "scheduled": job is not None,
                "next_run": next_run.strftime("%d-%m-%Y %H:%M") if next_run else "—",
                "last_time": last.start_time.strftime("%d-%m-%Y %H:%M") if last else "—",
                "last_status": last.status if last else None,
            }
        )

    items.sort(key=lambda row: (0 if row["state"] in ("error", "warning") else 1, row["station"]["name"]))

    return {
        "items": items,
        "logging_count": logging_count,
        "skipped_count": skipped_count,
        "issue_count": issue_count,
        "scheduler_running": scheduler.running,
        "total": len(stations),
    }
