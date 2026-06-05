import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.recorder import record_station
from app.stations import hours_to_cron, load_stations

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()


def scheduled_record(station: dict) -> None:
    start_time = datetime.now().replace(minute=0, second=0, microsecond=0)
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
        cron_expr = hours_to_cron(station["schedule_hours"])
        parts = cron_expr.split()
        if len(parts) != 5:
            logger.error(
                "Invalid cron expression for %s: %s",
                station["id"],
                cron_expr,
            )
            continue

        minute, hour, day, month, day_of_week = parts

        scheduler.add_job(
            scheduled_record,
            trigger=CronTrigger(
                minute=minute,
                hour=hour,
                day=day,
                month=month,
                day_of_week=day_of_week,
            ),
            args=[station],
            id=f"record_{station['id']}",
            replace_existing=True,
            misfire_grace_time=300,
        )
        logger.info(
            "Scheduled %s (%s) at whole hours: %s",
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
