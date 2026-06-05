import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.recorder import record_station
from app.stations import get_station_by_id, load_stations, recording_start_time, should_record_station

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()


def scheduled_record(station_id: str) -> None:
    station = get_station_by_id(station_id)
    if not station:
        logger.warning("Station %s not found, skipping recording", station_id)
        return

    if not should_record_station(station):
        logger.info("Skipped recording for %s (outside schedule/event window)", station_id)
        return

    start_time = recording_start_time(station)
    try:
        recording = record_station(station, start_time)
        logger.info(
            "Recorded %s -> %s (status: %s)",
            station_id,
            recording.file_path,
            recording.status,
        )
    except Exception:
        logger.exception("Scheduled recording failed for %s", station_id)


def next_whole_hour(station: dict, moment: datetime | None = None) -> datetime:
    tz = ZoneInfo(station.get("timezone", "Europe/Amsterdam"))
    now = moment.astimezone(tz) if moment and moment.tzinfo else datetime.now(tz)
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    if now > hour_start:
        return hour_start + timedelta(hours=1)
    return hour_start


def cancel_first_recording(station_id: str) -> None:
    job_id = f"record_first_{station_id}"
    if scheduler.running and scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def schedule_first_recording(station_id: str) -> datetime | None:
    station = get_station_by_id(station_id)
    if not station or not station.get("active"):
        return None
    if not should_record_station(station):
        return None

    run_at = next_whole_hour(station)
    tz = ZoneInfo(station["timezone"])

    scheduler.add_job(
        scheduled_record,
        trigger=DateTrigger(run_date=run_at),
        args=[station_id],
        id=f"record_first_{station_id}",
        replace_existing=True,
    )
    logger.info(
        "First recording for %s scheduled at %s (%s)",
        station_id,
        run_at.strftime("%d-%m-%Y %H:%M"),
        station["timezone"],
    )
    return run_at.astimezone(tz).replace(tzinfo=None)


def reload_scheduler() -> BackgroundScheduler:
    stations = load_stations(active_only=True)

    if scheduler.running:
        for job in scheduler.get_jobs():
            if job.id.startswith("record_") and not job.id.startswith("record_first_"):
                scheduler.remove_job(job.id)
    else:
        scheduler.start()

    for station in stations:
        tz_name = station.get("timezone", "Europe/Amsterdam")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            logger.error("Invalid timezone for %s: %s", station["id"], tz_name)
            tz = ZoneInfo("Europe/Amsterdam")

        scheduler.add_job(
            scheduled_record,
            trigger=CronTrigger(minute=0, hour="*", timezone=tz),
            args=[station["id"]],
            id=f"record_{station['id']}",
            replace_existing=True,
            misfire_grace_time=300,
        )
        logger.info(
            "Scheduled %s (%s) — %s",
            station["name"],
            station["id"],
            station["schedule_label"],
        )

    return scheduler


def setup_scheduler() -> BackgroundScheduler:
    return reload_scheduler()


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
