import json
import logging
import math
import shutil
import struct
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_BARS = 512
WIRE_BARS = 512
EMBED_BARS = 256
BYTES_PER_SECOND_128K = 16000
_response_cache: dict[str, tuple[int, int, dict]] = {}
_response_cache_lock = threading.Lock()


def peaks_cache_path(audio_path: Path) -> Path:
    return audio_path.with_name(f"{audio_path.name}.peaks.json")


def placeholder_peaks(bars: int = WIRE_BARS) -> list[float]:
    return [round(0.07 + 0.05 * math.sin(index * 0.06), 4) for index in range(bars)]


def downsample_peaks(peaks: list[float], bars: int = WIRE_BARS) -> list[float]:
    if not peaks or len(peaks) <= bars:
        return peaks
    step = len(peaks) / bars
    sampled: list[float] = []
    for index in range(bars):
        start = int(index * step)
        end = max(start + 1, int((index + 1) * step))
        sampled.append(round(max(peaks[start:end]), 4))
    return sampled


def peaks_cache_exists(path: Path) -> bool:
    cache_path = peaks_cache_path(path)
    if not cache_path.exists() or not path.exists():
        return False
    try:
        audio_stat = path.stat()
        cache_stat = cache_path.stat()
        return cache_stat.st_mtime >= audio_stat.st_mtime and cache_stat.st_size > 20
    except OSError:
        return False


def _wire_response(peaks: list[float], duration: float, precise: bool) -> dict:
    return {
        "peaks": downsample_peaks(peaks),
        "duration": duration,
        "ready": True,
        "precise": precise,
    }


def _cached_response(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        stat = path.stat()
        key = str(path.resolve())
        mtime = int(stat.st_mtime)
        size = int(stat.st_size)
        with _response_cache_lock:
            cached = _response_cache.get(key)
            if cached and cached[0] == mtime and cached[1] == size:
                return cached[2]
    except OSError:
        return None
    return None


def _store_response(path: Path, response: dict) -> None:
    try:
        stat = path.stat()
        key = str(path.resolve())
        with _response_cache_lock:
            _response_cache[key] = (int(stat.st_mtime), int(stat.st_size), response)
    except OSError:
        pass


def estimate_duration(path: Path) -> float:
    cached = load_cached_peaks(path)
    if cached:
        return cached[1]
    if not path.exists():
        return 3600.0
    size = path.stat().st_size
    if size <= 0:
        return 3600.0
    return max(1.0, size / BYTES_PER_SECOND_128K)


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
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        logger.warning("ffprobe failed for %s: %s", path, exc)
    return estimate_duration(path)


def _parse_audiowaveform_json(payload: dict) -> list[float]:
    raw = payload.get("data", [])
    channels = int(payload.get("channels", 1) or 1)
    bits = int(payload.get("bits", 8) or 8)
    max_val = 128 if bits == 8 else 32768
    stride = 2 * channels
    peaks: list[float] = []

    for index in range(0, len(raw) - 1, stride):
        minimum = abs(int(raw[index]))
        maximum = abs(int(raw[index + 1])) if index + 1 < len(raw) else 0
        peaks.append(max(minimum, maximum) / max_val)

    max_peak = max(peaks) if peaks else 0.0
    if max_peak > 0:
        peaks = [round(peak / max_peak, 4) for peak in peaks]
    return peaks


def _decode_peaks_audiowaveform(path: Path, bars: int) -> tuple[list[float], float]:
    if not shutil.which("audiowaveform"):
        return [], 0.0

    duration = max(estimate_duration(path), 1.0)
    pixels_per_second = max(0.15, bars / duration)

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        output_path = Path(handle.name)

    try:
        result = subprocess.run(
            [
                "audiowaveform",
                "-i",
                str(path),
                "-o",
                str(output_path),
                "--output-format",
                "json",
                "-b",
                "8",
                "--pixels-per-second",
                f"{pixels_per_second:.4f}",
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "audiowaveform failed for %s: %s",
                path.name,
                (result.stderr or result.stdout)[-300:],
            )
            return [], duration

        payload = json.loads(output_path.read_text(encoding="utf-8"))
        peaks = _parse_audiowaveform_json(payload)
        if not peaks:
            return [], duration
        return peaks[:bars], duration
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        logger.warning("audiowaveform failed for %s: %s", path.name, exc)
        return [], duration
    finally:
        output_path.unlink(missing_ok=True)


def _decode_peaks_ffmpeg(path: Path, bars: int) -> tuple[list[float], float]:
    duration = max(estimate_duration(path), 1.0)
    sample_rate = 80
    samples_per_bar = max(1, int(duration * sample_rate / bars))

    try:
        process = subprocess.Popen(
            [
                "ffmpeg",
                "-threads",
                "0",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-f",
                "s16le",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.warning("ffmpeg peak decode failed for %s: %s", path, exc)
        return [], duration

    peaks: list[float] = []
    bar_max = 0
    samples_in_bar = 0
    buffer = b""

    assert process.stdout is not None
    while len(peaks) < bars:
        chunk = process.stdout.read(262144)
        if not chunk:
            break
        buffer += chunk

        offset = 0
        while offset + 2 <= len(buffer) and len(peaks) < bars:
            sample = struct.unpack_from("<h", buffer, offset)[0]
            offset += 2
            bar_max = max(bar_max, abs(sample))
            samples_in_bar += 1
            if samples_in_bar >= samples_per_bar:
                peaks.append(bar_max / 32768.0)
                bar_max = 0
                samples_in_bar = 0

        buffer = buffer[offset:]

    if bar_max > 0 and len(peaks) < bars:
        peaks.append(bar_max / 32768.0)

    try:
        process.terminate()
        process.wait(timeout=2)
    except (subprocess.TimeoutExpired, OSError):
        process.kill()

    max_peak = max(peaks) if peaks else 0.0
    if max_peak > 0:
        peaks = [round(peak / max_peak, 4) for peak in peaks]

    while len(peaks) < bars:
        peaks.append(0.0)

    return peaks[:bars], duration


def _decode_peaks(path: Path, bars: int) -> tuple[list[float], float]:
    peaks, duration = _decode_peaks_audiowaveform(path, bars)
    if peaks:
        return peaks, duration
    return _decode_peaks_ffmpeg(path, bars)


def load_cached_peaks(path: Path) -> tuple[list[float], float] | None:
    cache_path = peaks_cache_path(path)
    if not cache_path.exists() or not path.exists():
        return None

    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if int(cache.get("source_mtime", -1)) != int(path.stat().st_mtime):
            return None
        if int(cache.get("source_size", -1)) != int(path.stat().st_size):
            return None
        peaks = cache.get("wire_peaks") or cache.get("peaks", [])
        duration = float(cache.get("duration", 3600))
        if peaks:
            return peaks, duration
    except (OSError, ValueError, TypeError):
        return None
    return None


def read_embed_peaks(path: Path | None) -> list[float] | None:
    if not path:
        return None
    cached = load_cached_peaks(path)
    if not cached:
        return None
    return downsample_peaks(cached[0], EMBED_BARS)


def save_cached_peaks(path: Path, peaks: list[float], duration: float) -> None:
    cache_path = peaks_cache_path(path)
    try:
        stat = path.stat()
        wire_peaks = downsample_peaks(peaks, EMBED_BARS)
        cache_path.write_text(
            json.dumps(
                {
                    "duration": duration,
                    "peaks": peaks,
                    "wire_peaks": wire_peaks,
                    "source_mtime": int(stat.st_mtime),
                    "source_size": int(stat.st_size),
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not write peaks cache %s: %s", cache_path, exc)


def read_peaks_fast(path: Path, max_wait: float = 0.0) -> dict:
    """Return cached peaks; optionally wait up to max_wait seconds for generation."""
    cached_response = _cached_response(path)
    if cached_response and cached_response.get("precise"):
        return cached_response

    duration = estimate_duration(path)
    if not path.exists() or path.stat().st_size == 0:
        response = _wire_response(placeholder_peaks(), duration, False)
        _store_response(path, response)
        return response

    cached = load_cached_peaks(path)
    if cached:
        peaks, cached_duration = cached
        response = _wire_response(peaks, cached_duration, True)
        _store_response(path, response)
        return response

    if max_wait > 0:
        done = threading.Event()

        def _generate() -> None:
            try:
                ensure_peaks(path)
            finally:
                done.set()

        threading.Thread(target=_generate, daemon=True).start()
        done.wait(timeout=max_wait)
        cached = load_cached_peaks(path)
        if cached:
            peaks, cached_duration = cached
            response = _wire_response(peaks, cached_duration, True)
            _store_response(path, response)
            return response

    ensure_peaks_async(path)
    return {
        "peaks": [],
        "duration": duration,
        "ready": False,
        "precise": False,
    }


def ensure_peaks(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    if load_cached_peaks(path):
        return
    peaks, duration = _decode_peaks(path, DEFAULT_BARS)
    if peaks:
        save_cached_peaks(path, peaks, duration)
        _store_response(path, _wire_response(peaks, duration, True))


def ensure_peaks_async(path: Path) -> None:
    threading.Thread(target=ensure_peaks, args=(path,), daemon=True).start()


def warm_missing_peaks(recordings_dir: Path, workers: int = 1) -> None:
    pending = [
        audio_path
        for audio_path in sorted(recordings_dir.glob("*.mp3"))
        if not load_cached_peaks(audio_path)
    ]
    if not pending:
        return

    def _warm(audio_path: Path) -> None:
        try:
            ensure_peaks(audio_path)
        except Exception:
            logger.exception("Failed to warm peaks for %s", audio_path.name)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(_warm, pending))
