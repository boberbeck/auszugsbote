"""FinTS-Anbindung über python-fints (PIN/TAN).

* Auszugsübersicht: HKKAU (python-fints: get_statements)
* PDF-Auszug:       HKEKP (eigenes Segment, s. u.) oder HKEKA im Format PDF (python-fints: get_statement)
* Umsätze:          HKCAZ als camt-XML (python-fints: get_transactions_xml)
* Freigaben:        entkoppelt per Banking-App (polling) oder TAN-Eingabe in der Oberfläche

HKEKP ist in python-fints 5.0.1 nicht implementiert (nur in FinTSOperations gelistet).
Der Segmentaufbau hier folgt dem von HKEKA ohne Formatfeld und ist EXPERIMENTELL –
er muss gegen eine echte Bank (z. B. DKB) geprüft werden. Scheitert HKEKP, wird auf
HKEKA (PDF) zurückgefallen.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Callable

from fints.client import FinTS3PinTanClient, NeedTANResponse
from fints.exceptions import FinTSUnsupportedOperation
from fints.fields import DataElementField, DataElementGroupField
from fints.formals import KTI1, StatementFormat
from fints.segments.base import FinTS3Segment

from ..config import BankConfig
from .base import (
    Abgebrochen, Account, BankSession, Capabilities, Document, StatementRef, TanHandler, TanPrompt, TanTimeout,
)

log = logging.getLogger(__name__)


# --- eigenes Segment: Kontoauszug im PDF-Format anfordern (experimentell) -----------------
class HKEKP1(FinTS3Segment):
    """Kontoauszug PDF anfordern, Version 1 (experimentell)"""
    account = DataElementGroupField(type=KTI1, _d="Kontoverbindung international")
    statement_number = DataElementField(type="num", max_length=5, required=False, _d="Kontoauszugsnummer")
    statement_year = DataElementField(type="num", length=4, required=False, _d="Kontoauszugsjahr")
    max_number_responses = DataElementField(type="num", max_length=4, required=False, _d="Maximale Anzahl Einträge")
    touchdown_point = DataElementField(type="an", max_length=35, required=False, _d="Aufsetzpunkt")


class HKEKP2(FinTS3Segment):
    """Kontoauszug PDF anfordern, Version 2 (experimentell)"""
    account = DataElementGroupField(type=KTI1, _d="Kontoverbindung international")
    statement_number = DataElementField(type="num", max_length=5, required=False, _d="Kontoauszugsnummer")
    statement_year = DataElementField(type="num", length=4, required=False, _d="Kontoauszugsjahr")
    max_number_responses = DataElementField(type="num", max_length=4, required=False, _d="Maximale Anzahl Einträge")
    touchdown_point = DataElementField(type="an", max_length=35, required=False, _d="Aufsetzpunkt")


def find_pdf(obj, _depth: int = 0) -> bytes | None:
    """Sucht in einem (ggf. generisch geparsten) Segment nach PDF-Bytes."""
    if _depth > 6 or obj is None:
        return None
    if isinstance(obj, (bytes, bytearray)):
        b = bytes(obj)
        i = b.find(b"%PDF")
        return b[i:] if 0 <= i < 1024 else None
    if isinstance(obj, (list, tuple)):
        for x in obj:
            r = find_pdf(x, _depth + 1)
            if r:
                return r
        return None
    for name in ("data", "_additional_data"):
        r = find_pdf(getattr(obj, name, None), _depth + 1)
        if r:
            return r
    fields = getattr(obj, "_fields", None)
    if fields:
        for name in fields:
            if name == "header":
                continue
            r = find_pdf(getattr(obj, name, None), _depth + 1)
            if r:
                return r
    return None


def _period(seg) -> tuple[dt.date | None, dt.date | None]:
    p = getattr(seg, "statement_period", None)
    if p is None:
        return None, None
    return getattr(p, "start_date", None), getattr(p, "end_date", None)


class AuszugsboteClient(FinTS3PinTanClient):
    """python-fints-Client mit zusätzlichem HKEKP-Abruf."""

    def _get_statement_pdf(self, command_seg, response):
        for resp in response.response_segments(command_seg, "HIEKP"):
            return resp
        return None

    def get_statement_pdf(self, account, number: int | None = None, year: int | None = None):
        with self._get_dialog() as dialog:
            hkekp = self._find_highest_supported_command(HKEKP1, HKEKP2)
            seg = hkekp(
                account=hkekp._fields["account"].type.from_sepa_account(account),
                statement_number=number,
                statement_year=year,
            )
            return self._send_with_possible_retry(dialog, seg, self._get_statement_pdf)


class FinTSSession(BankSession):
    def __init__(
        self,
        bank: BankConfig,
        *,
        login: str,
        pin: str,
        product_id: str,
        tan: TanHandler,
        customer_id: str | None = None,
        state_blob: bytes | None = None,
        save_state: Callable[[bytes], None] | None = None,
    ):
        self.bank = bank
        self.tan = tan
        self.save_state = save_state
        self.client = AuszugsboteClient(
            bank.blz, login, pin, bank.url,
            customer_id=customer_id or None,
            product_id=product_id,
            product_version="0.1.0",
            from_data=state_blob,
            tan_medium=bank.tan_medium,
        )
        self._entered = False

    # -- Verbindung ------------------------------------------------------------
    def __enter__(self) -> "FinTSSession":
        c = self.client
        if self.bank.tan_verfahren:
            c.set_tan_mechanism(self.bank.tan_verfahren)
        if not c.get_current_tan_mechanism():
            c.fetch_tan_mechanisms()
            if self.bank.tan_verfahren:
                c.set_tan_mechanism(self.bank.tan_verfahren)
        self._select_tan_medium()
        c.__enter__()
        self._entered = True
        if c.init_tan_response:
            self._resolve(c.init_tan_response, "Anmeldung")
        self.tan.step(f"Verbindung zu {self._host()} aufgebaut", "ok")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._entered:
                self.client.__exit__(exc_type, exc, tb)
        finally:
            if self.save_state:
                try:
                    self.save_state(self.client.deconstruct(including_private=True))
                except Exception:  # noqa: BLE001
                    log.exception("FinTS-Zustand konnte nicht gespeichert werden")

    def _host(self) -> str:
        return self.bank.url.split("//", 1)[-1].split("/", 1)[0]

    def _select_tan_medium(self) -> None:
        c = self.client
        try:
            required = c.selected_tan_medium is None and c.is_tan_media_required()
        except KeyError:
            required = False  # Ein-Schritt-Verfahren o. Ä.
        if not required:
            return
        _, media = c.get_tan_media()
        if not media:
            c.selected_tan_medium = ""
            return
        wanted = self.bank.tan_medium
        chosen = next((m for m in media if wanted and m.tan_medium_name == wanted), media[0])
        if len(media) > 1 and not wanted:
            self.tan.step(f"Mehrere TAN-Medien gefunden, nutze „{chosen.tan_medium_name}“", "warn")
        c.set_tan_medium(chosen)

    def _resolve(self, resp, order: str):
        """Wartet Freigaben ab (entkoppelt: Polling, sonst TAN aus der Oberfläche)."""
        while isinstance(resp, NeedTANResponse):
            prompt = TanPrompt(
                bank_id=self.bank.id,
                bank_name=self.bank.name,
                text=resp.challenge or "",
                decoupled=bool(resp.decoupled),
                order=order,
                matrix=resp.challenge_matrix,
            )
            if resp.decoupled:
                self.tan.begin(prompt)
                self.tan.wait_tick()
                resp = self.client.send_tan(resp, "")
            else:
                tan = self.tan.ask(prompt)
                resp = self.client.send_tan(resp, tan)
        self.tan.clear()
        return resp

    # -- Fähigkeiten -------------------------------------------------------------
    def capabilities(self) -> Capabilities:
        bpd = self.client.bpd
        has = lambda name: bpd.find_segment_first(name) is not None  # noqa: E731
        try:
            mechs = [(code, getattr(p, "name", code)) for code, p in self.client.get_tan_mechanisms().items()]
        except Exception:  # noqa: BLE001
            mechs = []
        return Capabilities(
            konten=has("HISPAS"),
            auszugsuebersicht=has("HIKAUS"),
            pdf_hkekp=has("HIEKPS"),
            pdf_hkeka=has("HIEKAS"),
            camt=has("HICAZS"),
            quittung=has("HIQTGS"),
            tan_verfahren=mechs,
            aktuelles_verfahren=self.client.get_current_tan_mechanism(),
        )

    def _account_allows(self, iban: str, transaction: str) -> bool | None:
        """Laut UPD für dieses Konto erlaubt? None = Bank macht keine Angabe."""
        for upd in self.client.upd.find_segments("HIUPD"):
            if (upd.iban or "").replace(" ", "") == iban:
                allowed = [t.transaction for t in (upd.allowed_transactions or [])]
                return (transaction in allowed) if allowed else None
        return None

    # -- Abrufe ------------------------------------------------------------------
    def accounts(self) -> list[Account]:
        accs = self._resolve(self.client.get_sepa_accounts(), "Konten lesen")
        names = {}
        for upd in self.client.upd.find_segments("HIUPD"):
            if upd.iban:
                names[upd.iban.replace(" ", "")] = upd.account_product_name or ""
        return [Account(iban=a.iban, bic=a.bic, product_name=names.get(a.iban, ""), raw=a) for a in accs]

    def statement_overview(self, account: Account) -> list[StatementRef]:
        res = self._resolve(self.client.get_statements(account.raw), "Auszugsübersicht")
        refs = []
        for s in res or []:
            created = getattr(s, "date_created", None)
            year = getattr(s, "year", None) or (created.year if created else dt.date.today().year)
            conf = getattr(s, "confirmation", None)
            refs.append(StatementRef(
                account=account,
                year=int(year),
                number=int(s.statement_number),
                created=created,
                collectable=bool(getattr(s, "collection_possible", True)),
                needs_receipt=getattr(conf, "value", conf) == "2",
            ))
        return refs

    def _methods(self, iban: str) -> list[str]:
        caps = self.capabilities()
        order = {"auto": ["HKEKP", "HKEKA"], "hkekp": ["HKEKP"], "hkeka": ["HKEKA"]}[self.bank.pdf_verfahren]
        out = []
        for m in order:
            supported = caps.pdf_hkekp if m == "HKEKP" else caps.pdf_hkeka
            if supported and self._account_allows(iban, m) is not False:
                out.append(m)
        return out

    def fetch_statement(self, ref: StatementRef) -> Document:
        errors = []
        for method in self._methods(ref.account.iban):
            try:
                if method == "HKEKP":
                    seg = self._resolve(
                        self.client.get_statement_pdf(ref.account.raw, ref.number, ref.year), "PDF-Auszug (HKEKP)"
                    )
                else:
                    seg = self._resolve(
                        self.client.get_statement(ref.account.raw, ref.number, ref.year, StatementFormat.PDF),
                        "Kontoauszug (HKEKA)",
                    )
                pdf = find_pdf(seg)
                if pdf:
                    start, end = _period(seg)
                    return Document(data=pdf, kind="pdf", method=method, period_start=start, period_end=end)
                errors.append(f"{method}: Antwort enthielt kein PDF")
            except FinTSUnsupportedOperation as exc:
                errors.append(f"{method}: nicht unterstützt ({exc})")
            except (Abgebrochen, TanTimeout):
                raise
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{method}: {exc}")
                self.tan.step(f"{method} fehlgeschlagen, versuche Alternative: {exc}", "warn")
        if not errors:
            errors.append("Bank meldet weder HKEKP noch HKEKA für dieses Konto")
        raise RuntimeError("; ".join(errors))

    def fetch_camt(self, account: Account, start: dt.date, end: dt.date) -> list[bytes]:
        booked, _pending = self._resolve(
            self.client.get_transactions_xml(account.raw, start, end), "Umsätze (camt)"
        )
        return [bytes(b) for b in booked if b]
