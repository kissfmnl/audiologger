import json
import logging
import re
from pathlib import Path

from sqlmodel import Session, select

from app.database import RECORDINGS_DIR, engine
from app.models import Station

logger = logging.getLogger(__name__)

ACCOUNTS_PATH = RECORDINGS_DIR / "dropbox.accounts.json"
ACCOUNT_ID_PATTERN = re.compile(r"^[a-z0-9_-]+$")


def _normalize_root(root: str) -> str:
    value = (root or "/AudioLogger").strip().rstrip("/") or "/AudioLogger"
    if not value.startswith("/"):
        value = f"/{value}"
    return value


def load_admin_dropbox_accounts() -> dict[str, dict[str, str]]:
    if not ACCOUNTS_PATH.exists():
        return {}
    try:
        payload = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read Dropbox accounts file: %s", exc)
        return {}
    if not isinstance(payload, dict):
        return {}

    accounts: dict[str, dict[str, str]] = {}
    for account_id, cfg in payload.items():
        if not isinstance(cfg, dict):
            continue
        token = str(cfg.get("token") or "").strip()
        if not token:
            continue
        accounts[str(account_id)] = {
            "label": str(cfg.get("label") or account_id),
            "token": token,
            "root": _normalize_root(str(cfg.get("root") or "/AudioLogger")),
        }
    return accounts


def save_admin_dropbox_accounts(accounts: dict[str, dict[str, str]]) -> None:
    ACCOUNTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_PATH.write_text(
        json.dumps(accounts, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def validate_account_id(account_id: str) -> str:
    account_id = account_id.strip().lower()
    if not account_id or not ACCOUNT_ID_PATTERN.match(account_id):
        raise ValueError("Account-ID: alleen kleine letters, cijfers, _ en -")
    return account_id


def token_hint(token: str) -> str:
    token = token or ""
    if len(token) <= 8:
        return "••••"
    return f"••••{token[-4:]}"


def list_accounts_for_admin() -> list[dict[str, str]]:
    return [
        {
            "id": account_id,
            "label": cfg["label"],
            "root": cfg["root"],
            "token_hint": token_hint(cfg["token"]),
        }
        for account_id, cfg in sorted(
            load_admin_dropbox_accounts().items(),
            key=lambda item: item[1]["label"].lower(),
        )
    ]


def stations_using_account(account_id: str) -> list[str]:
    with Session(engine) as session:
        stations = session.exec(
            select(Station).where(
                Station.dropbox_archive == True,  # noqa: E712
                Station.dropbox_account == account_id,
            )
        ).all()
        return [station.id for station in stations]


def upsert_dropbox_account(
    account_id: str,
    label: str,
    token: str,
    root: str,
    *,
    replace_token: bool = True,
) -> None:
    account_id = validate_account_id(account_id)
    label = label.strip() or account_id
    root = _normalize_root(root)
    accounts = load_admin_dropbox_accounts()

    if not replace_token:
        existing = accounts.get(account_id, {})
        token = token.strip() or existing.get("token", "")
    else:
        token = token.strip()

    if not token:
        raise ValueError("Access token is verplicht")

    accounts[account_id] = {
        "label": label,
        "token": token,
        "root": root,
    }
    save_admin_dropbox_accounts(accounts)
    logger.info("Dropbox account saved: %s", account_id)


def delete_dropbox_account(account_id: str) -> None:
    account_id = validate_account_id(account_id)
    in_use = stations_using_account(account_id)
    if in_use:
        raise ValueError(
            f"Account wordt nog gebruikt door: {', '.join(in_use)}. Zet archief daar eerst uit."
        )
    accounts = load_admin_dropbox_accounts()
    if account_id not in accounts:
        raise ValueError("Account niet gevonden")
    del accounts[account_id]
    save_admin_dropbox_accounts(accounts)
    logger.info("Dropbox account deleted: %s", account_id)
