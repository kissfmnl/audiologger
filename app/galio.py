"""Galio DAI pingel detection for commercial radio streams."""

import json
import logging
import math
import struct
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

SAMPLE_RATE = 22050
WINDOW_SIZE = 4096
HOP_SIZE = 1024

# Detection thresholds (from analysed reference pingel)
LOW_BAND = (200, 270)       # main ping ~233 Hz
MID_BAND = (1500, 1800)     # mid tone ~1650 Hz
HIGH_BAND = (7000, 7600)    # high impulse ~7300 Hz
LOW_PEAK_DB = -25.0
MID_PEAK_DB = -35.0
LOOKBACK_MIN = 0.22
LOOKBACK_MAX = 0.40
PING_DURATION = 2.2


def galio_cache_path(audio_path: Path) -> Path:
    return audio_path.with_name(f"{audio_path.name}.galio.json")


def _band_energy_db(samples: list[float], low_hz: float, high_hz: float) -> float:
    count = len(samples)
    if count < 64:
        return -120.0

    # Simple DFT magnitude for target band (adequate for narrow-band ping detection)
    band_power = 0.0
    freqs = range(int(low_hz), int(high_hz) + 1, max(1, int((high_hz - low_hz) / 8)))
    for freq in freqs:
        re = 0.0
        im = 0.0
        step = max(1, count // 512)
        for index in range(0, count, step):
            angle = 2 * math.pi * freq * index / SAMPLE_RATE
            sample = samples[index]
            re += sample * math.cos(angle)
            im += sample * math.sin(angle)
        band_power += re * re + im * im

    if band_power <= 0:
        return -120.0
    rms = math.sqrt(band_power / max(1, len(list(freqs))))
    return 20 * math.log10(max(rms, 1e-12))


def _decode_pcm(path: Path) -> list[float]:
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-i",
                str(path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(SAMPLE_RATE),
                "-f",
                "f32le",
                "-",
            ],
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Galio PCM decode failed for %s: %s", path.name, exc)
        return []

    if result.returncode != 0:
        return []

    samples = []
    for offset in range(0, len(result.stdout) - 3, 4):
        samples.append(struct.unpack_from("<f", result.stdout, offset)[0])
    return samples


def detect_galio_pings(audio_path: Path) -> list[dict]:
    samples = _decode_pcm(audio_path)
    if len(samples) < WINDOW_SIZE * 2:
        return []

    background_levels = []
    for start in range(0, min(len(samples) - WINDOW_SIZE, SAMPLE_RATE * 30), HOP_SIZE * 4):
        window = samples[start : start + WINDOW_SIZE]
        background_levels.append(_band_energy_db(window, LOW_BAND[0], LOW_BAND[1]))

    background_db = sorted(background_levels)[len(background_levels) // 2] if background_levels else -52.0
    threshold_low = max(LOW_PEAK_DB, background_db + 18)
    threshold_mid = max(MID_PEAK_DB, background_db + 12)

    pings: list[dict] = []
    history: list[tuple[float, float, float]] = []

    for start in range(0, len(samples) - WINDOW_SIZE, HOP_SIZE):
        window = samples[start : start + WINDOW_SIZE]
        time_sec = start / SAMPLE_RATE
        low_db = _band_energy_db(window, LOW_BAND[0], LOW_BAND[1])
        mid_db = _band_energy_db(window, MID_BAND[0], MID_BAND[1])
        high_db = _band_energy_db(window, HIGH_BAND[0], HIGH_BAND[1])

        history.append((time_sec, mid_db, high_db))
        if len(history) > 40:
            history.pop(0)

        if low_db < threshold_low:
            continue

        mid_hit = False
        for past_time, past_mid, _ in history:
            delta = time_sec - past_time
            if LOOKBACK_MIN <= delta <= LOOKBACK_MAX and past_mid >= threshold_mid:
                mid_hit = True
                break

        if not mid_hit:
            continue

        if pings and time_sec - pings[-1]["time"] < 5.0:
            continue

        confidence = min(1.0, (low_db - threshold_low) / 20 + 0.5)
        pings.append(
            {
                "time": round(time_sec, 3),
                "low_db": round(low_db, 1),
                "confidence": round(confidence, 2),
            }
        )

    return pings


def build_ad_markers(pings: list[dict], duration: float) -> list[dict]:
    """Estimate ad blocks starting at each verified pingel."""
    blocks = []
    for ping in pings:
        start = ping["time"]
        end = min(duration, start + 120.0)  # placeholder: ads up to 2 min; refine later
        blocks.append({"start": start, "end": end, "type": "galio_ad"})
    return blocks


def analyze_galio(audio_path: Path) -> dict:
    if not audio_path.exists():
        return {"pings": [], "ad_blocks": [], "duration": 0}

    pings = detect_galio_pings(audio_path)
    duration = max(1.0, (audio_path.stat().st_size / 16000))
    return {
        "pings": pings,
        "ad_blocks": build_ad_markers(pings, duration),
        "duration": duration,
        "ping_count": len(pings),
    }


def load_galio_analysis(audio_path: Path) -> dict | None:
    cache = galio_cache_path(audio_path)
    if not cache.exists() or not audio_path.exists():
        return None
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if int(data.get("source_mtime", -1)) != int(audio_path.stat().st_mtime):
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def save_galio_analysis(audio_path: Path, analysis: dict) -> None:
    cache = galio_cache_path(audio_path)
    try:
        stat = audio_path.stat()
        payload = {
            **analysis,
            "source_mtime": int(stat.st_mtime),
            "source_size": int(stat.st_size),
        }
        cache.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write galio cache %s: %s", cache, exc)


def ensure_galio_analysis(audio_path: Path) -> dict:
    cached = load_galio_analysis(audio_path)
    if cached:
        return cached
    analysis = analyze_galio(audio_path)
    save_galio_analysis(audio_path, analysis)
    return analysis


def ensure_galio_async(audio_path: Path) -> None:
    import threading

    threading.Thread(target=ensure_galio_analysis, args=(audio_path,), daemon=True).start()
