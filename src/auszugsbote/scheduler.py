"""Zeitplan (APScheduler)."""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import TYPE_CHECKING

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import ZeitplanConfig
from .runner import Busy

if TYPE_CHECKING:
    from .core import App

log = logging.getLogger(__name__)
JOB_ID = "abruf"
WOCHENTAGE = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def trigger_for(z: ZeitplanConfig, tz: str) -> CronTrigger | None:
    hh, mm = (int(x) for x in z.uhrzeit.split(":"))
    if z.modus == "manuell":
        return None
    if z.modus == "taeglich":
        return CronTrigger(hour=hh, minute=mm, timezone=tz)
    if z.modus == "woechentlich":
        return CronTrigger(day_of_week=WOCHENTAGE[z.wochentag % 7], hour=hh, minute=mm, timezone=tz)
    return CronTrigger(day=max(1, min(28, z.monatstag)), hour=hh, minute=mm, timezone=tz)


class Scheduler:
    def __init__(self, app: "App"):
        self.app = app
        self.tz = os.environ.get("TZ", "Europe/Berlin")
        self.sched = BackgroundScheduler(timezone=self.tz)

    def start(self) -> None:
        self.sched.start()
        self.apply()

    def shutdown(self) -> None:
        if self.sched.running:
            self.sched.shutdown(wait=False)

    def apply(self) -> None:
        trig = trigger_for(self.app.config.get().zeitplan, self.tz)
        if self.sched.get_job(JOB_ID):
            self.sched.remove_job(JOB_ID)
        if trig:
            self.sched.add_job(self._fire, trig, id=JOB_ID, max_instances=1, coalesce=True, misfire_grace_time=3600)

    def _fire(self) -> None:
        try:
            self.app.runner.start("zeitplan")
        except Busy:
            log.info("Zeitplan: Abruf läuft bereits")
        except Exception as exc:  # noqa: BLE001
            self.app.state.log("error", f"Zeitplan-Lauf nicht gestartet: {exc}")

    def next_run(self) -> datetime | None:
        job = self.sched.get_job(JOB_ID) if self.sched.running else None
        return job.next_run_time if job else None
