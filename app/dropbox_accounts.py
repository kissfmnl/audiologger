import json
import logging
import os
from typing import TypedDict

logger = logging.getLogger(__name__)


class DropboxAccount(TypedDict):
    label: str
    token: str
    root: str


def load_dropbox_accounts() -> dict[str, DropboxAccount]:
    accounts: dict[str, DropboxAccount] = {}
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
                accounts[str(account_id)] = {
                    "label": str(cfg.get("label") or account_id),
                    "token": token,
                    "root": root,
                }

    legacy_token = os.getenv("DROPBOX_ACCESS_TOKEN", "").strip()
    if legacy_token and "default" not in accounts:
        legacy_root = os.getenv("DROPBOX_ROOT_FOLDER", "/AudioLogger").strip().rstrip("/")
        accounts["default"] = {
            "label": "Standaard",
            "token": legacy_token,
            "root": legacy_root or "/AudioLogger",
        }

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
