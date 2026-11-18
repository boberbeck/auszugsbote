"""Tresor für Zugangsdaten.

Aufbau
------
* Ein zufälliger Datenschlüssel (DEK, 256 Bit) verschlüsselt jeden Eintrag einzeln
  mit AES-256-GCM. Der Name des Eintrags ist als "associated data" gebunden, damit
  sich Chiffrate nicht unbemerkt zwischen Einträgen vertauschen lassen.
* Der DEK selbst liegt nur verschlüsselt ("gewrappt") in der Datenbank. Der
  Hauptschlüssel (KEK) dafür kommt entweder
    - aus einer Schlüsseldatei (32 Byte Zufall, Base64) – für den automatischen Betrieb,
      typischerweise ein Docker-Secret, oder
    - per Argon2id aus einem Master-Passwort – dann muss nach jedem Neustart entsperrt werden.
* Entsperrt liegt der DEK nur im Arbeitsspeicher.

Gespeicherte Namen (Konvention):
    fints.product_id            FinTS-Produkt-ID der Deutschen Kreditwirtschaft
    bank.<id>.login             Anmeldename / Teilnehmernummer
    bank.<id>.customer_id       optional abweichende Kundenkennung
    bank.<id>.pin               PIN / Passwort
    bank.<id>.state             gespeicherter FinTS-Zustand (python-fints deconstruct)
    notify.ha_token             Home-Assistant-Token
"""
from __future__ import annotations

import base64
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DEK_AAD = b"auszugsbote:dek:v1"
ARGON2 = {"time_cost": 3, "memory_cost": 64 * 1024, "parallelism": 4}  # 64 MiB


class VaultError(Exception):
    pass


class VaultLocked(VaultError):
    pass


def generate_keyfile_content() -> str:
    """Inhalt für eine neue Schlüsseldatei (Base64 von 32 Zufallsbytes)."""
    return base64.b64encode(secrets.token_bytes(32)).decode() + "\n"


def read_keyfile(path: Path) -> bytes:
    raw = path.read_bytes().strip()
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise VaultError(f"Schlüsseldatei {path} ist kein gültiges Base64") from exc
    if len(key) != 32:
        raise VaultError(f"Schlüsseldatei {path} muss 32 Byte enthalten (hat {len(key)})")
    return key


def _kek_from_password(password: str, salt: bytes, params: dict) -> bytes:
    return hash_secret_raw(
        password.encode("utf-8"), salt, hash_len=32, type=Type.ID,
        time_cost=params["time_cost"], memory_cost=params["memory_cost"], parallelism=params["parallelism"],
    )


class Vault:
    def __init__(self, path: Path):
        self.path = path
        self._dek: bytes | None = None
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value BLOB)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY, nonce BLOB NOT NULL,"
                " ct BLOB NOT NULL, updated REAL NOT NULL)"
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # -- intern -------------------------------------------------------------
    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.isolation_level = None  # autocommit; Transaktionen explizit
        return db

    def _meta(self, key: str) -> bytes | None:
        with self._db() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, db: sqlite3.Connection, key: str, value: bytes | str) -> None:
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    # -- Status -------------------------------------------------------------
    @property
    def initialized(self) -> bool:
        return self._meta("wrapped_dek") is not None

    @property
    def mode(self) -> str | None:
        m = self._meta("mode")
        return m.decode() if isinstance(m, bytes) else m

    @property
    def unlocked(self) -> bool:
        return self._dek is not None

    # -- Einrichten / Entsperren --------------------------------------------
    def initialize(self, *, keyfile_key: bytes | None = None, password: str | None = None) -> None:
        if self.initialized:
            raise VaultError("Tresor ist bereits eingerichtet")
        if (keyfile_key is None) == (password is None):
            raise VaultError("Genau eine Schlüsselquelle angeben: Schlüsseldatei oder Master-Passwort")
        dek = secrets.token_bytes(32)
        salt = secrets.token_bytes(16)
        if keyfile_key is not None:
            mode, kek = "keyfile", keyfile_key
        else:
            if len(password) < 12:
                raise VaultError("Master-Passwort muss mindestens 12 Zeichen haben")
            mode, kek = "password", _kek_from_password(password, salt, ARGON2)
        nonce = secrets.token_bytes(12)
        wrapped = AESGCM(kek).encrypt(nonce, dek, DEK_AAD)
        with self._lock, self._db() as db:
            db.execute("BEGIN")
            self._set_meta(db, "version", "1")
            self._set_meta(db, "mode", mode)
            self._set_meta(db, "salt", salt)
            self._set_meta(db, "argon2", f"{ARGON2['time_cost']},{ARGON2['memory_cost']},{ARGON2['parallelism']}")
            self._set_meta(db, "wrapped_dek", nonce + wrapped)
            db.execute("COMMIT")
            self._dek = dek

    def _unwrap(self, kek: bytes) -> None:
        blob = self._meta("wrapped_dek")
        if blob is None:
            raise VaultError("Tresor ist noch nicht eingerichtet")
        try:
            dek = AESGCM(kek).decrypt(blob[:12], blob[12:], DEK_AAD)
        except Exception as exc:  # noqa: BLE001 – InvalidTag
            raise VaultError("Falscher Schlüssel bzw. falsches Master-Passwort") from exc
        with self._lock:
            self._dek = dek

    def unlock_with_keyfile(self, key: bytes) -> None:
        if self.mode != "keyfile":
            raise VaultError("Tresor ist mit Master-Passwort eingerichtet, nicht mit Schlüsseldatei")
        self._unwrap(key)

    def unlock_with_password(self, password: str) -> None:
        if self.mode != "password":
            raise VaultError("Tresor ist mit Schlüsseldatei eingerichtet, nicht mit Master-Passwort")
        raw = self._meta("argon2")
        params = raw.decode() if isinstance(raw, bytes) else raw
        t, m, p = (int(x) for x in params.split(","))
        kek = _kek_from_password(password, self._meta("salt"), {"time_cost": t, "memory_cost": m, "parallelism": p})
        self._unwrap(kek)

    def lock(self) -> None:
        with self._lock:
            self._dek = None

    # -- Einträge -----------------------------------------------------------
    def _require(self) -> bytes:
        dek = self._dek
        if dek is None:
            raise VaultLocked("Tresor ist gesperrt")
        return dek

    def set(self, name: str, value: bytes | str) -> None:
        data = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        dek = self._require()
        nonce = secrets.token_bytes(12)
        ct = AESGCM(dek).encrypt(nonce, data, name.encode("utf-8"))
        with self._lock, self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO secrets(name, nonce, ct, updated) VALUES (?, ?, ?, ?)",
                (name, nonce, ct, time.time()),
            )

    def get_bytes(self, name: str) -> bytes | None:
        dek = self._require()
        with self._db() as db:
            row = db.execute("SELECT nonce, ct FROM secrets WHERE name=?", (name,)).fetchone()
        if not row:
            return None
        try:
            return AESGCM(dek).decrypt(row[0], row[1], name.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise VaultError(f"Eintrag {name!r} ist beschädigt oder wurde manipuliert") from exc

    def get(self, name: str) -> str | None:
        b = self.get_bytes(name)
        return b.decode("utf-8") if b is not None else None

    def has(self, name: str) -> bool:
        with self._db() as db:
            return db.execute("SELECT 1 FROM secrets WHERE name=?", (name,)).fetchone() is not None

    def delete(self, name: str) -> None:
        with self._lock, self._db() as db:
            db.execute("DELETE FROM secrets WHERE name=?", (name,))

    def delete_prefix(self, prefix: str) -> None:
        with self._lock, self._db() as db:
            db.execute("DELETE FROM secrets WHERE substr(name, 1, ?) = ?", (len(prefix), prefix))

    def entries(self) -> list[tuple[str, float]]:
        """Nur Namen und Änderungszeit – niemals Werte."""
        with self._db() as db:
            return [(r[0], r[1]) for r in db.execute("SELECT name, updated FROM secrets ORDER BY name")]
