"""Abruf-Läufe: pro Bank verbinden, neue Auszüge holen, ablegen. Läuft im Hintergrund-Thread."""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import notify
from .banks.base import Abgebrochen, Account, BankSession, Document, TanPrompt, TanTimeout
from .banks.demo import DemoSession
from .config import BankConfig, KontoConfig
from .storage import Ablage, DateiInfo, render_relpath, sha256

if TYPE_CHECKING:
    from .core import App

log = logging.getLogger(__name__)


class Busy(Exception):
    pass


@dataclass
class RunEvent:
    ts: float
    level: str  # info | ok | warn | error | tan
    text: str


@dataclass
class TestResult:
    bank_id: str
    ok: bool
    when: float
    dauer: float = 0.0
    fehler: str = ""
    caps: dict = field(default_factory=dict)
    tan_verfahren: list = field(default_factory=list)
    konten: list = field(default_factory=list)  # (iban, produkt, abholbereit|None)

    def abholbereit(self, iban: str) -> int | None:
        return next((n for i, _p, n in self.konten if i == iban), None)


class Run:
    """Ein Lauf. Implementiert zugleich das TanHandler-Protokoll für die Bank-Sitzungen."""

    TICK = 3.0

    def __init__(self, app: "App", run_id: int, trigger: str, mode: str, timeout: int):
        self.app = app
        self.id = run_id
        self.trigger = trigger
        self.mode = mode  # abruf | test
        self.timeout = timeout
        self.started = time.time()
        self.finished: float | None = None
        self.status = "laeuft"
        self.events: list[RunEvent] = []
        self.new_files: list[str] = []
        self.errors: list[str] = []
        self.bank_id: str | None = None
        self.pending: TanPrompt | None = None
        self.pending_since = 0.0
        self._notified: set[str] = set()
        self._tan_value: str | None = None
        self._demo_ok = False
        self._cancel = threading.Event()
        self._wake = threading.Event()

    # -- TanHandler -------------------------------------------------------------
    def step(self, text: str, level: str = "info") -> None:
        self.events.append(RunEvent(time.time(), level, text))
        self.app.state.log("warn" if level == "warn" else "error" if level == "error" else "info",
                           text, run_id=self.id, bank_id=self.bank_id)

    def begin(self, prompt: TanPrompt) -> None:
        if self.pending is not None and self.pending.key == prompt.key:
            return
        self.pending = prompt
        self.pending_since = time.time()
        self._tan_value = None
        self._demo_ok = False
        how = "in der Banking-App bestätigen" if prompt.decoupled else "TAN eingeben"
        self.step(f"{prompt.bank_name}: Freigabe nötig – {how} ({prompt.order})", "tan")
        if prompt.bank_id not in self._notified:
            self._notified.add(prompt.bank_id)
            threading.Thread(
                target=notify.send,
                args=(self.app.config.get(), self.app.vault, "Auszugsbote: Freigabe nötig",
                      f"{prompt.bank_name}: bitte {how}."),
                daemon=True,
            ).start()

    def _check(self) -> None:
        if self._cancel.is_set():
            raise Abgebrochen("Lauf abgebrochen")
        if self.pending and time.time() - self.pending_since > self.timeout:
            raise TanTimeout(f"Keine Freigabe innerhalb von {self.timeout // 60} Minuten")

    def wait_tick(self) -> None:
        self._check()
        self._wake.wait(self.TICK)
        self._wake.clear()
        self._check()

    def ask(self, prompt: TanPrompt) -> str:
        self.begin(prompt)
        while True:
            self._check()
            if self._tan_value:
                v, self._tan_value = self._tan_value, None
                return v
            self._wake.wait(1.0)
            self._wake.clear()

    def clear(self) -> None:
        if self.pending is not None:
            self.step(f"{self.pending.bank_name}: Freigabe erteilt", "ok")
        self.pending = None

    def demo_approved(self) -> bool:
        return self._demo_ok

    # -- aus der Oberfläche -------------------------------------------------------
    def submit_tan(self, tan: str) -> None:
        self._tan_value = tan.strip()
        self._wake.set()

    def approve_demo(self) -> None:
        self._demo_ok = True
        self._wake.set()

    def cancel(self) -> None:
        self._cancel.set()
        self._wake.set()

    @property
    def running(self) -> bool:
        return self.finished is None

    @property
    def tan_remaining(self) -> int:
        if not self.pending:
            return 0
        return max(0, int(self.timeout - (time.time() - self.pending_since)))


class Runner:
    def __init__(self, app: "App"):
        self.app = app
        self._lock = threading.Lock()
        self.current: Run | None = None
        self.tests: dict[str, TestResult] = {}

    @property
    def busy(self) -> bool:
        return self.current is not None and self.current.running

    def start(self, trigger: str = "manuell", bank_ids: list[str] | None = None, mode: str = "abruf") -> Run:
        with self._lock:
            if self.busy:
                raise Busy("Es läuft bereits ein Abruf")
            if not self.app.vault.unlocked:
                raise RuntimeError("Tresor ist gesperrt")
            cfg = self.app.config.get()
            run_id = self.app.state.start_run(trigger if mode == "abruf" else "test")
            run = Run(self.app, run_id, trigger, mode, cfg.tan_timeout_sekunden)
            self.current = run
        t = threading.Thread(target=self._main, args=(run, bank_ids), name=f"lauf-{run_id}", daemon=True)
        t.start()
        return run

    def wait(self, timeout: float = 60) -> Run | None:
        """Für Tests/CLI: wartet, bis der aktuelle Lauf fertig ist."""
        end = time.time() + timeout
        while self.busy and time.time() < end:
            time.sleep(0.05)
        return self.current

    # -- Ablauf -----------------------------------------------------------------
    def _main(self, run: Run, bank_ids: list[str] | None) -> None:
        cfg = self.app.config.get()
        banks = [b for b in cfg.banken if (bank_ids is None and b.aktiv) or (bank_ids and b.id in bank_ids)]
        ok_banks = 0
        try:
            ablage = Ablage(cfg.ablage)
            if run.mode == "abruf":
                good, msg = ablage.check()
                if not good:
                    raise RuntimeError(msg)
            if not banks:
                run.step("Keine aktiven Bankzugänge eingerichtet", "warn")
            for bank in banks:
                run.bank_id = bank.id
                t0 = time.time()
                try:
                    if run.mode == "test":
                        self._test_bank(run, bank, t0)
                    else:
                        self._fetch_bank(run, bank, ablage)
                    ok_banks += 1
                except Abgebrochen:
                    raise
                except Exception as exc:  # noqa: BLE001
                    msg = f"{bank.name}: {exc}"
                    run.errors.append(msg)
                    run.step(msg, "error")
                    log.debug("Fehler bei %s\n%s", bank.id, traceback.format_exc())
                    if run.mode == "test":
                        self.tests[bank.id] = TestResult(bank.id, False, time.time(), time.time() - t0, str(exc))
                finally:
                    run.bank_id = None
                    run.pending = None
            if run.errors:
                run.status = "teilweise" if ok_banks else "fehler"
            else:
                run.status = "ok"
        except Abgebrochen:
            run.status = "abgebrochen"
            run.step("Lauf abgebrochen", "warn")
        except Exception as exc:  # noqa: BLE001
            run.status = "fehler"
            run.errors.append(str(exc))
            run.step(str(exc), "error")
        finally:
            run.finished = time.time()
            summary = self._summary(run)
            self.app.state.finish_run(run.id, run.status, summary)
            if run.mode == "abruf" and (run.new_files or run.errors) and run.trigger == "zeitplan":
                notify.send(self.app.config.get(), self.app.vault, "Auszugsbote", summary)

    @staticmethod
    def _summary(run: Run) -> str:
        if run.mode == "test":
            return "Verbindungstest " + ("erfolgreich" if not run.errors else "fehlgeschlagen")
        parts = [f"{len(run.new_files)} neue Datei(en) abgelegt"]
        if run.errors:
            parts.append(f"{len(run.errors)} Fehler")
        return ", ".join(parts)

    def session_for(self, bank: BankConfig, run: Run) -> BankSession:
        if bank.typ == "demo":
            return DemoSession(bank, run)
        from .banks.fints_bank import FinTSSession  # spät importieren (python-fints ist groß)

        v = self.app.vault
        product_id = v.get("fints.product_id")
        if not product_id:
            raise RuntimeError("FinTS-Produkt-ID fehlt (Einstellungen). Bis dahin nur Demo-Banken möglich.")
        login, pin = v.get(f"bank.{bank.id}.login"), v.get(f"bank.{bank.id}.pin")
        if not login or not pin:
            raise RuntimeError("Anmeldename oder PIN fehlt im Tresor")
        if not bank.blz or not bank.url:
            raise RuntimeError("BLZ oder FinTS-Adresse fehlt")
        return FinTSSession(
            bank, login=login, pin=pin, product_id=product_id, tan=run,
            customer_id=v.get(f"bank.{bank.id}.customer_id"),
            state_blob=v.get_bytes(f"bank.{bank.id}.state"),
            save_state=lambda blob: v.set(f"bank.{bank.id}.state", blob),
        )

    def _sync_accounts(self, bank: BankConfig, accounts: list[Account]) -> None:
        """Neu entdeckte Konten in die Konfiguration übernehmen (standardmäßig aktiv)."""
        known = {k.iban for k in bank.konten}
        neu = [KontoConfig(iban=a.iban, name=a.product_name or "Konto") for a in accounts if a.iban not in known]
        if not neu:
            return

        def upd(cfg):
            cfg.bank(bank.id).konten.extend(neu)
        self.app.config.update(upd)
        bank.konten.extend(neu)

    def _fetch_bank(self, run: Run, bank: BankConfig, ablage: Ablage) -> None:
        with self.session_for(bank, run) as s:
            caps = s.capabilities()
            accounts = s.accounts()
            self._sync_accounts(bank, accounts)
            aktive = [a for a in accounts if (k := bank.konto(a.iban)) is None or k.aktiv]
            run.step(f"{bank.name}: {len(accounts)} Konten gelesen, {len(aktive)} aktiv", "ok")
            for acc in aktive:
                k = bank.konto(acc.iban)
                kontoname = (k.name if k and k.name else acc.product_name) or "Konto"
                if caps.auszugsuebersicht and caps.pdf:
                    refs = s.statement_overview(acc)
                    neu = [r for r in refs if r.collectable and not self.app.state.has_statement(
                        bank.id, acc.iban, "pdf", r.year, r.number)]
                    run.step(f"{bank.name} · {kontoname}: {len(neu)} neue Auszüge", "info")
                    for ref in neu:
                        doc = s.fetch_statement(ref)
                        self._store(run, bank, acc, kontoname, doc, ref.year, ref.number,
                                    fallback_date=ref.created, ablage=ablage)
                        if ref.needs_receipt:
                            run.step("Bank erwartet eine Empfangsquittung (HKQTG) – noch nicht unterstützt", "warn")
                elif caps.pdf:
                    run.step(f"{bank.name}: Bank meldet keine Auszugsübersicht (HKKAU) – "
                             "PDF-Abruf ohne Übersicht ist noch nicht umgesetzt", "warn")
                if caps.camt and (bank.camt_ablegen or not caps.pdf):
                    self._fetch_camt(run, s, bank, acc, kontoname, ablage)
                elif not caps.pdf:
                    run.step(f"{bank.name}: weder PDF-Auszüge noch camt-Umsätze verfügbar", "warn")

    def _fetch_camt(self, run: Run, s: BankSession, bank: BankConfig, acc: Account, kontoname: str,
                    ablage: Ablage) -> None:
        today = dt.date.today()
        end = today.replace(day=1) - dt.timedelta(days=1)  # letzter Tag des Vormonats
        start = end.replace(day=1)
        if self.app.state.has_statement(bank.id, acc.iban, "camt", end.year, end.month):
            return
        docs = s.fetch_camt(acc, start, end)
        if not docs:
            run.step(f"{bank.name} · {kontoname}: keine Umsätze für {end:%m/%Y}", "info")
            return
        data = docs[0] if len(docs) == 1 else _join_camt(docs)
        doc = Document(data=data, kind="camt", method="HKCAZ", period_start=start, period_end=end)
        self._store(run, bank, acc, kontoname, doc, end.year, end.month, fallback_date=end, ablage=ablage)

    def _store(self, run: Run, bank: BankConfig, acc: Account, kontoname: str, doc: Document,
               year: int, number: int, fallback_date: dt.date | None, ablage: Ablage) -> None:
        h = sha256(doc.data)
        st = self.app.state
        datum = doc.period_end or fallback_date or dt.date.today()
        if st.has_sha(h):
            run.step(f"{bank.name} · {kontoname}: Auszug {number:02d}/{year} ist schon abgelegt (gleicher Inhalt)",
                     "info")
            relpath = ""
        else:
            info = DateiInfo(bank=bank.name, kontoname=kontoname, iban=acc.iban, jahr=datum.year, nr=number,
                             datum=datum)
            muster = ablage.cfg.muster if doc.kind == "pdf" else ablage.cfg.camt_muster
            rel = ablage.save(render_relpath(muster, info), doc.data)
            relpath = str(rel)
            run.new_files.append(relpath)
            run.step(f"Abgelegt: {relpath}", "ok")
        st.add_statement(
            bank_id=bank.id, iban=acc.iban, kind=doc.kind, year=year, number=number, method=doc.method,
            relpath=relpath, sha256=h, size=len(doc.data),
            period_start=doc.period_start.isoformat() if doc.period_start else None,
            period_end=doc.period_end.isoformat() if doc.period_end else None,
            run_id=run.id,
        )

    def _test_bank(self, run: Run, bank: BankConfig, t0: float) -> None:
        with self.session_for(bank, run) as s:
            caps = s.capabilities()
            accounts = s.accounts()
            konten = []
            for a in accounts:
                n = None
                if caps.auszugsuebersicht:
                    try:
                        refs = s.statement_overview(a)
                        n = sum(1 for r in refs if r.collectable and not self.app.state.has_statement(
                            bank.id, a.iban, "pdf", r.year, r.number))
                    except Exception as exc:  # noqa: BLE001
                        run.step(f"Auszugsübersicht für {a.iban[-4:]}: {exc}", "warn")
                konten.append((a.iban, a.product_name, n))
            self._sync_accounts(bank, accounts)
            if not bank.tan_verfahren and caps.aktuelles_verfahren:
                def upd(cfg):
                    cfg.bank(bank.id).tan_verfahren = caps.aktuelles_verfahren
                self.app.config.update(upd)
            run.step(f"{bank.name}: Verbindungstest erfolgreich, {len(accounts)} Konten", "ok")
            self.tests[bank.id] = TestResult(
                bank.id, True, time.time(), time.time() - t0,
                caps={k: getattr(caps, k) for k in ("konten", "auszugsuebersicht", "pdf_hkekp", "pdf_hkeka",
                                                    "camt", "quittung")},
                tan_verfahren=caps.tan_verfahren, konten=konten,
            )


def _join_camt(docs: list[bytes]) -> bytes:
    """Mehrere camt-Dokumente (Seiten) – einfach hintereinander in einen Container."""
    out = b'<?xml version="1.0" encoding="UTF-8"?>\n<AuszugsboteCamtSammlung>\n'
    for d in docs:
        body = d.split(b"?>", 1)[1] if d.lstrip().startswith(b"<?xml") else d
        out += body.strip() + b"\n"
    return out + b"</AuszugsboteCamtSammlung>\n"
