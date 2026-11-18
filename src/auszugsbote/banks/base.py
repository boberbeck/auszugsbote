"""Gemeinsame Typen für Bank-Anbindungen."""
from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol


class Abgebrochen(Exception):
    """Lauf wurde in der Oberfläche abgebrochen."""


class TanTimeout(Exception):
    """Freigabe kam nicht rechtzeitig."""


@dataclass
class Account:
    iban: str
    bic: str | None = None
    product_name: str = ""
    raw: Any = None  # z. B. fints.models.SEPAAccount


@dataclass
class StatementRef:
    account: Account
    year: int
    number: int
    created: dt.date | None = None
    collectable: bool = True
    needs_receipt: bool = False


@dataclass
class Document:
    data: bytes
    kind: str  # "pdf" | "camt"
    method: str  # "HKEKP" | "HKEKA" | "HKCAZ" | "demo"
    period_start: dt.date | None = None
    period_end: dt.date | None = None


@dataclass
class TanPrompt:
    bank_id: str
    bank_name: str
    text: str
    decoupled: bool
    order: str = ""  # welcher Auftrag freigegeben wird
    matrix: tuple[str, bytes] | None = None  # z. B. ("image/png", b"...") für photoTAN
    demo: bool = False

    @property
    def key(self) -> tuple:
        return (self.bank_id, self.text, self.decoupled, self.order)


class TanHandler(Protocol):
    def begin(self, prompt: TanPrompt) -> None: ...
    def wait_tick(self) -> None: ...
    def ask(self, prompt: TanPrompt) -> str: ...
    def clear(self) -> None: ...
    def demo_approved(self) -> bool: ...
    def step(self, text: str, level: str = "info") -> None: ...


@dataclass
class Capabilities:
    konten: bool = False
    auszugsuebersicht: bool = False  # HKKAU
    pdf_hkekp: bool = False
    pdf_hkeka: bool = False
    camt: bool = False  # HKCAZ
    quittung: bool = False  # HKQTG
    tan_verfahren: list[tuple[str, str]] = field(default_factory=list)  # (Code, Name)
    aktuelles_verfahren: str | None = None

    @property
    def pdf(self) -> bool:
        return self.pdf_hkekp or self.pdf_hkeka


class BankSession(ABC):
    """Eine offene Verbindung zu einer Bank. Als Context-Manager verwenden."""

    @abstractmethod
    def __enter__(self) -> "BankSession": ...

    @abstractmethod
    def __exit__(self, exc_type, exc, tb) -> None: ...

    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    def accounts(self) -> list[Account]: ...

    @abstractmethod
    def statement_overview(self, account: Account) -> list[StatementRef]: ...

    @abstractmethod
    def fetch_statement(self, ref: StatementRef) -> Document: ...

    @abstractmethod
    def fetch_camt(self, account: Account, start: dt.date, end: dt.date) -> list[bytes]: ...
