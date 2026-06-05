import logging
from datetime import datetime
from pathlib import Path

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.database import BASE_DIR
from app.recorder import record_station

logger = logging.getLogger(__name__)

STATIONS_CONFIG = BASE_DIR / "config" / "stations.yaml"
scheduler = BackgroundScheduler()


def load_stations() -> list[dict]:
    if not STATIONS_CONFIG.exists():
        raise FileNotFoundError(f"Stations config not found: {STATIONS_CONFIG}")

    with open(STATIONS_CONFIG, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    stations = data.get("stations", [])
    return [s for s in stations if s.get("active", True)]


def get_station_by_id(station_id: str) -> dict | None:
    for station in load_stations():
        if station["id"] == station_id:
            return station
    return None


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


def setup_scheduler() -> BackgroundScheduler:
    if scheduler.running:
        return scheduler

    stations = load_stations()

    for station in stations:
        cron_expr = station.get("schedule", "0 * * * *")
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
            "Scheduled %s (%s) with cron: %s",
            station["name"],
            station["id"],
            cron_expr,
        )

    scheduler.start()
    return scheduler


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
