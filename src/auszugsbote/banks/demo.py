"""Demo-Bank: erfundene Konten und Auszüge, simulierte App-Freigabe.

Damit lässt sich Auszugsbote komplett ausprobieren, bevor die FinTS-Produkt-ID da ist.
Alle Daten sind erfunden (Beispiel-IBANs).
"""
from __future__ import annotations

import datetime as dt
import time

from ..config import BankConfig
from ..pdf import simple_pdf
from .base import Account, BankSession, Capabilities, Document, StatementRef, TanHandler, TanPrompt

DEMO_KONTEN = [
    ("DE89370400440532013000", "Girokonto"),
    ("DE02120300000000202051", "Tagesgeld"),
]
AUTO_FREIGABE_SEKUNDEN = 8.0


def _month_back(d: dt.date, n: int) -> dt.date:
    y, m = d.year, d.month - n
    while m <= 0:
        m += 12
        y -= 1
    return dt.date(y, m, 1)


class DemoSession(BankSession):
    def __init__(self, bank: BankConfig, tan: TanHandler, today: dt.date | None = None,
                 auto_freigabe: float = AUTO_FREIGABE_SEKUNDEN):
        self.bank = bank
        self.tan = tan
        self.today = today or dt.date.today()
        self.auto_freigabe = auto_freigabe
        self._approved = False

    def __enter__(self) -> "DemoSession":
        self.tan.step(f"Verbindung zu {self.bank.name} (Demo) aufgebaut", "ok")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        pass

    def capabilities(self) -> Capabilities:
        return Capabilities(
            konten=True, auszugsuebersicht=True, pdf_hkekp=True, pdf_hkeka=True, camt=False,
            tan_verfahren=[("900", "Demo-App (entkoppelt)")], aktuelles_verfahren="900",
        )

    def accounts(self) -> list[Account]:
        return [Account(iban=i, bic="DEMODEMOXXX", product_name=n) for i, n in DEMO_KONTEN]

    def statement_overview(self, account: Account) -> list[StatementRef]:
        refs = []
        for back in (3, 2, 1):
            first = _month_back(self.today, back - 1)
            created = first + dt.timedelta(days=4)
            if created > self.today:
                continue
            refs.append(StatementRef(account=account, year=created.year, number=created.month, created=created))
        return refs

    def _freigabe(self) -> None:
        if self._approved:
            return
        prompt = TanPrompt(self.bank.id, self.bank.name, "Demo: Kontoauszüge abrufen freigeben",
                           decoupled=True, order="PDF-Auszug (HKEKP)", demo=True)
        self.tan.begin(prompt)
        start = time.monotonic()
        while not self.tan.demo_approved():
            if time.monotonic() - start >= self.auto_freigabe:
                self.tan.step("Demo: Freigabe automatisch erteilt", "info")
                break
            self.tan.wait_tick()
        self.tan.clear()
        self._approved = True

    def fetch_statement(self, ref: StatementRef) -> Document:
        self._freigabe()
        created = ref.created or self.today
        end = created - dt.timedelta(days=created.day)  # letzter Tag des Vormonats
        start = end.replace(day=1)
        lines = [
            (18, f"{self.bank.name} – Kontoauszug {ref.number:02d}/{ref.year}"),
            (11, "DEMO – erfundene Daten, kein echter Kontoauszug"),
            (11, f"Konto: {ref.account.product_name}  IBAN {ref.account.iban}"),
            (11, f"Zeitraum: {start:%d.%m.%Y} – {end:%d.%m.%Y}"),
            (11, "Alter Kontostand                1.234,56 EUR"),
            (11, f"{start:%d.%m.} Gehalt                     +2.500,00 EUR"),
            (11, f"{start + dt.timedelta(days=2):%d.%m.} Miete                      -950,00 EUR"),
            (11, f"{start + dt.timedelta(days=9):%d.%m.} Supermarkt                  -84,37 EUR"),
            (11, "Neuer Kontostand                2.700,19 EUR"),
        ]
        pdf = simple_pdf(lines, title=f"Kontoauszug {ref.number:02d}/{ref.year}")
        return Document(data=pdf, kind="pdf", method="demo", period_start=start, period_end=end)

    def fetch_camt(self, account: Account, start: dt.date, end: dt.date) -> list[bytes]:
        return []
