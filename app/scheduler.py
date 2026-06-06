import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.recorder import has_completed_recording, record_station
from app.retention import cleanup_expired_recordings
from app.stations import get_station_by_id, load_stations, recording_start_time, should_record_station

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()

RECORDING_MINUTE = 2
RETRY_DELAYS_MINUTES = [8, 18, 35]


def scheduled_record(station_id: str, retry_round: int = 0) -> None:
    station = get_station_by_id(station_id)
    if not station:
        logger.warning("Station %s not found, skipping recording", station_id)
        return

    if not should_record_station(station):
        logger.info("Skipped recording for %s (outside schedule/event window)", station_id)
        return

    start_time = recording_start_time(station)

    if has_completed_recording(station_id, start_time):
        logger.info("Hour already recorded for %s at %s", station_id, start_time)
        return

    try:
        recording = record_station(station, start_time)
        logger.info(
            "Recorded %s -> %s (status: %s)",
            station_id,
            recording.file_path,
            recording.status,
        )
    except Exception as exc:
        logger.warning("Recording failed for %s (round %d): %s", station_id, retry_round, exc)
        if retry_round < len(RETRY_DELAYS_MINUTES):
            schedule_hour_retry(station_id, start_time, retry_round + 1)


def schedule_hour_retry(station_id: str, start_time: datetime, retry_round: int) -> None:
    delay = RETRY_DELAYS_MINUTES[retry_round - 1]
    run_at = datetime.now() + timedelta(minutes=delay)
    hour_end = start_time + timedelta(hours=1)

    if run_at >= hour_end - timedelta(minutes=2):
        logger.warning(
            "No more retries for %s at %s (too close to hour end)",
            station_id,
            start_time,
        )
        return

    job_id = f"record_retry_{station_id}_{start_time.strftime('%Y%m%d%H')}_{retry_round}"
    scheduler.add_job(
        scheduled_record,
        trigger=DateTrigger(run_date=run_at),
        args=[station_id],
        kwargs={"retry_round": retry_round},
        id=job_id,
        replace_existing=True,
    )
    logger.info(
        "Scheduled retry %d for %s at %s (in %d min)",
        retry_round,
        station_id,
        run_at.strftime("%H:%M"),
        delay,
    )


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
    run_at = run_at.replace(minute=RECORDING_MINUTE, second=0, microsecond=0)
    tz = ZoneInfo(station["timezone"])
    now = datetime.now(tz)
    if run_at <= now:
        run_at = run_at + timedelta(hours=1)

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
        for job in list(scheduler.get_jobs()):
            if job.id.startswith("record_"):
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
            trigger=CronTrigger(minute=RECORDING_MINUTE, hour="*", timezone=tz),
            args=[station["id"]],
            id=f"record_{station['id']}",
            replace_existing=True,
            misfire_grace_time=900,
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            "Scheduled %s (%s) — %s",
            station["name"],
            station["id"],
            station["schedule_label"],
        )

    scheduler.add_job(
        cleanup_expired_recordings,
        trigger=CronTrigger(hour=4, minute=0),
        id="cleanup_recordings",
        replace_existing=True,
    )

    return scheduler


def setup_scheduler() -> BackgroundScheduler:
    scheduler_result = reload_scheduler()
    cleanup_expired_recordings()
    return scheduler_result


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
