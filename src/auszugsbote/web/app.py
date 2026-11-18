"""Weboberfläche (FastAPI + Jinja2 + HTMX)."""
from __future__ import annotations

import base64
import datetime as dt
import hmac
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import __version__
from ..config import BankConfig, KontoConfig
from ..core import App
from ..runner import Busy
from ..storage import AblageError, Ablage, DateiInfo, PLATZHALTER, render_relpath
from ..vault import VaultError

HERE = Path(__file__).parent
PH = PasswordHasher()

# Vorbelegungen für häufige Banken. BLZ ggf. an das eigene Konto anpassen.
BANK_PRESETS = {
    "commerzbank": {"name": "Commerzbank", "blz": "", "url": "https://fints.commerzbank.de/fints",
                    "hinweis": "BLZ deines Kontos eintragen. Login = Teilnehmernummer. Kontoauszug auf "
                               "elektronisches Postfach umstellen, dann ist der FinTS-Abruf aktiv."},
    "dkb": {"name": "DKB", "blz": "12030000", "url": "https://fints.dkb.de/fints",
            "hinweis": "Login = Anmeldename. PDF über HKEKP; bei „nicht freigeschaltet“ DKB-Support fragen."},
    "ing": {"name": "ING", "blz": "50010517", "url": "https://fints.ing.de/fints/",
            "hinweis": "Login = Zugangsnummer. PDF-Auszüge per FinTS eher nicht verfügbar – dann camt-Umsätze."},
    "demo": {"name": "Demobank", "blz": "", "url": "",
             "hinweis": "Erfundene Konten und Auszüge zum Ausprobieren, ohne Produkt-ID."},
}

OFFEN = {"/login", "/setup", "/healthz"}
OHNE_TRESOR = OFFEN | {"/entsperren", "/logout"}


def create_app(app_core: App | None = None) -> FastAPI:
    core = app_core or App()

    @asynccontextmanager
    async def lifespan(_):
        core.startup()
        yield
        core.shutdown()

    api = FastAPI(title="Auszugsbote", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(_csrf_guard)])
    api.state.core = core
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    _filters(templates)

    # -- Middleware: Login, Einrichtung, Tresor -------------------------------------------
    @api.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/static/") or path in OFFEN:
            return await call_next(request)
        if not core.state.get_meta("web_password") or not core.vault.initialized:
            return RedirectResponse("/setup", 303)
        if not request.session.get("user"):
            return RedirectResponse(f"/login?next={quote(path)}", 303)
        if time.time() - request.session.get("seen", 0) > 12 * 3600:
            request.session.clear()
            return RedirectResponse("/login", 303)
        request.session["seen"] = time.time()
        if path not in OHNE_TRESOR and not core.vault.unlocked:
            return RedirectResponse("/entsperren", 303)
        resp = await call_next(request)
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    secret = os.environ.get("AUSZUGSBOTE_SESSION_SECRET") or secrets.token_urlsafe(32)
    api.add_middleware(SessionMiddleware, secret_key=secret, same_site="strict",
                       https_only=os.environ.get("AUSZUGSBOTE_HTTPS", "0") == "1", max_age=12 * 3600,
                       session_cookie="auszugsbote")
    api.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(24)
        cfg = core.config.get()
        base = {
            "request": request, "csrf": request.session["csrf"], "cfg": cfg, "core": core,
            "version": __version__, "run": core.runner.current, "flash": request.session.pop("flash", None),
            "ablage_ok": Ablage(cfg.ablage).check()[0],
        }
        base.update(ctx)
        return templates.TemplateResponse(request, name, base)

    def flash(request: Request, text: str, level: str = "ok") -> None:
        request.session["flash"] = {"text": text, "level": level}

    # -- Einrichtung & Login -----------------------------------------------------------
    @api.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        return "ok"

    @api.get("/setup", response_class=HTMLResponse)
    def setup_form(request: Request):
        if core.state.get_meta("web_password") and core.vault.initialized:
            return RedirectResponse("/", 303)
        return page(request, "setup.html", keyfile=core.keyfile, keyfile_exists=core.keyfile.exists(),
                    need_vault=not core.vault.initialized, need_web=not core.state.get_meta("web_password"))

    @api.post("/setup")
    async def setup_post(request: Request, web_password: str = Form(""), web_password2: str = Form(""),
                         master: str = Form(""), master2: str = Form("")):
        if core.state.get_meta("web_password") and core.vault.initialized:
            return RedirectResponse("/", 303)
        errors = []
        if not core.state.get_meta("web_password"):
            if len(web_password) < 10:
                errors.append("Login-Passwort: mindestens 10 Zeichen")
            elif web_password != web_password2:
                errors.append("Login-Passwörter stimmen nicht überein")
        if not core.vault.initialized and not core.keyfile.exists():
            if len(master) < 12:
                errors.append("Master-Passwort: mindestens 12 Zeichen")
            elif master != master2:
                errors.append("Master-Passwörter stimmen nicht überein")
        if errors:
            return page(request, "setup.html", errors=errors, keyfile=core.keyfile,
                        keyfile_exists=core.keyfile.exists(), need_vault=not core.vault.initialized,
                        need_web=not core.state.get_meta("web_password"))
        if not core.vault.initialized:
            if core.keyfile.exists():
                core.try_keyfile_unlock()
            else:
                core.vault.initialize(password=master)
        if not core.state.get_meta("web_password"):
            core.state.set_meta("web_password", PH.hash(web_password))
        request.session.clear()
        request.session["user"] = "admin"
        request.session["seen"] = time.time()
        flash(request, "Eingerichtet. Lege jetzt einen Bankzugang an – zum Ausprobieren die Demobank.")
        return RedirectResponse("/banken/neu", 303)

    @api.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/"):
        if not core.state.get_meta("web_password"):
            return RedirectResponse("/setup", 303)
        return page(request, "login.html", next=next if next.startswith("/") else "/")

    @api.post("/login")
    async def login_post(request: Request, password: str = Form(...), next: str = Form("/")):
        stored = core.state.get_meta("web_password")
        try:
            ok = bool(stored) and PH.verify(stored, password)
        except VerifyMismatchError:
            ok = False
        if not ok:
            time.sleep(1.5)
            return page(request, "login.html", next=next, error="Passwort falsch")
        request.session.clear()
        request.session["user"] = "admin"
        request.session["seen"] = time.time()
        return RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/", 303)

    @api.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", 303)

    @api.get("/entsperren", response_class=HTMLResponse)
    def unlock_form(request: Request):
        if core.vault.unlocked:
            return RedirectResponse("/", 303)
        return page(request, "unlock.html", mode=core.vault.mode, msg=core.startup_message)

    @api.post("/entsperren")
    def unlock_post(request: Request, master: str = Form("")):
        try:
            if core.vault.mode == "keyfile":
                core.try_keyfile_unlock()
            else:
                core.vault.unlock_with_password(master)
        except VaultError as exc:
            time.sleep(1.5)
            return page(request, "unlock.html", mode=core.vault.mode, error=str(exc))
        if not core.vault.unlocked:
            return page(request, "unlock.html", mode=core.vault.mode, error="Schlüsseldatei nicht gefunden")
        return RedirectResponse("/", 303)

    # -- Übersicht ---------------------------------------------------------------------
    @api.get("/", response_class=HTMLResponse)
    def index(request: Request):
        cfg = core.config.get()
        ablage = Ablage(cfg.ablage)
        ok, ablage_msg = ablage.check()
        konten = []
        for b in cfg.banken:
            test = core.runner.tests.get(b.id)
            for k in b.konten:
                konten.append({"bank": b, "konto": k, "last": core.state.last_statement(b.id, k.iban),
                               "test": test})
        now = time.time()
        return page(
            request, "index.html",
            neu_30=core.state.statement_count(now - 30 * 86400),
            letzte=core.state.statements(limit=8),
            konten=konten,
            aktiv=sum(1 for x in konten if x["konto"].aktiv and x["bank"].aktiv),
            ablage_ok=ok, ablage_msg=ablage_msg,
            baum=_tree(ablage.tree()),
            gesamt=ablage.count_files(),
            next_run=core.scheduler.next_run() if core.scheduler else None,
            last_run=core.state.last_run(),
            produkt_id=core.vault.has("fints.product_id"),
        )

    @api.get("/auszuege", response_class=HTMLResponse)
    def auszuege(request: Request, bank: str = "", jahr: int | None = None):
        return page(request, "auszuege.html", rows=core.state.statements(limit=500, bank_id=bank or None, year=jahr),
                    years=core.state.years(), f_bank=bank, f_jahr=jahr)

    # -- Läufe ---------------------------------------------------------------------------
    @api.post("/laeufe", response_class=HTMLResponse)
    def start_run(request: Request, bank: str = Form("")):
        try:
            core.runner.start("manuell", [bank] if bank else None)
        except (Busy, RuntimeError) as exc:
            if request.headers.get("HX-Request"):
                return HTMLResponse(f'<div class="toast err">{exc}</div>')
            flash(request, str(exc), "err")
            return RedirectResponse("/", 303)
        return run_partial(request)

    @api.get("/laeufe/aktuell", response_class=HTMLResponse)
    def run_partial(request: Request):
        run = core.runner.current
        if run is None:
            return HTMLResponse("")
        matrix = None
        if run.pending and run.pending.matrix:
            mime, data = run.pending.matrix
            matrix = f"data:{mime};base64,{base64.b64encode(data).decode()}"
        return page(request, "_run.html", run=run, matrix=matrix)

    @api.post("/laeufe/aktuell/tan", response_class=HTMLResponse)
    def run_tan(request: Request, tan: str = Form(...)):
        if core.runner.current:
            core.runner.current.submit_tan(tan)
        return run_partial(request)

    @api.post("/laeufe/aktuell/demo-freigabe", response_class=HTMLResponse)
    def run_demo(request: Request):
        if core.runner.current:
            core.runner.current.approve_demo()
        return run_partial(request)

    @api.post("/laeufe/aktuell/abbrechen", response_class=HTMLResponse)
    def run_cancel(request: Request):
        if core.runner.current:
            core.runner.current.cancel()
        return run_partial(request)

    # -- Bankzugänge ---------------------------------------------------------------------
    @api.get("/banken", response_class=HTMLResponse)
    def banken(request: Request):
        return page(request, "banken.html", tests=core.runner.tests)

    @api.get("/banken/neu", response_class=HTMLResponse)
    def bank_neu(request: Request, preset: str = ""):
        p = BANK_PRESETS.get(preset, {})
        bank = BankConfig(id="neu", name=p.get("name", ""), typ="demo" if preset == "demo" else "fints",
                          blz=p.get("blz", ""), url=p.get("url", ""))
        return page(request, "bank_form.html", bank=bank, neu=True, presets=BANK_PRESETS, preset=preset,
                    hinweis=p.get("hinweis", ""), hat_login=False, hat_pin=False, test=None)

    @api.get("/banken/{bank_id}", response_class=HTMLResponse)
    def bank_edit(request: Request, bank_id: str):
        bank = core.config.get().bank(bank_id)
        if not bank:
            raise HTTPException(404)
        v = core.vault
        return page(request, "bank_form.html", bank=bank, neu=False, presets=BANK_PRESETS, preset="",
                    hinweis="", hat_login=v.has(f"bank.{bank_id}.login"), hat_pin=v.has(f"bank.{bank_id}.pin"),
                    test=core.runner.tests.get(bank_id))

    @api.post("/banken/{bank_id}")
    async def bank_save(request: Request, bank_id: str):
        form = await request.form()
        neu = bank_id == "neu"
        name = str(form.get("name", "")).strip()
        if not name:
            flash(request, "Bitte einen Namen angeben", "err")
            return RedirectResponse(f"/banken/{bank_id}" if not neu else "/banken/neu", 303)
        cfg = core.config.get()
        if neu:
            base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "bank"
            new_id, i = base, 2
            while cfg.bank(new_id):
                new_id, i = f"{base}-{i}", i + 1
            bank_id = new_id
        old = cfg.bank(bank_id)
        konten = []
        if old:
            for k in old.konten:
                konten.append(KontoConfig(
                    iban=k.iban,
                    name=str(form.get(f"konto_name_{k.iban}", k.name)).strip() or k.name,
                    aktiv=form.get(f"konto_aktiv_{k.iban}") == "on",
                ))
        try:
            bank = BankConfig(
                id=bank_id, name=name,
                typ="demo" if form.get("typ") == "demo" else "fints",
                blz=re.sub(r"\D", "", str(form.get("blz", ""))),
                url=str(form.get("url", "")).strip(),
                tan_verfahren=str(form.get("tan_verfahren", "")).strip() or None,
                tan_medium=str(form.get("tan_medium", "")).strip() or None,
                pdf_verfahren=str(form.get("pdf_verfahren", "auto")),
                camt_ablegen=form.get("camt_ablegen") == "on",
                aktiv=form.get("aktiv") == "on" or neu,
                konten=konten,
            )
        except ValueError as exc:
            flash(request, f"Eingabe ungültig: {exc}", "err")
            return RedirectResponse(f"/banken/{'neu' if neu else bank_id}", 303)
        if bank.typ == "fints" and bank.url and not bank.url.startswith("https://"):
            flash(request, "Die FinTS-Adresse muss mit https:// beginnen", "err")
            return RedirectResponse(f"/banken/{'neu' if neu else bank_id}", 303)

        def upd(c):
            c.banken = [b for b in c.banken if b.id != bank_id] + [bank] if not old else \
                [bank if b.id == bank_id else b for b in c.banken]
        core.config.update(upd)
        v = core.vault
        for feld in ("login", "pin", "customer_id"):
            wert = str(form.get(feld, ""))
            if wert:
                v.set(f"bank.{bank_id}.{feld}", wert)
        if form.get("state_reset") == "on":
            v.delete(f"bank.{bank_id}.state")
        if form.get("aktion") == "test":
            try:
                core.runner.start("test", [bank_id], mode="test")
            except (Busy, RuntimeError) as exc:
                flash(request, str(exc), "err")
            return RedirectResponse(f"/banken/{bank_id}?lauf=1", 303)
        flash(request, f"{bank.name} gespeichert")
        return RedirectResponse(f"/banken/{bank_id}", 303)

    @api.post("/banken/{bank_id}/loeschen")
    def bank_delete(request: Request, bank_id: str):
        core.config.update(lambda c: setattr(c, "banken", [b for b in c.banken if b.id != bank_id]))
        core.vault.delete_prefix(f"bank.{bank_id}.")
        core.runner.tests.pop(bank_id, None)
        flash(request, "Bankzugang und gespeicherte Zugangsdaten gelöscht")
        return RedirectResponse("/banken", 303)

    # -- Ablage & Zeitplan ----------------------------------------------------------------
    @api.get("/ablage", response_class=HTMLResponse)
    def ablage(request: Request):
        cfg = core.config.get()
        return page(request, "ablage.html", platzhalter=PLATZHALTER, vorschau=_vorschau(cfg.ablage.muster),
                    check=Ablage(cfg.ablage).check(),
                    next_run=core.scheduler.next_run() if core.scheduler else None)

    @api.post("/ablage/vorschau", response_class=HTMLResponse)
    async def ablage_vorschau(request: Request):
        form = await request.form()
        return HTMLResponse(_vorschau_html(str(form.get("muster", ""))))

    @api.post("/ablage")
    async def ablage_save(request: Request):
        form = await request.form()
        try:
            muster = str(form.get("muster", "")).strip()
            _vorschau(muster, strict=True)

            def upd(c):
                c.ablage.pfad = str(form.get("pfad", c.ablage.pfad)).strip() or c.ablage.pfad
                c.ablage.anzeige_pfad = str(form.get("anzeige_pfad", "")).strip()
                c.ablage.muster = muster
                c.ablage.paperless_consume = str(form.get("paperless_consume", "")).strip()
                c.zeitplan.modus = str(form.get("modus", "taeglich"))
                c.zeitplan.uhrzeit = str(form.get("uhrzeit", "06:30"))
                c.zeitplan.wochentag = int(form.get("wochentag", 0) or 0)
                c.zeitplan.monatstag = int(form.get("monatstag", 1) or 1)
            core.config.update(upd)
        except (AblageError, ValueError) as exc:
            flash(request, f"Nicht gespeichert: {exc}", "err")
            return RedirectResponse("/ablage", 303)
        if core.scheduler:
            core.scheduler.apply()
        if form.get("aktion") == "testdatei":
            cfg = core.config.get()
            ok, msg = Ablage(cfg.ablage).check()
            flash(request, f"Testschreiben: {msg}", "ok" if ok else "err")
        else:
            flash(request, "Ablage und Zeitplan gespeichert")
        return RedirectResponse("/ablage", 303)

    # -- Protokoll, Tresor, Einstellungen --------------------------------------------------
    @api.get("/protokoll", response_class=HTMLResponse)
    def protokoll(request: Request):
        return page(request, "protokoll.html", runs=core.state.runs(30), logs=core.state.logs(300))

    @api.get("/tresor", response_class=HTMLResponse)
    def tresor(request: Request):
        return page(request, "tresor.html", entries=core.vault.entries(), mode=core.vault.mode,
                    keyfile=core.keyfile)

    @api.post("/tresor/sperren")
    def tresor_sperren(request: Request):
        if core.runner.busy:
            flash(request, "Während eines Abrufs nicht möglich", "err")
            return RedirectResponse("/tresor", 303)
        core.vault.lock()
        return RedirectResponse("/entsperren", 303)

    @api.get("/einstellungen", response_class=HTMLResponse)
    def einstellungen(request: Request):
        return page(request, "einstellungen.html", produkt_id=core.vault.has("fints.product_id"),
                    ha_token=core.vault.has("notify.ha_token"))

    @api.post("/einstellungen")
    async def einstellungen_save(request: Request):
        form = await request.form()
        pid = str(form.get("product_id", "")).strip()
        if pid:
            core.vault.set("fints.product_id", pid)
        tok = str(form.get("ha_token", "")).strip()
        if tok:
            core.vault.set("notify.ha_token", tok)

        def upd(c):
            c.benachrichtigung.ha_url = str(form.get("ha_url", "")).strip()
            c.benachrichtigung.ha_service = str(form.get("ha_service", "")).strip()
            c.tan_timeout_sekunden = max(60, min(900, int(form.get("tan_timeout", 300) or 300)))
        core.config.update(upd)
        neu, neu2 = str(form.get("web_password", "")), str(form.get("web_password2", ""))
        if neu:
            if len(neu) < 10 or neu != neu2:
                flash(request, "Neues Login-Passwort: mind. 10 Zeichen und zweimal gleich", "err")
                return RedirectResponse("/einstellungen", 303)
            core.state.set_meta("web_password", PH.hash(neu))
        flash(request, "Einstellungen gespeichert")
        return RedirectResponse("/einstellungen", 303)

    return api


# -- Hilfsfunktionen -------------------------------------------------------------------------
async def _csrf_guard(request: Request) -> None:
    """Globale Abhängigkeit: jede POST-Anfrage braucht das CSRF-Token der Sitzung."""
    if request.method == "POST":
        await _check_csrf(request)


async def _check_csrf(request: Request) -> None:
    token = request.session.get("csrf")
    sent = request.headers.get("X-CSRF-Token")
    if not sent:
        form = await request.form()
        sent = form.get("csrf")
    if not token or not sent or not hmac.compare_digest(str(token), str(sent)):
        raise HTTPException(403, "CSRF-Prüfung fehlgeschlagen – Seite neu laden")


def _vorschau(muster: str, strict: bool = False) -> list[str]:
    beispiele = [
        DateiInfo("Commerzbank", "Giro", "DE89370400440532013000", 2026, 10, dt.date(2026, 10, 31)),
        DateiInfo("DKB", "Giro", "DE02120300000000202051", 2026, 11, dt.date(2026, 10, 31)),
    ]
    try:
        return [str(render_relpath(muster, b)) for b in beispiele]
    except AblageError:
        if strict:
            raise
        return []


def _vorschau_html(muster: str) -> str:
    from html import escape
    try:
        rows = _vorschau(muster, strict=True)
        return "".join(f"<div>{escape(r)}</div>" for r in rows)
    except AblageError as exc:
        return f'<div class="err-text">{escape(str(exc))}</div>'


def _tree(paths: list[str]) -> list[tuple[str, str, bool]]:
    """Baut eine Baumdarstellung: Liste von (Präfix, Name, ist_ordner)."""
    root: dict = {}
    for p in paths:
        node = root
        parts = p.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part + "/", {})
        node[parts[-1]] = None
    out: list[tuple[str, str, bool]] = []

    def walk(node: dict, prefix: str) -> None:
        items = sorted(node.items(), key=lambda kv: (kv[1] is None, kv[0]))
        for i, (name, child) in enumerate(items):
            last = i == len(items) - 1
            out.append((prefix + ("└─ " if last else "├─ "), name, child is not None))
            if child is not None:
                walk(child, prefix + ("   " if last else "│  "))
    walk(root, "")
    return out


def _filters(t: Jinja2Templates) -> None:
    def ts(v, fmt="%d.%m. %H:%M"):
        if v is None:
            return "—"
        if isinstance(v, (int, float)):
            v = dt.datetime.fromtimestamp(v)
        return v.strftime(fmt)

    def size(n):
        n = n or 0
        return f"{n / 1024:,.0f} KB".replace(",", ".") if n < 1024 * 1024 else f"{n / 1048576:,.1f} MB".replace(".", ",")

    def iban_mask(iban: str):
        i = (iban or "").replace(" ", "")
        if len(i) < 12:
            return i
        return f"{i[:4]} {i[4:8]} •••• •••• {i[-4:]}"

    def wochentag(v):
        return ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"][v.weekday()]

    t.env.filters.update(ts=ts, size=size, iban_mask=iban_mask, wochentag=wochentag)
