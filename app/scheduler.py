import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.recorder import record_station
from app.stations import load_stations, recording_start_time, should_record_station

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()


def scheduled_record(station: dict) -> None:
    if not should_record_station(station):
        logger.info("Skipped recording for %s (outside schedule/event window)", station["id"])
        return

    start_time = recording_start_time(station)
    try:
        recording = record_station(station, start_time)
        logger.info(
            "Recorded %s -> %s (status: %s)",
            station["id"],
            recording.file_path,
            recording.status,
        )
    except Exception:
        logger.exception("Scheduled recording failed for %s", station["id"])


def reload_scheduler() -> BackgroundScheduler:
    stations = load_stations(active_only=True)

    if scheduler.running:
        for job in scheduler.get_jobs():
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
            args=[station],
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


def get_scheduler_jobs() -> list[dict]:
    jobs = []
    for job in scheduler.get_jobs():
        next_run = job.next_run_time
        jobs.append(
            {
                "id": job.id,
                "name": job.name or job.id,
                "next_run": next_run.strftime("%d-%m-%Y %H:%M") if next_run else "—",
            }
        )
    return jobs
