"""Single background thread that runs the jobs in sequence, so they never contend for the
API rate limit or the SQLite writer lock."""

import logging
import threading
import time

from . import jobs
from .api import DeadlockApi
from .config import Settings
from .db import connect

log = logging.getLogger(__name__)
TICK_S = 30


class Scheduler:
    def __init__(self, settings: Settings, api: DeadlockApi):
        self.settings = settings
        self.api = api
        self.sync_requested = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="scheduler", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.sync_requested.set()

    def request_sync(self) -> None:
        self.sync_requested.set()

    def _due(self, conn, job: str, interval: int) -> bool:
        row = conn.execute("SELECT last_started_at FROM job_runs WHERE job = ?", (job,)).fetchone()
        return row is None or row["last_started_at"] is None or time.time() - row["last_started_at"] >= interval

    def _run(self) -> None:
        conn = connect(self.settings.database_path)
        while not self._stop.is_set():
            try:
                if self._due(conn, "heroes", 86400):
                    jobs.refresh_heroes(conn, self.api)
                    jobs._record(conn, "heroes", int(time.time()), None)
                if self._due(conn, "baselines", self.settings.baseline_interval_s):
                    jobs.compute_baselines(conn, self.api, self.settings)
                if self.sync_requested.is_set() or self._due(conn, "sync", self.settings.sync_interval_s):
                    self.sync_requested.clear()
                    jobs.sync_all_accounts(conn, self.api, self.settings)
            except Exception:
                log.exception("scheduled job failed; retrying next tick")
            self.sync_requested.wait(TICK_S)
        conn.close()
