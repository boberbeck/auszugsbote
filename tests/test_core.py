import datetime as dt
from pathlib import Path, PurePosixPath

import pytest

from auszugsbote.config import AblageConfig, BankConfig
from auszugsbote.core import App
from auszugsbote.pdf import simple_pdf
from auszugsbote.storage import Ablage, AblageError, DateiInfo, render_relpath
from auszugsbote.vault import Vault, VaultError, VaultLocked, generate_keyfile_content, read_keyfile


def test_vault_keyfile_roundtrip(tmp_path: Path):
    kf = tmp_path / "key"
    kf.write_text(generate_keyfile_content())
    v = Vault(tmp_path / "vault.db")
    v.initialize(keyfile_key=read_keyfile(kf))
    v.set("bank.x.pin", "geheim123")
    v.set("bank.x.state", b"\x00\x01binary")
    v2 = Vault(tmp_path / "vault.db")
    with pytest.raises(VaultLocked):
        v2.get("bank.x.pin")
    v2.unlock_with_keyfile(read_keyfile(kf))
    assert v2.get("bank.x.pin") == "geheim123"
    assert v2.get_bytes("bank.x.state") == b"\x00\x01binary"
    # Werte liegen nicht im Klartext in der Datei
    assert b"geheim123" not in (tmp_path / "vault.db").read_bytes()


def test_vault_wrong_key_and_password(tmp_path: Path):
    v = Vault(tmp_path / "vault.db")
    v.initialize(password="ein-langes-master-passwort")
    v.set("a", "b")
    v2 = Vault(tmp_path / "vault.db")
    with pytest.raises(VaultError):
        v2.unlock_with_password("falsch-falsch-falsch")
    v2.unlock_with_password("ein-langes-master-passwort")
    assert v2.get("a") == "b"


def test_vault_entries_bound_to_name(tmp_path: Path):
    """Ein Chiffrat lässt sich nicht unter anderem Namen unterschieben."""
    import sqlite3
    v = Vault(tmp_path / "vault.db")
    v.initialize(keyfile_key=b"k" * 32)
    v.set("bank.a.pin", "1111")
    db = sqlite3.connect(tmp_path / "vault.db")
    nonce, ct = db.execute("SELECT nonce, ct FROM secrets WHERE name='bank.a.pin'").fetchone()
    db.execute("INSERT INTO secrets VALUES ('bank.b.pin', ?, ?, 0)", (nonce, ct))
    db.commit()
    with pytest.raises(VaultError):
        v.get("bank.b.pin")


def test_render_relpath():
    info = DateiInfo(bank="DKB", kontoname="Giro Konto", iban="DE89370400440532013000", jahr=2026, nr=3,
                     datum=dt.date(2026, 2, 28))
    p = render_relpath("{jahr}\\{bank}/{jahr}-{monat}_{bank}_{kontoname}-{iban4}_Nr{nr}.pdf", info)
    assert p == PurePosixPath("2026/DKB/2026-02_DKB_Giro-Konto-3000_Nr03.pdf")
    with pytest.raises(AblageError):
        render_relpath("{unbekannt}.pdf", info)
    with pytest.raises(AblageError):
        render_relpath("../{bank}.pdf", info)


def test_ablage_atomic_and_collision(tmp_path: Path):
    a = Ablage(AblageConfig(pfad=str(tmp_path)))
    rel = PurePosixPath("2026/X/a.pdf")
    assert a.save(rel, b"eins") == rel
    assert a.save(rel, b"eins") == rel  # identisch → keine Kopie
    assert a.save(rel, b"zwei") == PurePosixPath("2026/X/a_2.pdf")
    assert not [p for p in tmp_path.rglob("._auszugsbote_*")]


def test_simple_pdf_is_pdf():
    data = simple_pdf([(12, "Grüße – Test")])
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")


def test_demo_run_end_to_end(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("auszugsbote.banks.demo.AUTO_FREIGABE_SEKUNDEN", 0.0)
    ablage = tmp_path / "ablage"
    ablage.mkdir()
    app = App(data=tmp_path / "data", keyfile=tmp_path / "nokey", with_scheduler=False)
    app.vault.initialize(keyfile_key=b"k" * 32)

    def setup(cfg):
        cfg.ablage.pfad = str(ablage)
        cfg.banken.append(BankConfig(id="demo", name="Demobank", typ="demo"))
    app.config.update(setup)

    app.runner.start("manuell")
    run = app.runner.wait(30)
    assert run.status == "ok", [e.text for e in run.events]
    files = sorted(p.relative_to(ablage).as_posix() for p in ablage.rglob("*.pdf"))
    assert len(files) == 6  # 2 Konten × 3 Monate
    assert all(f.startswith(("20",)) for f in files)
    # Konten wurden übernommen
    assert len(app.config.get().bank("demo").konten) == 2

    # zweiter Lauf: nichts Neues
    app.runner.start("manuell")
    run2 = app.runner.wait(30)
    assert run2.status == "ok"
    assert run2.new_files == []
    assert len(list(ablage.rglob("*.pdf"))) == 6
