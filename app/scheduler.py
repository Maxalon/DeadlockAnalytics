"""Single background thread that runs the jobs in sequence, so they never contend for the
API rate limit or the SQLite writer lock."""

import logging
import threading
import time

from . import jobs
from .api import DeadlockApi
from .config import Settings
from .db import connect
from .fetcher import Fetcher, FetcherError

log = logging.getLogger(__name__)
TICK_S = 30


class Scheduler:
    def __init__(self, settings: Settings, api: DeadlockApi, fetcher: Fetcher | None = None):
        self.settings = settings
        self.api = api
        self.fetcher = fetcher
        self.sync_requested = threading.Event()  # "sync every account now"
        self._wake = threading.Event()  # cut the idle wait short
        self._stop = threading.Event()
        # Accounts whose full history should be re-pulled now (player page opened, or its
        # re-pull button). Handled before the periodic jobs, one at a time on this thread.
        self._lock = threading.Lock()
        self._pending: set[int] = set()
        self._running: int | None = None
        # Matches someone opened that have no details yet (match id -> queued), and the latest
        # reason one couldn't be fetched (match id -> (message, unix time)).
        self._match_queue: dict[int, float] = {}
        self._match_running: int | None = None
        self.match_errors: dict[int, tuple[str, float]] = {}
        self._thread = threading.Thread(target=self._run, name="scheduler", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def request_sync(self) -> None:
        self.sync_requested.set()
        self._wake.set()

    def request_account_sync(self, account_id: int) -> None:
        with self._lock:
            self._pending.add(account_id)
        self._wake.set()

    def request_match_details(self, match_id: int) -> None:
        with self._lock:
            self._match_queue.setdefault(match_id, time.time())
        self._wake.set()

    def is_fetching_match(self, match_id: int) -> bool:
        with self._lock:
            return match_id in self._match_queue or self._match_running == match_id

    def _fetch_queued_matches(self, conn) -> None:
        while self.fetcher is not None:
            with self._lock:
                if not self._match_queue:
                    return
                match_id = self._match_running = next(iter(self._match_queue))
                del self._match_queue[match_id]
            try:
                jobs.fetch_match_details(conn, self.fetcher, match_id)
                self.match_errors.pop(match_id, None)
            except FetcherError as exc:
                log.info("details for match %s not available now: %s", match_id, exc)
                self.match_errors[match_id] = (str(exc), time.time())
            finally:
                with self._lock:
                    self._match_running = None

    def is_syncing(self, account_id: int) -> bool:
        with self._lock:
            return account_id in self._pending or self._running == account_id

    def _sync_pending_accounts(self, conn) -> None:
        pulled = False
        while True:
            with self._lock:
                if not self._pending:
                    break
                account_id = self._running = self._pending.pop()
            try:
                jobs.sync_account_safely(conn, self.api, account_id, self.settings, full=True, fetcher=self.fetcher)
            finally:
                with self._lock:
                    self._running = None
            pulled = True
        if pulled:
            try:  # so a newly added account shows its name and avatar right away
                jobs.refresh_profiles(conn, self.api)
            except Exception as exc:
                log.warning("profile refresh failed: %s", exc)

    def _due(self, conn, job: str, interval: int) -> bool:
        row = conn.execute("SELECT last_started_at FROM job_runs WHERE job = ?", (job,)).fetchone()
        return row is None or row["last_started_at"] is None or time.time() - row["last_started_at"] >= interval

    def _run(self) -> None:
        conn = connect(self.settings.database_path)
        while not self._stop.is_set():
            try:
                self._fetch_queued_matches(conn)
                self._sync_pending_accounts(conn)
                if self._due(conn, "heroes", 86400):
                    jobs.refresh_heroes(conn, self.api)
                    jobs._record(conn, "heroes", int(time.time()), None)
                if self._due(conn, "baselines", self.settings.baseline_interval_s):
                    jobs.compute_baselines(conn, self.api, self.settings)
                with self._lock:
                    only_accounts = bool(self._pending or self._match_queue)
                if only_accounts:
                    continue  # an account re-pull arrived meanwhile; serve it first
                if self.sync_requested.is_set() or self._due(conn, "sync", self.settings.sync_interval_s):
                    self.sync_requested.clear()
                    if self.fetcher is None:
                        jobs.sync_all_accounts(conn, self.api, self.settings)
                    else:
                        # The bot's Steam budget is only spent on request (re-pull, opening a
                        # match); on schedule only what deadlock-api can give for free.
                        started = int(time.time())
                        jobs.enrich_from_deadlock_api(conn, self.fetcher)
                        jobs._record(conn, "sync", started, None)
            except Exception:
                log.exception("scheduled job failed; retrying next tick")
            # Clearing right after waking is safe: anything requested in between is still in
            # _pending / sync_requested and is picked up at the top of the loop.
            self._wake.wait(TICK_S)
            self._wake.clear()
        conn.close()
