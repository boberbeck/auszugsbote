"""Konfiguration: alles ohne Geheimnisse, gespeichert als YAML.

PINs, Logins, Produkt-ID und Tokens liegen NICHT hier, sondern im Tresor (vault.py).
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Callable, Literal

import yaml
from pydantic import BaseModel, Field, field_validator


def data_dir() -> Path:
    return Path(os.environ.get("AUSZUGSBOTE_DATA", "/data"))


def config_path() -> Path:
    return Path(os.environ.get("AUSZUGSBOTE_CONFIG", str(data_dir() / "config.yaml")))


def keyfile_path() -> Path:
    return Path(os.environ.get("AUSZUGSBOTE_VAULT_KEYFILE", "/run/secrets/vault_key"))


class KontoConfig(BaseModel):
    iban: str
    name: str = ""
    aktiv: bool = True

    @field_validator("iban")
    @classmethod
    def _norm_iban(cls, v: str) -> str:
        return re.sub(r"\s+", "", v).upper()


class BankConfig(BaseModel):
    id: str
    name: str
    typ: Literal["fints", "demo"] = "fints"
    blz: str = ""
    url: str = ""
    # Sicherheitsfunktion des TAN-Verfahrens, z. B. "940" (wird beim Verbindungstest angeboten)
    tan_verfahren: str | None = None
    tan_medium: str | None = None
    pdf_verfahren: Literal["auto", "hkekp", "hkeka"] = "auto"
    camt_ablegen: bool = False
    aktiv: bool = True
    konten: list[KontoConfig] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", v):
            raise ValueError("id: nur a-z, 0-9, - und _ (max. 32 Zeichen)")
        return v

    def konto(self, iban: str) -> KontoConfig | None:
        iban = re.sub(r"\s+", "", iban).upper()
        return next((k for k in self.konten if k.iban == iban), None)


class AblageConfig(BaseModel):
    pfad: str = "/data/ablage"
    # Nur zur Anzeige in der Oberfläche, z. B. \\NAS\Finanzen\Kontoauszuege
    anzeige_pfad: str = ""
    muster: str = "{jahr}/{bank}/{jahr}-{monat}_{bank}_{kontoname}-{iban4}_Nr{nr}.pdf"
    camt_muster: str = "{jahr}/{bank}/{jahr}-{monat}_{bank}_{kontoname}-{iban4}_Umsaetze.camt.xml"
    paperless_consume: str = ""
    dateirechte: str = "0640"


class ZeitplanConfig(BaseModel):
    modus: Literal["taeglich", "woechentlich", "monatlich", "manuell"] = "taeglich"
    uhrzeit: str = "06:30"
    wochentag: int = 0  # 0 = Montag
    monatstag: int = 1

    @field_validator("uhrzeit")
    @classmethod
    def _zeit(cls, v: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("Uhrzeit im Format HH:MM")
        return v


class BenachrichtigungConfig(BaseModel):
    # Home Assistant, z. B. http://homeassistant.local:8123 und notify.mobile_app_handy
    ha_url: str = ""
    ha_service: str = ""


class AppConfig(BaseModel):
    ablage: AblageConfig = Field(default_factory=AblageConfig)
    zeitplan: ZeitplanConfig = Field(default_factory=ZeitplanConfig)
    benachrichtigung: BenachrichtigungConfig = Field(default_factory=BenachrichtigungConfig)
    tan_timeout_sekunden: int = 300
    banken: list[BankConfig] = Field(default_factory=list)

    def bank(self, bank_id: str) -> BankConfig | None:
        return next((b for b in self.banken if b.id == bank_id), None)


class ConfigStore:
    """Lädt und speichert config.yaml (atomar, threadsicher)."""

    def __init__(self, path: Path | None = None):
        self.path = path or config_path()
        self._lock = threading.RLock()
        self._cfg = self._load()

    def _load(self) -> AppConfig:
        if self.path.exists():
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            return AppConfig.model_validate(raw)
        return AppConfig()

    def get(self) -> AppConfig:
        with self._lock:
            return self._cfg.model_copy(deep=True)

    def save(self, cfg: AppConfig) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            text = yaml.safe_dump(cfg.model_dump(mode="json"), allow_unicode=True, sort_keys=False)
            tmp.write_text("# Auszugsbote – Konfiguration (keine Geheimnisse!)\n" + text, encoding="utf-8")
            os.replace(tmp, self.path)
            self._cfg = cfg.model_copy(deep=True)

    def update(self, fn: Callable[[AppConfig], None]) -> AppConfig:
        with self._lock:
            cfg = self.get()
            fn(cfg)
            cfg = AppConfig.model_validate(cfg.model_dump())
            self.save(cfg)
            return cfg
