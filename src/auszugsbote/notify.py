"""Benachrichtigung per Home Assistant (optional)."""
from __future__ import annotations

import logging

import httpx

from .config import AppConfig
from .vault import Vault, VaultError

log = logging.getLogger(__name__)


def send(cfg: AppConfig, vault: Vault, title: str, message: str) -> bool:
    b = cfg.benachrichtigung
    if not b.ha_url or not b.ha_service:
        return False
    try:
        token = vault.get("notify.ha_token") if vault.unlocked else None
    except VaultError:
        token = None
    if not token:
        log.warning("Home-Assistant-Token fehlt im Tresor")
        return False
    service = b.ha_service.strip()
    domain, _, name = service.partition(".")
    if not name:
        domain, name = "notify", service
    url = f"{b.ha_url.rstrip('/')}/api/services/{domain}/{name}"
    try:
        r = httpx.post(url, json={"title": title, "message": message},
                       headers={"Authorization": f"Bearer {token}"}, timeout=10)
        r.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Home-Assistant-Benachrichtigung fehlgeschlagen: %s", exc)
        return False
