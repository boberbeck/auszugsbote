"""Ablage im freigegebenen Ordner: Dateinamen aus Muster, atomares Schreiben, Duplikate."""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import AblageConfig

# Präfix für temporäre Dateien. "._" ignorieren Paperless (Consume) und macOS-Finder standardmäßig.
TMP_PREFIX = "._auszugsbote_"
PLATZHALTER = ["jahr", "monat", "tag", "datum", "bank", "kontoname", "iban", "iban4", "nr"]
_VERBOTEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class AblageError(Exception):
    pass


@dataclass
class DateiInfo:
    bank: str
    kontoname: str
    iban: str
    jahr: int
    nr: int
    datum: dt.date  # maßgebliches Datum (Ende des Berichtszeitraums, sonst Erstellung)


def _clean(value: str) -> str:
    value = _VERBOTEN.sub("-", str(value)).strip().strip(".")
    value = re.sub(r"\s+", "-", value)
    return value or "unbekannt"


def render_relpath(muster: str, info: DateiInfo) -> PurePosixPath:
    """Baut den relativen Pfad aus dem Muster. "/" und "\\" trennen Ordner."""
    werte = {
        "jahr": f"{info.jahr:04d}",
        "monat": f"{info.datum.month:02d}",
        "tag": f"{info.datum.day:02d}",
        "datum": info.datum.isoformat(),
        "bank": _clean(info.bank),
        "kontoname": _clean(info.kontoname or "Konto"),
        "iban": _clean(info.iban),
        "iban4": _clean(info.iban[-4:] if info.iban else "0000"),
        "nr": f"{info.nr:02d}",
    }
    unbekannt = set(re.findall(r"{(\w+)}", muster)) - set(werte)
    if unbekannt:
        raise AblageError(f"Unbekannte Platzhalter im Muster: {', '.join(sorted(unbekannt))}")
    teile = [t for t in re.split(r"[\\/]+", muster) if t]
    if not teile:
        raise AblageError("Muster ist leer")
    out = []
    for teil in teile:
        gerendert = teil.format(**werte)
        if gerendert in (".", "..") or not gerendert.strip():
            raise AblageError(f"Ungültiger Pfadteil im Muster: {teil!r}")
        out.append(_VERBOTEN.sub("-", gerendert))
    return PurePosixPath(*out)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(target: Path, data: bytes, mode: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{TMP_PREFIX}{secrets.token_hex(4)}_{target.name}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass  # manche SMB/CIFS-Mounts erlauben kein chmod
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


class Ablage:
    def __init__(self, cfg: AblageConfig):
        self.cfg = cfg
        self.root = Path(cfg.pfad)

    def check(self) -> tuple[bool, str]:
        """Prüft, ob der Ordner existiert und beschreibbar ist."""
        if not self.root.is_dir():
            return False, f"Ordner {self.root} existiert nicht (ist die Freigabe eingehängt?)"
        probe = self.root / f"{TMP_PREFIX}probe"
        try:
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as exc:
            return False, f"Ordner ist nicht beschreibbar: {exc}"
        try:
            st = os.statvfs(self.root)
            frei = st.f_bavail * st.f_frsize
            return True, f"beschreibbar · {_human(frei)} frei"
        except OSError:
            return True, "beschreibbar"

    def save(self, relpath: PurePosixPath, data: bytes) -> PurePosixPath:
        """Schreibt die Datei. Existiert eine andere Datei gleichen Namens, wird _2, _3 … angehängt.
        Gibt den tatsächlich verwendeten relativen Pfad zurück."""
        mode = int(self.cfg.dateirechte, 8)
        target = self.root / relpath
        if not target.resolve().is_relative_to(self.root.resolve()):
            raise AblageError("Pfad zeigt aus dem Ablageordner heraus")
        n = 1
        while target.exists():
            if target.read_bytes() == data:
                return PurePosixPath(target.relative_to(self.root).as_posix())  # identisch, schon da
            n += 1
            stem, suffix = _split_suffix(relpath.name)
            target = self.root / relpath.parent / f"{stem}_{n}{suffix}"
        _atomic_write(target, data, mode)
        if self.cfg.paperless_consume:
            consume = Path(self.cfg.paperless_consume)
            if consume.is_dir():
                _atomic_write(consume / target.name, data, mode)
        return PurePosixPath(target.relative_to(self.root).as_posix())

    def tree(self, max_files: int = 12) -> list[str]:
        """Die zuletzt geänderten Dateien (relativ), für die Übersicht."""
        if not self.root.is_dir():
            return []
        files = []
        for p in self.root.rglob("*"):
            if p.is_file() and not p.name.startswith(TMP_PREFIX) and not p.name.startswith("."):
                try:
                    files.append((p.stat().st_mtime, p.relative_to(self.root).as_posix()))
                except OSError:
                    continue
        files.sort(reverse=True)
        return sorted(f for _, f in files[:max_files])

    def count_files(self) -> int:
        if not self.root.is_dir():
            return 0
        return sum(1 for p in self.root.rglob("*") if p.is_file() and not p.name.startswith("."))


def _split_suffix(name: str) -> tuple[str, str]:
    for suf in (".camt.xml", ".pdf", ".xml", ".sta"):
        if name.lower().endswith(suf):
            return name[: -len(suf)], name[-len(suf):]
    stem, dot, ext = name.rpartition(".")
    return (stem, dot + ext) if dot else (name, "")


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}".replace(",", "X").replace(".", ",").replace("X", ".")
        n /= 1024
    return str(n)
