import re
from pathlib import Path

from fastapi.testclient import TestClient
from fints.models import SEPAAccount
from fints.parser import FinTS3Serializer

from auszugsbote.banks.fints_bank import HKEKP1, HKEKP2, find_pdf
from auszugsbote.core import App
from auszugsbote.web.app import create_app


def test_hkekp_segment_renders():
    acc = SEPAAccount(iban="DE89370400440532013000", bic="COBADEFFXXX", accountnumber="532013000",
                      subaccount=None, blz="37040044")
    for cls in (HKEKP1, HKEKP2):
        seg = cls(account=cls._fields["account"].type.from_sepa_account(acc), statement_number=11,
                  statement_year=2026)
        seg.header.number = 3
        raw = FinTS3Serializer().serialize_message(seg)
        assert raw.startswith(f"HKEKP:3:{cls.VERSION}".encode()), raw
        assert b"DE89370400440532013000" in raw and raw.endswith(b"+11+2026'")


def test_find_pdf_in_nested_data():
    class Fake:
        _fields = {"header": None, "data": None}
        header = None
        data = None
        _additional_data = [b"xx", [b"%PDF-1.4 inhalt"]]
    assert find_pdf(Fake()) == b"%PDF-1.4 inhalt"
    assert find_pdf(b"kein pdf") is None


def _csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_web_setup_login_and_csrf(tmp_path: Path):
    core = App(data=tmp_path / "data", keyfile=tmp_path / "nokey", with_scheduler=False)
    with TestClient(create_app(core)) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/setup"
        page = c.get("/setup").text
        r = c.post("/setup", data={"csrf": _csrf(page), "web_password": "passwort-123", "web_password2": "passwort-123",
                                   "master": "master-passwort-xyz", "master2": "master-passwort-xyz"},
                   follow_redirects=False)
        assert r.status_code == 303, r.text
        assert core.vault.initialized and core.vault.mode == "password" and core.vault.unlocked
        assert c.get("/").status_code == 200
        # POST ohne Token wird abgewiesen
        assert c.post("/laeufe", data={"bank": ""}).status_code == 403
        # Abmelden, falsches Passwort, richtiges Passwort
        c.post("/logout", data={"csrf": _csrf(c.get("/tresor").text)})
        login = c.get("/login").text
        assert "Passwort falsch" in c.post("/login", data={"csrf": _csrf(login), "password": "falsch"}).text
        r = c.post("/login", data={"csrf": _csrf(login), "password": "passwort-123"}, follow_redirects=False)
        assert r.status_code == 303
        # Tresor sperren → Entsperren-Seite
        c.post("/tresor/sperren", data={"csrf": _csrf(c.get("/tresor").text)})
        assert not core.vault.unlocked
        assert c.get("/", follow_redirects=False).headers["location"] == "/entsperren"
        unlock = c.get("/entsperren").text
        c.post("/entsperren", data={"csrf": _csrf(unlock), "master": "master-passwort-xyz"})
        assert core.vault.unlocked
