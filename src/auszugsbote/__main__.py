"""Kommandozeile: auszugsbote serve | genkey | abrufen"""
from __future__ import annotations

import argparse
import logging
import os
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="auszugsbote", description="Kontoauszüge per FinTS abholen und ablegen")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="Weboberfläche und Zeitplan starten (Standard)")
    s.add_argument("--host", default=os.environ.get("AUSZUGSBOTE_HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(os.environ.get("AUSZUGSBOTE_PORT", "8080")))
    sub.add_parser("genkey", help="Inhalt für eine neue Tresor-Schlüsseldatei ausgeben")
    sub.add_parser("abrufen", help="Einmal alle aktiven Banken abrufen (braucht Schlüsseldatei)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=os.environ.get("AUSZUGSBOTE_LOGLEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # python-fints protokolliert sonst ganze Nachrichten – nur Warnungen zulassen
    logging.getLogger("fints").setLevel(logging.WARNING)

    if args.cmd == "genkey":
        from .vault import generate_keyfile_content
        sys.stdout.write(generate_keyfile_content())
        return 0

    if args.cmd == "abrufen":
        from .core import App
        app = App(with_scheduler=False)
        app.startup()
        if not app.vault.unlocked:
            print("Tresor gesperrt – für die Kommandozeile wird die Schlüsseldatei benötigt.", file=sys.stderr)
            return 2
        run = app.runner.start("manuell")
        last = 0
        while app.runner.busy:
            for e in run.events[last:]:
                print(f"[{e.level}] {e.text}")
            last = len(run.events)
            app.runner.wait(1)
        for e in run.events[last:]:
            print(f"[{e.level}] {e.text}")
        print(f"Status: {run.status}")
        return 0 if run.status == "ok" else 1

    import uvicorn
    from .web.app import create_app
    uvicorn.run(create_app(), host=getattr(args, "host", "0.0.0.0"), port=getattr(args, "port", 8080),
                proxy_headers=True, forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
                log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
