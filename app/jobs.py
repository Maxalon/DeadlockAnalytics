"""Background jobs: account match sync, hero baseline snapshots, hero/profile metadata."""

import logging
import math
import sqlite3
import time
from collections import defaultdict
from datetime import UTC, datetime

from . import scoring
from .api import DeadlockApi
from .config import Settings
from .db import transaction
from .fetcher import Fetcher, FetcherError

log = logging.getLogger(__name__)

PAGE_SIZE = 1000
BASELINE_PAGE_SIZE = 5000
# Matches can be ingested out of match-id order, so every sync re-checks this window.
SYNC_LOOKBACK_S = 2 * 86400
# Old matches can still show up long after they were played (e.g. once a player runs
# deadlock-api's ingest tool, which backfills their whole history), so once a day the sync
# re-reads the account's full history instead of only the lookback window.
FULL_RESYNC_S = 86400
TEAMS = {"Team0": 0, "Team1": 1}
NORMAL_GAME_MODE = 1  # Valve's ECitadelGameMode; the same matches deadlock-api's game_mode=normal returns
# Fetcher errors for one match that shouldn't fail the account's sync: the match is still being
# processed (next sync gets it), or Valve no longer has it.
SKIPPABLE_MATCH_ERRORS = {"in_flight", "gone", "invalid_match", "download_failed"}


def _unix(value) -> int:
    if isinstance(value, int | float):
        return int(value)
    return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp())


def _team(value) -> int:
    return TEAMS[value] if isinstance(value, str) else int(value)


def player_row(match_id: int, p: dict) -> dict:
    return {
        "match_id": match_id,
        "player_slot": p["player_slot"],
        "account_id": p["account_id"],
        "team": _team(p["team"]),
        "hero_id": p["hero_id"],
        "kills": p["kills"],
        "deaths": p["deaths"],
        "assists": p["assists"],
        "net_worth": p["net_worth"],
        "player_damage": p["max_player_damage"],
        "objective_damage": p["max_boss_damage"],
        "ally_healing": p["max_teammate_healing"],
        "mvp_rank": p.get("mvp_rank"),
        "abandon_time_s": p.get("abandon_match_time_s") or 0,
    }


def store_match(conn: sqlite3.Connection, m: dict) -> None:
    conn.execute(
        """INSERT INTO matches
             (match_id, start_time, duration_s, match_mode, game_mode, winning_team, average_badge, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(match_id) DO UPDATE SET fetched_at = excluded.fetched_at""",
        (
            m["match_id"],
            _unix(m["start_time"]),
            m["duration_s"],
            m["match_mode"],
            m["game_mode"],
            _team(m["winning_team"]),
            m.get("average_badge"),
            int(time.time()),
        ),
    )
    for p in m["players"]:
        row = player_row(m["match_id"], p)
        conn.execute(
            f"INSERT OR REPLACE INTO match_players ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()),
        )


def _record(conn, job: str, started: int, error: str | None) -> None:
    with transaction(conn):
        conn.execute(
            """INSERT INTO job_runs (job, last_started_at, last_finished_at, last_error) VALUES (?, ?, ?, ?)
               ON CONFLICT(job) DO UPDATE SET last_started_at = excluded.last_started_at,
                 last_finished_at = excluded.last_finished_at, last_error = excluded.last_error""",
            (job, started, int(time.time()), error),
        )


def sync_account(
    conn: sqlite3.Connection,
    api: DeadlockApi,
    account_id: int,
    settings: Settings,
    full: bool = False,
    fetcher: Fetcher | None = None,
) -> int:
    """Fetch the account's new matches. Returns matches stored.

    Through the Steam bot when one is configured: it sees the player's complete history.
    Otherwise, or when the bot can't read this player, from deadlock-api, which only has the
    matches it happened to collect; the reason is then kept in the account's last_error."""
    fallback_reason = None
    if fetcher is not None:
        try:
            return _sync_from_fetcher(conn, fetcher, account_id, settings)
        except FetcherError as exc:
            log.warning("Steam bot sync failed for %s (%s); using deadlock-api", account_id, exc)
            fallback_reason = f"Steam bot: {exc} (used deadlock-api instead)"
    return _sync_from_api(conn, api, account_id, settings, full, fallback_reason)


def _sync_from_fetcher(conn: sqlite3.Connection, fetcher: Fetcher, account_id: int, settings: Settings) -> int:
    """Read the full match history from Valve and download the matches not stored yet."""
    started = int(time.time())
    fetcher.ensure_ready()
    have = {r[0] for r in conn.execute("SELECT match_id FROM match_players WHERE account_id = ?", (account_id,))}
    wanted = sorted(
        m["match_id"]
        for m in fetcher.history(account_id)
        if m["game_mode"] == NORMAL_GAME_MODE and m["start_time"] >= settings.backfill_since
    )
    stored = 0
    for match_id in wanted:
        if match_id in have:
            continue
        try:
            match = fetcher.match(match_id)
        except FetcherError as exc:
            if exc.code not in SKIPPABLE_MATCH_ERRORS:
                raise
            log.info("skipping match %s of %s: %s", match_id, account_id, exc)
            continue
        with transaction(conn):
            store_match(conn, match)
        stored += 1
    with transaction(conn):
        conn.execute(
            """UPDATE accounts SET last_synced_at = ?, last_full_sync_at = ?, last_error = NULL,
                 last_source = 'Steam bot' WHERE account_id = ?""",
            (started, started, account_id),
        )
    return stored


def _sync_from_api(
    conn: sqlite3.Connection,
    api: DeadlockApi,
    account_id: int,
    settings: Settings,
    full: bool,
    note: str | None = None,
) -> int:
    """The account's matches since its last sync (with lookback), or its whole history once a day."""
    row = conn.execute(
        "SELECT last_synced_at, last_full_sync_at FROM accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    started = int(time.time())
    full = (
        full
        or not (row and row["last_synced_at"] and row["last_full_sync_at"])
        or (started - row["last_full_sync_at"] >= FULL_RESYNC_S)
    )
    since = settings.backfill_since if full else row["last_synced_at"] - SYNC_LOOKBACK_S
    stored, min_match_id = 0, None
    while True:
        page = api.matches(
            account_ids=[account_id],
            min_unix_timestamp=since,
            min_match_id=min_match_id,
            order_by="match_id",
            order_direction="asc",
            limit=PAGE_SIZE,
        )
        with transaction(conn):
            for m in page:
                store_match(conn, m)
        stored += len(page)
        if len(page) < PAGE_SIZE:
            break
        min_match_id = page[-1]["match_id"] + 1
    with transaction(conn):
        conn.execute(
            """UPDATE accounts SET last_synced_at = ?, last_error = ?, last_source = 'deadlock-api',
                 last_full_sync_at = CASE WHEN ? THEN ? ELSE last_full_sync_at END
               WHERE account_id = ?""",
            (started, note, full, started, account_id),
        )
    return stored


def sync_account_safely(
    conn: sqlite3.Connection,
    api: DeadlockApi,
    account_id: int,
    settings: Settings,
    full: bool = False,
    fetcher: Fetcher | None = None,
) -> str | None:
    """sync_account, but a failure is logged and stored on the account instead of raised."""
    try:
        n = sync_account(conn, api, account_id, settings, full=full, fetcher=fetcher)
        log.info("synced account %s%s: %s matches", account_id, " (full)" if full else "", n)
        return None
    except Exception as exc:
        log.exception("sync failed for %s", account_id)
        with transaction(conn):
            conn.execute("UPDATE accounts SET last_error = ? WHERE account_id = ?", (str(exc)[:500], account_id))
        return f"{account_id}: {exc}"


def sync_all_accounts(
    conn: sqlite3.Connection, api: DeadlockApi, settings: Settings, fetcher: Fetcher | None = None
) -> None:
    started = int(time.time())
    errors = []
    for (account_id,) in conn.execute("SELECT account_id FROM accounts ORDER BY account_id").fetchall():
        # keep going with the others
        if error := sync_account_safely(conn, api, account_id, settings, fetcher=fetcher):
            errors.append(error)
    try:
        refresh_profiles(conn, api)
    except Exception as exc:
        log.warning("profile refresh failed: %s", exc)
    _record(conn, "sync", started, "; ".join(errors) or None)


class Welford:
    """Streaming mean / sample SD."""

    __slots__ = ("n", "mean", "m2")

    def __init__(self):
        self.n, self.mean, self.m2 = 0, 0.0, 0.0

    def add(self, x: float) -> None:
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (x - self.mean)

    @property
    def sd(self) -> float:
        return math.sqrt(self.m2 / (self.n - 1)) if self.n > 1 else 0.0


def accumulate(acc: dict, matches: list[dict]) -> None:
    for m in matches:
        for p in m["players"]:
            row = player_row(m["match_id"], p)
            for stat in scoring.STATS:
                acc[(row["hero_id"], stat)].add(scoring.stat_value(row, stat))


def compute_baselines(conn: sqlite3.Connection, api: DeadlockApi, settings: Settings) -> int:
    """Sample matches from the last BASELINE_DAYS and store per-hero mean/SD for every scored stat.

    The sample is spread evenly over one slice per day (the newest matches of each day),
    because ~30k matches is under a day of play: taking only the newest would cover a few hours.
    """
    started = int(time.time())
    window_start = started - settings.baseline_days * 86400
    slices = max(settings.baseline_days, 1)
    per_slice = max(settings.baseline_max_matches // slices, 1)
    acc: dict = defaultdict(Welford)
    n_matches, oldest = 0, started
    try:
        for i in range(slices):
            slice_end = started - i * 86400
            slice_start = max(slice_end - 86400, window_start)
            taken, max_match_id = 0, None
            while taken < per_slice:
                page = api.matches(
                    min_unix_timestamp=slice_start,
                    max_unix_timestamp=slice_end,
                    max_match_id=max_match_id,
                    order_by="match_id",
                    order_direction="desc",
                    limit=min(BASELINE_PAGE_SIZE, per_slice - taken),
                )
                if not page:
                    break
                accumulate(acc, page)
                taken += len(page)
                oldest = min(oldest, *(_unix(m["start_time"]) for m in page))
                max_match_id = page[-1]["match_id"] - 1
            n_matches += taken
        if n_matches == 0:
            raise RuntimeError("no matches returned for the baseline window")
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO baseline_snapshots (computed_at, window_start, window_end, n_matches) VALUES (?, ?, ?, ?)",
                (started, oldest, started, n_matches),
            )
            snapshot_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO hero_baselines (snapshot_id, hero_id, stat, n, mean, sd) VALUES (?, ?, ?, ?, ?, ?)",
                [(snapshot_id, hero, stat, w.n, w.mean, w.sd) for (hero, stat), w in acc.items() if w.n > 1],
            )
        log.info("baseline snapshot %s from %s matches", snapshot_id, n_matches)
        _record(conn, "baselines", started, None)
        return snapshot_id
    except Exception as exc:
        _record(conn, "baselines", started, str(exc)[:500])
        raise


def refresh_heroes(conn: sqlite3.Connection, api: DeadlockApi) -> None:
    heroes = api.heroes()
    with transaction(conn):
        for h in heroes:
            images = h.get("images") or {}
            icon = images.get("icon_image_small_webp") or images.get("icon_image_small")
            conn.execute(
                "INSERT OR REPLACE INTO heroes (hero_id, name, icon_url) VALUES (?, ?, ?)", (h["id"], h["name"], icon)
            )


def refresh_profiles(conn: sqlite3.Connection, api: DeadlockApi) -> None:
    ids = [r[0] for r in conn.execute("SELECT account_id FROM accounts")]
    profiles = api.steam_profiles(ids)
    with transaction(conn):
        for p in profiles:
            conn.execute(
                "UPDATE accounts SET persona_name = ?, avatar_url = ? WHERE account_id = ?",
                (p.get("personaname"), p.get("avatarmedium") or p.get("avatar"), p["account_id"]),
            )
