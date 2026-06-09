import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlmodel import Session, select

from app.database import engine
from app.models import Recording
from app.recorder import (
    finalize_all_stale_recordings,
    has_completed_recording,
    invalidate_short_completed_recordings,
    is_hour_actively_recording,
    record_station,
)
from app.retention import cleanup_expired_recordings
from app.dropbox_upload import retry_pending_dropbox_uploads
from app.stations import get_station_by_id, load_stations, should_record_station

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()
_recording_executor = ThreadPoolExecutor(max_workers=40, thread_name_prefix="record")

RETRY_DELAYS_SECONDS = (90, 180, 300)


def resolve_recording_hour(station: dict, force_start_time: str | None = None) -> datetime:
    """Vast uur voor deze opname — niet opnieuw berekenen na wachten op een slot."""
    if force_start_time:
        return datetime.fromisoformat(force_start_time)
    tz = ZoneInfo(station.get("timezone", "Europe/Amsterdam"))
    now = datetime.now(tz)
    return now.replace(minute=0, second=0, microsecond=0, tzinfo=None)


def _schedule_recording_retry(station: dict, start_time: datetime, attempt: int) -> None:
    if attempt >= len(RETRY_DELAYS_SECONDS):
        return

    tz = ZoneInfo(station.get("timezone", "Europe/Amsterdam"))
    now = datetime.now(tz)
    hour_end = start_time.replace(tzinfo=tz) + timedelta(hours=1)
    if now >= hour_end + timedelta(minutes=15):
        return

    run_at = now + timedelta(seconds=RETRY_DELAYS_SECONDS[attempt])
    job_id = f"retry_{station['id']}_{start_time.strftime('%Y%m%d%H')}"
    scheduler.add_job(
        scheduled_record,
        trigger=DateTrigger(run_date=run_at),
        args=[station["id"]],
        kwargs={
            "force_start_time": start_time.isoformat(),
            "attempt": attempt + 1,
        },
        id=job_id,
        replace_existing=True,
    )
    logger.info(
        "Retry %s for %s at %s scheduled in %ss",
        attempt + 1,
        station["id"],
        start_time.strftime("%Y-%m-%d %H:%M"),
        RETRY_DELAYS_SECONDS[attempt],
    )


def _run_scheduled_record(station_id: str, start_time: datetime, attempt: int) -> None:
    station = get_station_by_id(station_id)
    if not station:
        logger.warning("Station %s not found, skipping recording", station_id)
        return

    if not should_record_station(station):
        logger.info("Skipped recording for %s (outside schedule/event window)", station_id)
        return

    if has_completed_recording(station_id, start_time):
        logger.info("Hour already recorded for %s at %s", station_id, start_time)
        return

    try:
        recording = record_station(station, start_time)
        logger.info(
            "Recorded %s -> %s (status: %s, %ss)",
            station_id,
            recording.file_path,
            recording.status,
            recording.duration_seconds,
        )
    except Exception as exc:
        logger.warning("Recording failed for %s: %s", station_id, exc)
        if not has_completed_recording(station_id, start_time):
            _schedule_recording_retry(station, start_time, attempt)


def scheduled_record(
    station_id: str,
    force_start_time: str | None = None,
    attempt: int = 0,
) -> None:
    station = get_station_by_id(station_id)
    if not station:
        logger.warning("Station %s not found, skipping recording", station_id)
        return

    start_time = resolve_recording_hour(station, force_start_time)
    _recording_executor.submit(_run_scheduled_record, station_id, start_time, attempt)


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

    tz = ZoneInfo(station["timezone"])
    run_at = next_whole_hour(station)
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


def _queue_record(
    station_id: str,
    start_time: datetime,
    delay_seconds: int = 0,
    job_id: str | None = None,
) -> None:
    if not scheduler.running:
        return
    station = get_station_by_id(station_id)
    if not station:
        return
    tz = ZoneInfo(station.get("timezone", "Europe/Amsterdam"))
    run_at = datetime.now(tz) + timedelta(seconds=delay_seconds)
    if job_id is None:
        job_id = f"queue_{station_id}_{start_time.strftime('%Y%m%d%H')}"
    scheduler.add_job(
        scheduled_record,
        trigger=DateTrigger(run_date=run_at),
        args=[station_id],
        kwargs={"force_start_time": start_time.isoformat(), "attempt": 0},
        id=job_id,
        replace_existing=True,
    )


def ensure_current_hour_recordings() -> int:
    """Start opname voor het lopende uur als die nog ontbreekt."""
    if not scheduler.running:
        return 0

    queued = 0
    for station in load_stations(active_only=True):
        if not should_record_station(station):
            continue

        tz = ZoneInfo(station.get("timezone", "Europe/Amsterdam"))
        now = datetime.now(tz)
        hour_start = now.replace(minute=0, second=0, microsecond=0, tzinfo=None)

        if has_completed_recording(station["id"], hour_start):
            continue
        if is_hour_actively_recording(station, hour_start, now):
            continue

        delay = 0
        _queue_record(
            station["id"],
            hour_start,
            delay_seconds=delay,
            job_id=f"ensure_{station['id']}_{hour_start.strftime('%Y%m%d%H')}",
        )
        logger.info(
            "Queued ensure recording for %s hour %s in %ss",
            station["id"],
            hour_start.strftime("%H:%M"),
            delay,
        )
        queued += 1
    return queued


def hard_refresh_recordings() -> dict:
    """Finalize stale state, retry recent failures, start missing current-hour recordings."""
    logger.info("Hard refresh: recording recovery starting")
    finalized = finalize_all_stale_recordings()
    invalidated = invalidate_short_completed_recordings()
    retry_queued = retry_todays_failed_recordings(max_hours_back=2)
    ensured = ensure_current_hour_recordings()
    summary = {
        "finalized": finalized,
        "invalidated": invalidated,
        "retry_queued": retry_queued,
        "ensured": ensured,
    }
    logger.info("Hard refresh complete: %s", summary)
    return summary


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
            trigger=CronTrigger(minute=0, second=0, timezone=tz),
            args=[station["id"]],
            id=f"record_{station['id']}",
            replace_existing=True,
            misfire_grace_time=600,
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

    scheduler.add_job(
        finalize_all_stale_recordings,
        trigger=CronTrigger(minute="*/5"),
        id="finalize_stale_recordings",
        replace_existing=True,
    )

    scheduler.add_job(
        invalidate_short_completed_recordings,
        trigger=CronTrigger(minute="*/15"),
        id="invalidate_short_recordings",
        replace_existing=True,
    )

    scheduler.add_job(
        hard_refresh_recordings,
        trigger=CronTrigger(minute=0, second=30, timezone=ZoneInfo("Europe/Amsterdam")),
        id="hourly_recording_recovery",
        replace_existing=True,
    )

    scheduler.add_job(
        retry_pending_dropbox_uploads,
        trigger=CronTrigger(minute="*/30"),
        id="retry_dropbox_uploads",
        replace_existing=True,
    )

    return scheduler


def retry_todays_failed_recordings(max_hours_back: int = 2) -> int:
    if not scheduler.running:
        return 0

    stations_by_id = {station["id"]: station for station in load_stations(active_only=True)}
    with Session(engine) as session:
        failed = session.exec(select(Recording).where(Recording.status == "failed")).all()

    queued = 0
    for recording in failed:
        station = stations_by_id.get(recording.station_id)
        if not station:
            continue
        if has_completed_recording(recording.station_id, recording.start_time):
            continue

        tz = ZoneInfo(station.get("timezone", "Europe/Amsterdam"))
        now = datetime.now(tz)
        if recording.start_time.date() != now.date():
            continue

        current_hour = now.replace(minute=0, second=0, microsecond=0, tzinfo=None)
        earliest = current_hour - timedelta(hours=max_hours_back)
        if recording.start_time < earliest:
            continue

        delay = 15 + abs(hash(recording.station_id)) % 120
        job_id = f"retry_failed_{recording.station_id}_{recording.start_time.strftime('%Y%m%d%H')}"
        _queue_record(
            recording.station_id,
            recording.start_time,
            delay_seconds=delay,
            job_id=job_id,
        )
        logger.info(
            "Queued retry for failed %s hour %s in %ss",
            recording.station_id,
            recording.start_time.strftime("%H:%M"),
            delay,
        )
        queued += 1
    return queued


def setup_scheduler() -> BackgroundScheduler:
    scheduler_result = reload_scheduler()
    hard_refresh_recordings()
    cleanup_expired_recordings()
    return scheduler_result


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
    _recording_executor.shutdown(wait=False)
