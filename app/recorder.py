import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import Session

from app.database import LOGS_DIR, RECORDINGS_DIR, engine
from app.models import Recording

RECORDING_DURATION_SECONDS = 3600
MP3_BITRATE = "128k"


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


def run_ffmpeg_record(url: str, output_path: Path, log_path: Path) -> tuple[bool, str]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        url,
        "-t",
        str(RECORDING_DURATION_SECONDS),
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        MP3_BITRATE,
        "-ar",
        "44100",
        str(output_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=RECORDING_DURATION_SECONDS + 120,
        )

        log_content = (
            f"Command: {' '.join(cmd)}\n"
            f"Return code: {result.returncode}\n"
            f"--- STDOUT ---\n{result.stdout}\n"
            f"--- STDERR ---\n{result.stderr}\n"
        )
        log_path.write_text(log_content, encoding="utf-8")

        if result.returncode != 0:
            return False, result.stderr or "ffmpeg returned non-zero exit code"

        if not output_path.exists() or output_path.stat().st_size == 0:
            return False, "Output file missing or empty after recording"

        return True, ""

    except subprocess.TimeoutExpired:
        log_path.write_text(
            f"Command: {' '.join(cmd)}\nRecording timed out.\n",
            encoding="utf-8",
        )
        return False, "Recording timed out"
    except FileNotFoundError:
        return False, "ffmpeg not found on system PATH"
    except OSError as exc:
        return False, str(exc)


def save_recording_to_db(
    session: Session,
    station: dict,
    start_time: datetime,
    output_path: Path,
    status: str,
) -> Recording:
    end_time = start_time + timedelta(seconds=RECORDING_DURATION_SECONDS)
    duration = RECORDING_DURATION_SECONDS if status == "completed" else 0

    recording = Recording(
        station_id=station["id"],
        station_name=station["name"],
        country=station["country"],
        start_time=start_time,
        end_time=end_time,
        duration_seconds=duration,
        file_path=str(output_path),
        file_size_mb=get_file_size_mb(output_path) if status == "completed" else 0.0,
        status=status,
    )
    session.add(recording)
    session.commit()
    session.refresh(recording)
    return recording


def record_station(station: dict, start_time: datetime | None = None) -> Recording:
    if start_time is None:
        start_time = datetime.now().replace(minute=0, second=0, microsecond=0)

    output_path = build_output_path(station, start_time)
    log_path = LOGS_DIR / f"{output_path.stem}.log"

    success = False
    last_error = ""

    for attempt in range(2):
        success, last_error = run_ffmpeg_record(station["url"], output_path, log_path)
        if success:
            break
        if attempt == 0 and output_path.exists():
            output_path.unlink(missing_ok=True)

    status = "completed" if success else "failed"

    if not success and output_path.exists():
        output_path.unlink(missing_ok=True)

    with Session(engine) as session:
        recording = save_recording_to_db(
            session, station, start_time, output_path, status
        )

    if not success:
        raise RuntimeError(
            f"Recording failed for {station['id']} after 2 attempts: {last_error}"
        )

    return recording
