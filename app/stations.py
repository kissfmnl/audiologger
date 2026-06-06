import json
import logging
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

import yaml
from sqlmodel import Session, select
from sqlalchemy import text

from app.database import (
    BASE_DIR,
    LOGOS_DIR,
    RECORDINGS_DIR,
    STATIONS_BACKUP_BAK_PATH,
    STATIONS_BACKUP_PATH,
    engine,
)
from app.models import Station

logger = logging.getLogger(__name__)

STATIONS_CONFIG = BASE_DIR / "config" / "stations.yaml"
STATION_ID_PATTERN = re.compile(r"^[a-z0-9_]+$")
DEFAULT_TIMEZONE = "Europe/Amsterdam"
DEFAULT_RETENTION_DAYS = 7
DEFAULT_EVENT_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 365

COUNTRIES = [
    {"code": "NL", "name": "Nederland", "flag": "🇳🇱", "default_timezone": "Europe/Amsterdam"},
    {"code": "BE", "name": "België", "flag": "🇧🇪", "default_timezone": "Europe/Brussels"},
    {"code": "DE", "name": "Duitsland", "flag": "🇩🇪", "default_timezone": "Europe/Berlin"},
    {"code": "FR", "name": "Frankrijk", "flag": "🇫🇷", "default_timezone": "Europe/Paris"},
    {"code": "UK", "name": "Verenigd Koninkrijk", "flag": "🇬🇧", "default_timezone": "Europe/London"},
    {"code": "IE", "name": "Ierland", "flag": "🇮🇪", "default_timezone": "Europe/Dublin"},
    {"code": "ES", "name": "Spanje", "flag": "🇪🇸", "default_timezone": "Europe/Madrid"},
    {"code": "IT", "name": "Italië", "flag": "🇮🇹", "default_timezone": "Europe/Rome"},
    {"code": "PT", "name": "Portugal", "flag": "🇵🇹", "default_timezone": "Europe/Lisbon"},
    {"code": "AT", "name": "Oostenrijk", "flag": "🇦🇹", "default_timezone": "Europe/Vienna"},
    {"code": "CH", "name": "Zwitserland", "flag": "🇨🇭", "default_timezone": "Europe/Zurich"},
    {"code": "SE", "name": "Zweden", "flag": "🇸🇪", "default_timezone": "Europe/Stockholm"},
    {"code": "NO", "name": "Noorwegen", "flag": "🇳🇴", "default_timezone": "Europe/Oslo"},
    {"code": "DK", "name": "Denemarken", "flag": "🇩🇰", "default_timezone": "Europe/Copenhagen"},
    {"code": "PL", "name": "Polen", "flag": "🇵🇱", "default_timezone": "Europe/Warsaw"},
    {"code": "US", "name": "Verenigde Staten", "flag": "🇺🇸", "default_timezone": "America/New_York"},
    {"code": "CA", "name": "Canada", "flag": "🇨🇦", "default_timezone": "America/Toronto"},
    {"code": "MX", "name": "Mexico", "flag": "🇲🇽", "default_timezone": "America/Mexico_City"},
    {"code": "BR", "name": "Brazilië", "flag": "🇧🇷", "default_timezone": "America/Sao_Paulo"},
    {"code": "AU", "name": "Australië", "flag": "🇦🇺", "default_timezone": "Australia/Sydney"},
    {"code": "NZ", "name": "Nieuw-Zeeland", "flag": "🇳🇿", "default_timezone": "Pacific/Auckland"},
    {"code": "JP", "name": "Japan", "flag": "🇯🇵", "default_timezone": "Asia/Tokyo"},
    {"code": "CN", "name": "China", "flag": "🇨🇳", "default_timezone": "Asia/Shanghai"},
    {"code": "IN", "name": "India", "flag": "🇮🇳", "default_timezone": "Asia/Kolkata"},
    {"code": "AE", "name": "VAE", "flag": "🇦🇪", "default_timezone": "Asia/Dubai"},
    {"code": "ZA", "name": "Zuid-Afrika", "flag": "🇿🇦", "default_timezone": "Africa/Johannesburg"},
]

COUNTRY_MAP = {c["code"]: c for c in COUNTRIES}

TIMEZONE_REGION_LABELS = {
    "Africa": "Afrika",
    "America": "Amerika",
    "Antarctica": "Antarctica",
    "Arctic": "Arctic",
    "Asia": "Azië",
    "Atlantic": "Atlantische Oceaan",
    "Australia": "Australië",
    "Europe": "Europa",
    "Indian": "Indische Oceaan",
    "Pacific": "Stille Oceaan",
    "Etc": "Overig (UTC)",
}


def get_timezone_groups() -> list[dict]:
    groups: dict[str, list[str]] = {}
    for tz in sorted(available_timezones()):
        region = tz.split("/")[0] if "/" in tz else "Etc"
        groups.setdefault(region, []).append(tz)

    ordered_regions = sorted(groups.keys(), key=lambda r: (r != "Europe", r != "America", r))
    return [
        {
            "region": region,
            "label": TIMEZONE_REGION_LABELS.get(region, region),
            "timezones": groups[region],
        }
        for region in ordered_regions
    ]


def resolve_logo_file(logo_path: str | None) -> Path | None:
    if not logo_path:
        return None

    path = Path(logo_path)
    candidates = [
        path,
        LOGOS_DIR / path.name,
        LOGOS_DIR / logo_path,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def logo_filename(station_id: str) -> str:
    return f"{station_id}.jpg"


def get_country_info(country_code: str) -> dict:
    code = country_code.upper()
    if code in COUNTRY_MAP:
        return COUNTRY_MAP[code]
    return {
        "code": code,
        "name": code,
        "flag": "📻",
        "default_timezone": DEFAULT_TIMEZONE,
    }


def flag_for_country(country_code: str) -> str:
    return get_country_info(country_code)["flag"]


def default_timezone_for_country(country_code: str) -> str:
    return get_country_info(country_code).get("default_timezone", DEFAULT_TIMEZONE)


def validate_timezone(timezone: str) -> str:
    tz = timezone.strip()
    if not tz:
        raise ValueError("Kies een tijdzone")
    try:
        ZoneInfo(tz)
    except Exception as exc:
        raise ValueError(f"Ongeldige tijdzone: {tz}") from exc
    return tz


def hours_to_cron(hours: str = "*") -> str:
    return "0 * * * *"


def format_schedule_label(station: dict) -> str:
    if station.get("is_event"):
        start = station.get("event_start_date") or "—"
        end = station.get("event_end_date") or "—"
        days = retention_days_for_station(station)
        return f"Evenement · {start} t/m {end} · bewaard {days} dagen"
    tz = station.get("timezone", "Europe/Amsterdam")
    return f"Hele dag · elk uur ({tz}) · bewaard {DEFAULT_RETENTION_DAYS} dagen"


def retention_days_for_station(station: dict) -> int:
    if station.get("is_event"):
        days = station.get("retention_days")
        if days is None:
            return DEFAULT_EVENT_RETENTION_DAYS
        return int(days)
    return DEFAULT_RETENTION_DAYS


def retention_label(station: dict) -> str:
    return f"{retention_days_for_station(station)} dagen"


def parse_retention_days(is_event: bool, retention_days: str | int | None) -> int | None:
    if not is_event:
        return None

    if retention_days is None or retention_days == "":
        return DEFAULT_EVENT_RETENTION_DAYS

    try:
        days = int(retention_days)
    except (TypeError, ValueError) as exc:
        raise ValueError("Bewaartermijn moet een heel aantal dagen zijn") from exc

    if days < 1 or days > MAX_RETENTION_DAYS:
        raise ValueError(f"Bewaartermijn moet tussen 1 en {MAX_RETENTION_DAYS} dagen liggen")

    return days


def station_to_dict(station: Station) -> dict:
    logo_url = None
    logo_file = resolve_logo_file(station.logo_path)
    if logo_file:
        logo_url = f"/logos/{logo_file.name}"

    country = station.country.upper()
    info = get_country_info(country)

    data = {
        "id": station.id,
        "name": station.name,
        "country": country,
        "flag": info["flag"],
        "timezone": station.timezone or DEFAULT_TIMEZONE,
        "country_name": info["name"],
        "url": station.url,
        "schedule_hours": "*",
        "schedule_cron": hours_to_cron(),
        "is_event": station.is_event,
        "event_start_date": station.event_start_date or "",
        "event_end_date": station.event_end_date or "",
        "retention_days": station.retention_days,
        "active": station.active,
        "logo_path": station.logo_path,
        "logo_url": logo_url,
    }
    data["schedule_label"] = format_schedule_label(data)
    data["retention_label"] = retention_label(data)
    return data


def _station_backup_row(station: Station) -> dict:
    return {
        "id": station.id,
        "name": station.name,
        "country": station.country,
        "flag": station.flag,
        "timezone": station.timezone or DEFAULT_TIMEZONE,
        "url": station.url,
        "schedule_hours": station.schedule_hours or "*",
        "is_event": station.is_event,
        "event_start_date": station.event_start_date,
        "event_end_date": station.event_end_date,
        "retention_days": station.retention_days,
        "active": station.active,
        "logo_path": station.logo_path,
    }


def _load_backup_payload(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Could not read station backup %s: %s", path, exc)
        return []
    return payload if isinstance(payload, list) else []


def _restore_from_backup_file(path: Path) -> int:
    payload = _load_backup_payload(path)
    if not payload:
        return 0

    with Session(engine) as session:
        for item in payload:
            session.add(
                Station(
                    id=item["id"],
                    name=item["name"],
                    country=item.get("country", "NL"),
                    flag=item.get("flag", "📻"),
                    timezone=item.get("timezone", DEFAULT_TIMEZONE),
                    url=item["url"],
                    schedule_hours=item.get("schedule_hours", "*"),
                    is_event=bool(item.get("is_event", False)),
                    event_start_date=item.get("event_start_date"),
                    event_end_date=item.get("event_end_date"),
                    retention_days=item.get("retention_days"),
                    active=bool(item.get("active", True)),
                    logo_path=item.get("logo_path"),
                )
            )
        session.commit()

    logger.warning("Restored %d stations from backup %s", len(payload), path)
    return len(payload)


def backup_stations_to_disk(*, allow_empty: bool = False) -> None:
    """Schrijf zenderconfig naar het persistente volume (backup bij elke wijziging)."""
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    with Session(engine) as session:
        stations = session.exec(select(Station).order_by(Station.name)).all()
        payload = [_station_backup_row(station) for station in stations]

    if not payload and not allow_empty:
        existing = _load_backup_payload(STATIONS_BACKUP_PATH)
        if existing:
            logger.warning(
                "Refusing to overwrite backup with empty list (%d zenders in bestaande backup)",
                len(existing),
            )
            return

    if STATIONS_BACKUP_PATH.exists():
        shutil.copy2(STATIONS_BACKUP_PATH, STATIONS_BACKUP_BAK_PATH)

    STATIONS_BACKUP_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Station backup saved (%d zenders) -> %s", len(payload), STATIONS_BACKUP_PATH)


def restore_stations_from_backup_if_needed() -> int:
    """Herstel zenders uit backup als de database leeg is (bijv. na volume-reset)."""
    with Session(engine) as session:
        if session.exec(select(Station)).first():
            return 0

    for path in (STATIONS_BACKUP_PATH, STATIONS_BACKUP_BAK_PATH):
        restored = _restore_from_backup_file(path)
        if restored:
            backup_stations_to_disk(allow_empty=False)
            return restored

    logger.warning(
        "Database has no stations and no usable backup at %s",
        STATIONS_BACKUP_PATH,
    )
    return 0


def ensure_stations_backup_exists() -> None:
    """Maak een backup als er zenders zijn maar nog geen backupbestand."""
    if STATIONS_BACKUP_PATH.exists():
        return
    with Session(engine) as session:
        if session.exec(select(Station)).first():
            backup_stations_to_disk()


def migrate_station_schema() -> None:
    columns = {
        "timezone": "TEXT DEFAULT 'Europe/Amsterdam'",
        "is_event": "INTEGER DEFAULT 0",
        "event_start_date": "TEXT",
        "event_end_date": "TEXT",
        "retention_days": "INTEGER",
    }

    with engine.connect() as conn:
        existing = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(station)")).fetchall()
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE station ADD COLUMN {name} {definition}"))
        conn.commit()

    with Session(engine) as session:
        stations = session.exec(select(Station)).all()
        changed = False
        for station in stations:
            info = get_country_info(station.country)
            if station.flag != info["flag"]:
                station.flag = info["flag"]
                changed = True
            if station.schedule_hours != "*":
                station.schedule_hours = "*"
                changed = True
            if not station.timezone:
                station.timezone = DEFAULT_TIMEZONE
                changed = True
            else:
                try:
                    ZoneInfo(station.timezone)
                except Exception:
                    station.timezone = default_timezone_for_country(station.country)
                    changed = True
        if changed:
            session.commit()


def seed_stations_from_yaml() -> None:
    """Alleen handmatig aanroepen (dev). Wordt niet meer automatisch bij opstart gebruikt."""
    if not STATIONS_CONFIG.exists():
        return

    with open(STATIONS_CONFIG, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    with Session(engine) as session:
        if session.exec(select(Station)).first():
            return

        for item in data.get("stations", []):
            country = item.get("country", "NL").upper()
            info = get_country_info(country)
            station = Station(
                id=item["id"],
                name=item["name"],
                country=country,
                flag=info["flag"],
                timezone=info.get("default_timezone", DEFAULT_TIMEZONE),
                url=item["url"],
                schedule_hours="*",
                active=item.get("active", True),
            )
            session.add(station)

        session.commit()
        logger.info("Seeded stations from stations.yaml (manual/dev only)")


def reconcile_logos() -> None:
    """Koppel logo-bestanden op schijf aan zenders in de database."""
    if not LOGOS_DIR.exists():
        return

    with Session(engine) as session:
        changed = False
        for station in session.exec(select(Station)).all():
            expected = logo_filename(station.id)
            logo_file = LOGOS_DIR / expected
            if logo_file.exists() and station.logo_path != expected:
                station.logo_path = expected
                changed = True
            elif station.logo_path:
                resolved = resolve_logo_file(station.logo_path)
                if resolved and station.logo_path != expected:
                    station.logo_path = expected
                    changed = True
        if changed:
            session.commit()
            logger.info("Reconciled station logos from disk")


def load_stations(active_only: bool = False) -> list[dict]:
    with Session(engine) as session:
        statement = select(Station).order_by(Station.name)
        if active_only:
            statement = statement.where(Station.active == True)  # noqa: E712
        stations = session.exec(statement).all()
        return [station_to_dict(s) for s in stations]


def get_station_by_id(station_id: str) -> dict | None:
    with Session(engine) as session:
        station = session.get(Station, station_id)
        return station_to_dict(station) if station else None


def get_station_model(session: Session, station_id: str) -> Station | None:
    return session.get(Station, station_id)


def validate_station_id(station_id: str) -> None:
    if not station_id or not STATION_ID_PATTERN.match(station_id):
        raise ValueError("ID mag alleen kleine letters, cijfers en underscores bevatten")


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise ValueError("Stream-URL is verplicht")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url


def validate_country(country: str) -> str:
    return country.upper().strip()[:2] or "NL"


def parse_event_dates(
    is_event: bool,
    event_start: str | None,
    event_end: str | None,
) -> tuple[bool, str | None, str | None]:
    if not is_event:
        return False, None, None

    if not event_start or not event_end:
        raise ValueError("Evenementzenders vereisen een start- en einddatum")

    try:
        start = date.fromisoformat(event_start)
        end = date.fromisoformat(event_end)
    except ValueError as exc:
        raise ValueError("Ongeldige datum (gebruik JJJJ-MM-DD)") from exc

    if end < start:
        raise ValueError("Einddatum moet na startdatum liggen")

    return True, start.isoformat(), end.isoformat()


def should_record_station(station: dict, moment: datetime | None = None) -> bool:
    if not station.get("active", True):
        return False

    tz = ZoneInfo(station.get("timezone", "Europe/Amsterdam"))
    now = moment.astimezone(tz) if moment and moment.tzinfo else datetime.now(tz)

    if station.get("is_event"):
        start_raw = station.get("event_start_date")
        end_raw = station.get("event_end_date")
        if not start_raw or not end_raw:
            return False
        today = now.date()
        return date.fromisoformat(start_raw) <= today <= date.fromisoformat(end_raw)

    return True


def recording_start_time(station: dict, moment: datetime | None = None) -> datetime:
    tz = ZoneInfo(station.get("timezone", "Europe/Amsterdam"))
    now = moment.astimezone(tz) if moment and moment.tzinfo else datetime.now(tz)
    local = now.replace(minute=0, second=0, microsecond=0)
    return local.replace(tzinfo=None)


def create_station(
    session: Session,
    station_id: str,
    name: str,
    country: str,
    timezone: str,
    url: str,
    is_event: bool = False,
    event_start_date: str | None = None,
    event_end_date: str | None = None,
    retention_days: str | int | None = None,
    active: bool = True,
) -> Station:
    validate_station_id(station_id)
    url = normalize_url(url)
    country = validate_country(country)
    timezone = validate_timezone(timezone)
    is_event, event_start_date, event_end_date = parse_event_dates(
        is_event, event_start_date, event_end_date
    )
    parsed_retention = parse_retention_days(is_event, retention_days)

    if session.get(Station, station_id):
        raise ValueError(f"Zender met ID '{station_id}' bestaat al")

    info = get_country_info(country)
    station = Station(
        id=station_id,
        name=name.strip(),
        country=country,
        flag=info["flag"],
        timezone=timezone,
        url=url,
        schedule_hours="*",
        is_event=is_event,
        event_start_date=event_start_date,
        event_end_date=event_end_date,
        retention_days=parsed_retention,
        active=active,
    )
    session.add(station)
    session.commit()
    session.refresh(station)
    backup_stations_to_disk()
    return station


def update_station(
    session: Session,
    station_id: str,
    name: str,
    country: str,
    timezone: str,
    url: str,
    is_event: bool,
    event_start_date: str | None,
    event_end_date: str | None,
    retention_days: str | int | None,
    active: bool,
) -> Station:
    station = session.get(Station, station_id)
    if not station:
        raise ValueError("Zender niet gevonden")

    url = normalize_url(url)
    country = validate_country(country)
    timezone = validate_timezone(timezone)
    is_event, event_start_date, event_end_date = parse_event_dates(
        is_event, event_start_date, event_end_date
    )
    parsed_retention = parse_retention_days(is_event, retention_days)
    info = get_country_info(country)

    station.name = name.strip()
    station.country = country
    station.flag = info["flag"]
    station.timezone = timezone
    station.url = url
    station.schedule_hours = "*"
    station.is_event = is_event
    station.event_start_date = event_start_date
    station.event_end_date = event_end_date
    station.retention_days = parsed_retention
    station.active = active

    session.add(station)
    session.commit()
    session.refresh(station)
    backup_stations_to_disk()
    return station


def delete_station(session: Session, station_id: str) -> None:
    station = session.get(Station, station_id)
    if not station:
        raise ValueError("Zender niet gevonden")

    if station.logo_path:
        logo = resolve_logo_file(station.logo_path)
        if logo and logo.exists():
            logo.unlink(missing_ok=True)

    session.delete(station)
    session.commit()
    backup_stations_to_disk(allow_empty=True)


def save_station_logo(station_id: str, image_bytes: bytes) -> str:
    if len(image_bytes) > 5 * 1024 * 1024:
        raise ValueError("Logo is te groot (max 5 MB)")

    LOGOS_DIR.mkdir(parents=True, exist_ok=True)
    filename = logo_filename(station_id)
    logo_path = LOGOS_DIR / filename
    logo_path.write_bytes(image_bytes)

    with Session(engine) as session:
        station = session.get(Station, station_id)
        if not station:
            raise ValueError("Zender niet gevonden")

        station.logo_path = filename
        session.add(station)
        session.commit()

    backup_stations_to_disk()
    return filename
