"""Zentrale App-Instanz: Konfiguration, Tresor, Zustand, Runner, Zeitplan."""
from __future__ import annotations

import logging
from pathlib import Path

from .config import ConfigStore, config_path, data_dir, keyfile_path
from .runner import Runner
from .scheduler import Scheduler
from .state import State
from .vault import Vault, VaultError, read_keyfile

log = logging.getLogger(__name__)


class App:
    def __init__(self, data: Path | None = None, config_file: Path | None = None,
                 keyfile: Path | None = None, with_scheduler: bool = True):
        self.data = data or data_dir()
        self.data.mkdir(parents=True, exist_ok=True)
        self.config = ConfigStore(config_file or (self.data / "config.yaml" if data else config_path()))
        self.vault = Vault(self.data / "vault.db")
        self.state = State(self.data / "state.db")
        self.keyfile = keyfile or keyfile_path()
        self.runner = Runner(self)
        self.scheduler = Scheduler(self) if with_scheduler else None
        self.startup_message = ""

    def startup(self) -> None:
        self.state.mark_interrupted_runs()
        self.try_keyfile_unlock()
        if self.scheduler:
            self.scheduler.start()

    def try_keyfile_unlock(self) -> None:
        if not self.keyfile.exists():
            return
        try:
            key = read_keyfile(self.keyfile)
            if not self.vault.initialized:
                self.vault.initialize(keyfile_key=key)
                log.info("Tresor mit Schlüsseldatei eingerichtet")
            elif self.vault.mode == "keyfile":
                self.vault.unlock_with_keyfile(key)
                log.info("Tresor mit Schlüsseldatei entsperrt")
        except VaultError as exc:
            self.startup_message = f"Schlüsseldatei: {exc}"
            log.error(self.startup_message)

    def shutdown(self) -> None:
        if self.scheduler:
            self.scheduler.shutdown()
