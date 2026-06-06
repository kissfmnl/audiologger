import json
import logging
import struct
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_BARS = 1800


def peaks_cache_path(audio_path: Path) -> Path:
    return audio_path.with_name(f"{audio_path.name}.peaks.json")


def get_audio_duration(path: Path) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        logger.warning("ffprobe failed for %s: %s", path, exc)
    return 3600.0


def _decode_peaks(path: Path, bars: int) -> tuple[list[float], float]:
    duration = get_audio_duration(path)
    sample_rate = min(max(bars * 2, 2000), 8000)

    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-f",
                "s16le",
                "-",
            ],
            capture_output=True,
            timeout=180,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("ffmpeg peak decode failed for %s: %s", path, exc)
        return [], duration

    if result.returncode != 0 or not result.stdout:
        return [], duration

    sample_count = len(result.stdout) // 2
    if sample_count == 0:
        return [], duration

    samples = struct.unpack(f"<{sample_count}h", result.stdout)
    chunk_size = max(1, sample_count // bars)
    peaks: list[float] = []

    for index in range(0, sample_count, chunk_size):
        chunk = samples[index : index + chunk_size]
        if chunk:
            peaks.append(max(abs(sample) for sample in chunk) / 32768.0)

    peaks = peaks[:bars]
    max_peak = max(peaks) if peaks else 0.0
    if max_peak > 0:
        peaks = [round(peak / max_peak, 4) for peak in peaks]

    return peaks, duration


def load_cached_peaks(path: Path) -> tuple[list[float], float] | None:
    cache_path = peaks_cache_path(path)
    if not cache_path.exists() or not path.exists():
        return None

    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if cache.get("source_mtime") != path.stat().st_mtime:
            return None
        if cache.get("source_size") != path.stat().st_size:
            return None
        peaks = cache.get("peaks", [])
        duration = float(cache.get("duration", 3600))
        if peaks:
            return peaks, duration
    except (OSError, ValueError, TypeError):
        return None
    return None


def save_cached_peaks(path: Path, peaks: list[float], duration: float) -> None:
    cache_path = peaks_cache_path(path)
    try:
        cache_path.write_text(
            json.dumps(
                {
                    "duration": duration,
                    "peaks": peaks,
                    "source_mtime": path.stat().st_mtime,
                    "source_size": path.stat().st_size,
                }
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not write peaks cache %s: %s", cache_path, exc)


def get_peaks_for_file(path: Path, bars: int = DEFAULT_BARS) -> tuple[list[float], float]:
    if not path.exists() or path.stat().st_size == 0:
        return [], 0.0

    cached = load_cached_peaks(path)
    if cached:
        return cached

    peaks, duration = _decode_peaks(path, bars)
    if peaks and path.stat().st_size > 500_000:
        save_cached_peaks(path, peaks, duration)

    return peaks, duration
