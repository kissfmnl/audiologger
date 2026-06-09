import json
import logging
import os
from typing import TypedDict

logger = logging.getLogger(__name__)


class DropboxAccount(TypedDict):
    label: str
    token: str
    root: str


def _load_accounts_from_env_vars() -> dict[str, DropboxAccount]:
    accounts: dict[str, DropboxAccount] = {}
    account_ids_raw = os.getenv("DROPBOX_ACCOUNT_IDS", "").strip()
    if not account_ids_raw:
        return accounts

    for account_id in account_ids_raw.split(","):
        account_id = account_id.strip().lower()
        if not account_id:
            continue
        env_key = account_id.upper().replace("-", "_")
        token = os.getenv(f"DROPBOX_TOKEN_{env_key}", "").strip()
        if not token:
            logger.warning("DROPBOX_ACCOUNT_IDS bevat '%s' maar DROPBOX_TOKEN_%s ontbreekt", account_id, env_key)
            continue
        root = os.getenv(f"DROPBOX_ROOT_{env_key}", "/AudioLogger").strip().rstrip("/") or "/AudioLogger"
        label = os.getenv(f"DROPBOX_LABEL_{env_key}", account_id).strip() or account_id
        accounts[account_id] = {
            "label": label,
            "token": token,
            "root": root,
        }

    return accounts


def load_dropbox_accounts() -> dict[str, DropboxAccount]:
    from app.dropbox_settings import load_admin_dropbox_accounts

    accounts: dict[str, DropboxAccount] = dict(load_admin_dropbox_accounts())
    raw = os.getenv("DROPBOX_ACCOUNTS", "").strip()
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.error("DROPBOX_ACCOUNTS is geen geldige JSON")
            payload = {}
        if isinstance(payload, dict):
            for account_id, cfg in payload.items():
                if not isinstance(cfg, dict):
                    continue
                token = str(cfg.get("token") or "").strip()
                if not token:
                    continue
                root = str(cfg.get("root") or "/AudioLogger").strip().rstrip("/") or "/AudioLogger"
                accounts.setdefault(str(account_id), {
                    "label": str(cfg.get("label") or account_id),
                    "token": token,
                    "root": root,
                })

    for account_id, cfg in _load_accounts_from_env_vars().items():
        accounts.setdefault(account_id, cfg)

    legacy_token = os.getenv("DROPBOX_ACCESS_TOKEN", "").strip()
    if legacy_token:
        accounts.setdefault(
            "default",
            {
                "label": os.getenv("DROPBOX_LABEL", "Standaard").strip() or "Standaard",
                "token": legacy_token,
                "root": os.getenv("DROPBOX_ROOT_FOLDER", "/AudioLogger").strip().rstrip("/") or "/AudioLogger",
            },
        )

    return accounts


def list_dropbox_accounts_for_admin() -> list[dict[str, str]]:
    return [
        {"id": account_id, "label": cfg["label"]}
        for account_id, cfg in sorted(load_dropbox_accounts().items(), key=lambda item: item[1]["label"])
    ]


def dropbox_configured() -> bool:
    return bool(load_dropbox_accounts())


def resolve_station_account(station: dict) -> tuple[str, DropboxAccount] | None:
    if not station.get("dropbox_archive"):
        return None

    accounts = load_dropbox_accounts()
    if not accounts:
        return None

    preferred = (station.get("dropbox_account") or "").strip()
    if preferred and preferred in accounts:
        return preferred, accounts[preferred]

    if "default" in accounts:
        return "default", accounts["default"]

    account_id = next(iter(accounts))
    return account_id, accounts[account_id]


def dropbox_account_label(station: dict) -> str | None:
    resolved = resolve_station_account(station)
    if not resolved:
        return None
    return resolved[1]["label"]


def station_archive_is_ready(station: dict) -> bool:
    return resolve_station_account(station) is not None
