import subprocess
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from app.database import TRIMMED_DIR


def format_timestamp(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"
    return f"{minutes:02d}:{secs:06.3f}"


def trim_recording(input_path: Path, start_sec: float, end_sec: float) -> Path:
    if not input_path.exists():
        raise FileNotFoundError(f"Recording not found: {input_path}")

    if start_sec < 0:
        raise ValueError("Start time cannot be negative")

    if end_sec <= start_sec:
        raise ValueError("End time must be greater than start time")

    TRIMMED_DIR.mkdir(parents=True, exist_ok=True)

    output_name = f"trim_{input_path.stem}_{uuid4().hex[:8]}.mp3"
    output_path = TRIMMED_DIR / output_name

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-ss",
        format_timestamp(start_sec),
        "-to",
        format_timestamp(end_sec),
        "-c",
        "copy",
        str(output_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Trim operation timed out") from exc
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg not found on system PATH") from exc

    if result.returncode != 0:
        raise RuntimeError(result.stderr or "ffmpeg trim failed")

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("Trimmed output file missing or empty")

    return output_path
