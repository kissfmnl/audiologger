import logging
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app.database import LOGS_DIR, RECORDINGS_DIR, engine
from app.galio import ensure_galio_async
from app.peaks import ensure_peaks_async
from app.models import Recording

logger = logging.getLogger(__name__)

RECORDING_DURATION_SECONDS = 3600
MP3_BITRATE = "128k"
RECORDING_USER_AGENT = "Mozilla/5.0 (compatible; AudioLogger/1.0)"
MAX_CONCURRENT_RECORDINGS = 2
_recording_slots = threading.BoundedSemaphore(MAX_CONCURRENT_RECORDINGS)


def sanitize_name(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name).strip("_")


def build_filename(station: dict, start_time: datetime) -> str:
    country = station["country"]
    name = sanitize_name(station["name"])
    return f"{country}_{name}_{start_time.strftime('%Y-%m-%d')}_{start_time.strftime('%H00')}.mp3"


def build_output_path(station: dict, start_time: datetime) -> Path:
    return RECORDINGS_DIR / build_filename(station, start_time)


def get_file_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    return round(path.stat().st_size / (1024 * 1024), 2)


def partial_size_mb(output_path: Path) -> float:
    parts_dir = output_path.parent / f".{output_path.stem}_parts"
    if not parts_dir.exists():
        return 0.0
    total = sum(part.stat().st_size for part in parts_dir.glob("part*.mp3") if part.exists())
    return round(total / (1024 * 1024), 2)


def is_hour_actively_recording(station: dict, start_time: datetime) -> bool:
    output_path = build_output_path(station, start_time)
    parts_dir = output_path.parent / f".{output_path.stem}_parts"
    if parts_dir.is_dir():
        for part in parts_dir.glob("part*.mp3"):
            if part.stat().st_size > 0:
                return True

    log_path = LOGS_DIR / f"{output_path.stem}.log"
    if log_path.exists() and time.time() - log_path.stat().st_mtime < 120:
        return True

    return False


def get_partial_path_for_hour(station: dict, start_time: datetime) -> Path | None:
    output_path = build_output_path(station, start_time)
    if output_path.exists() and output_path.stat().st_size > 0:
        return output_path

    parts_dir = output_path.parent / f".{output_path.stem}_parts"
    if not parts_dir.exists():
        return None

    parts = sorted(parts_dir.glob("part*.mp3"), key=lambda path: path.stat().st_mtime)
    for part in reversed(parts):
        if part.stat().st_size > 0:
            return part
    return None


def get_partial_recording_path(recording: Recording) -> Path | None:
    output_path = Path(recording.file_path)
    if output_path.exists() and output_path.stat().st_size > 0:
        return output_path

    parts_dir = output_path.parent / f".{output_path.stem}_parts"
    if not parts_dir.exists():
        return None

    parts = sorted(parts_dir.glob("part*.mp3"), key=lambda path: path.stat().st_mtime)
    for part in reversed(parts):
        if part.stat().st_size > 0:
            return part
    return None


MIN_VALID_MP3_BYTES = 100_000


def _hour_end(start_time: datetime, tz: ZoneInfo) -> datetime:
    if start_time.tzinfo is None:
        return start_time.replace(tzinfo=tz) + timedelta(hours=1)
    return start_time.astimezone(tz) + timedelta(hours=1)


def has_completed_recording(station_id: str, start_time: datetime) -> bool:
    with Session(engine) as session:
        existing = session.exec(
            select(Recording).where(
                Recording.station_id == station_id,
                Recording.start_time == start_time,
                Recording.status == "completed",
            )
        ).first()
        return existing is not None


def _ffmpeg_input_args(url: str) -> list[str]:
    return [
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
        "-rw_timeout",
        "15000000",
        "-user_agent",
        RECORDING_USER_AGENT,
        "-i",
        url,
    ]


def run_ffmpeg_record(
    url: str,
    output_path: Path,
    log_path: Path,
    duration_seconds: int = RECORDING_DURATION_SECONDS,
) -> tuple[bool, str]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        *_ffmpeg_input_args(url),
        "-t",
        str(duration_seconds),
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        MP3_BITRATE,
        "-ar",
        "44100",
        "-write_xing",
        "0",
        str(output_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=duration_seconds + 120,
        )

        log_content = (
            f"Command: {' '.join(cmd)}\n"
            f"Return code: {result.returncode}\n"
            f"--- STDOUT ---\n{result.stdout}\n"
            f"--- STDERR ---\n{result.stderr}\n"
        )
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"\n--- segment {time.strftime('%H:%M:%S')} ---\n{log_content}")

        if result.returncode != 0:
            error = (result.stderr or result.stdout or "").strip()
            return False, error[-500:] if error else "ffmpeg returned non-zero exit code"

        if not output_path.exists() or output_path.stat().st_size == 0:
            return False, "Output file missing or empty after recording"

        return True, ""

    except subprocess.TimeoutExpired:
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"\n--- segment {time.strftime('%H:%M:%S')} ---\nRecording timed out.\n")
        return False, "Recording timed out"
    except FileNotFoundError:
        return False, "ffmpeg not found on system PATH"
    except OSError as exc:
        return False, str(exc)


def concat_mp3_segments(segments: list[Path], output_path: Path) -> bool:
    if not segments:
        return False

    if len(segments) == 1:
        shutil.move(str(segments[0]), str(output_path))
        return output_path.exists() and output_path.stat().st_size > 0

    list_path = output_path.with_suffix(".concat.txt")
    list_path.write_text(
        "\n".join(f"file '{segment.resolve()}'" for segment in segments),
        encoding="utf-8",
    )
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-c",
        "copy",
        "-y",
        str(output_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        list_path.unlink(missing_ok=True)
        if result.returncode != 0:
            logger.error("Concat failed: %s", (result.stderr or result.stdout)[-500:])
            return False
        return output_path.exists() and output_path.stat().st_size > 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.error("Concat failed: %s", exc)
        list_path.unlink(missing_ok=True)
        return False


def save_recording_to_db(
    session: Session,
    station: dict,
    start_time: datetime,
    output_path: Path,
    status: str,
    error_message: str | None = None,
) -> Recording:
    end_time = start_time + timedelta(seconds=RECORDING_DURATION_SECONDS)
    duration = RECORDING_DURATION_SECONDS if status == "completed" else 0
    if status == "completed":
        file_size = get_file_size_mb(output_path)
    elif status == "recording":
        file_size = partial_size_mb(output_path)
    else:
        file_size = 0.0

    existing = session.exec(
        select(Recording).where(
            Recording.station_id == station["id"],
            Recording.start_time == start_time,
        )
    ).first()

    if existing:
        existing.station_name = station["name"]
        existing.country = station["country"]
        existing.end_time = end_time
        existing.duration_seconds = duration
        existing.file_path = str(output_path)
        existing.file_size_mb = file_size
        existing.status = status
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing

    recording = Recording(
        station_id=station["id"],
        station_name=station["name"],
        country=station["country"],
        start_time=start_time,
        end_time=end_time,
        duration_seconds=duration,
        file_path=str(output_path),
        file_size_mb=file_size,
        status=status,
    )
    session.add(recording)
    session.commit()
    session.refresh(recording)
    if error_message:
        logger.warning(
            "Recording failed for %s at %s: %s",
            station["id"],
            start_time.isoformat(),
            error_message,
        )
    return recording


def record_station(station: dict, start_time: datetime | None = None) -> Recording:
    tz = ZoneInfo(station.get("timezone", "Europe/Amsterdam"))

    if start_time is None:
        now = datetime.now(tz)
        start_time = now.replace(minute=0, second=0, microsecond=0, tzinfo=None)

    if has_completed_recording(station["id"], start_time):
        with Session(engine) as session:
            return session.exec(
                select(Recording).where(
                    Recording.station_id == station["id"],
                    Recording.start_time == start_time,
                    Recording.status == "completed",
                )
            ).one()

    logger.info(
        "Waiting for recording slot (%s at %s)",
        station["id"],
        start_time.strftime("%Y-%m-%d %H:%M"),
    )
    _recording_slots.acquire()
    try:
        return _record_station_locked(station, start_time, tz)
    finally:
        _recording_slots.release()


def _record_station_locked(station: dict, start_time: datetime, tz: ZoneInfo) -> Recording:
    hour_start = start_time.replace(tzinfo=tz)
    hour_end = hour_start + timedelta(hours=1)

    output_path = build_output_path(station, start_time)
    log_path = LOGS_DIR / f"{output_path.stem}.log"
    log_path.write_text(f"Recording hour {start_time.isoformat()}\n", encoding="utf-8")

    parts_dir = output_path.parent / f".{output_path.stem}_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    segments: list[Path] = []
    segment_idx = 0
    last_error = ""

    with Session(engine) as session:
        save_recording_to_db(session, station, start_time, output_path, "recording")

    try:
        while datetime.now(tz) < hour_end:
            remaining = int((hour_end - datetime.now(tz)).total_seconds())
            if remaining < 1:
                break

            segment_path = parts_dir / f"part{segment_idx:04d}.mp3"
            success, last_error = run_ffmpeg_record(
                station["url"],
                segment_path,
                log_path,
                duration_seconds=remaining,
            )

            if segment_path.exists() and segment_path.stat().st_size > 0:
                segments.append(segment_path)
                segment_idx += 1
                with Session(engine) as session:
                    save_recording_to_db(session, station, start_time, output_path, "recording")

            if success:
                break

            logger.warning(
                "Stream error for %s at %s, restarting immediately: %s",
                station["id"],
                start_time.strftime("%Y-%m-%d %H:%M"),
                last_error,
            )
            if segment_path.exists() and segment_path.stat().st_size == 0:
                segment_path.unlink(missing_ok=True)

        success = bool(segments) and concat_mp3_segments(segments, output_path)
        status = "completed" if success else "failed"

        if not success and output_path.exists():
            output_path.unlink(missing_ok=True)

        with Session(engine) as session:
            recording = save_recording_to_db(
                session,
                station,
                start_time,
                output_path,
                status,
                error_message=None if success else last_error,
            )

        if not success:
            raise RuntimeError(
                f"Recording failed for {station['id']} at {start_time.isoformat()}: {last_error}"
            )

        ensure_peaks_async(output_path)
        ensure_galio_async(output_path)
        return recording
    finally:
        shutil.rmtree(parts_dir, ignore_errors=True)


def finalize_stale_recording(
    session: Session,
    station: dict,
    recording: Recording,
) -> Recording:
    """Close out recordings left in 'recording' after crashes, deploys, or hung jobs."""
    if recording.status != "recording":
        return recording

    tz = ZoneInfo(station.get("timezone", "Europe/Amsterdam"))
    start_time = recording.start_time
    hour_end = _hour_end(start_time, tz)
    now = datetime.now(tz)

    if now < hour_end + timedelta(seconds=90):
        if is_hour_actively_recording(station, start_time):
            return recording

    if is_hour_actively_recording(station, start_time):
        return recording

    output_path = Path(recording.file_path)
    parts_dir = output_path.parent / f".{output_path.stem}_parts"

    if output_path.exists() and output_path.stat().st_size >= MIN_VALID_MP3_BYTES:
        updated = save_recording_to_db(session, station, start_time, output_path, "completed")
        ensure_peaks_async(output_path)
        ensure_galio_async(output_path)
        logger.info("Finalized stale recording as completed: %s %s", station["id"], start_time)
        return updated

    segments = []
    if parts_dir.is_dir():
        segments = [part for part in sorted(parts_dir.glob("part*.mp3")) if part.stat().st_size > 0]

    if segments and concat_mp3_segments(segments, output_path):
        updated = save_recording_to_db(session, station, start_time, output_path, "completed")
        ensure_peaks_async(output_path)
        ensure_galio_async(output_path)
        shutil.rmtree(parts_dir, ignore_errors=True)
        logger.info("Salvaged stale recording from parts: %s %s", station["id"], start_time)
        return updated

    updated = save_recording_to_db(session, station, start_time, output_path, "failed")
    shutil.rmtree(parts_dir, ignore_errors=True)
    logger.warning("Marked stale recording as failed: %s %s", station["id"], start_time)
    return updated


def finalize_all_stale_recordings() -> int:
    from app.stations import get_station_by_id

    finalized = 0
    with Session(engine) as session:
        stale = session.exec(select(Recording).where(Recording.status == "recording")).all()
        for recording in stale:
            station = get_station_by_id(recording.station_id)
            if not station:
                continue
            before = recording.status
            finalize_stale_recording(session, station, recording)
            session.refresh(recording)
            if recording.status != before:
                finalized += 1
    return finalized
