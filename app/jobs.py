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
             (match_id, start_time, duration_s, match_mode, game_mode, winning_team, average_badge, fetched_at,
              not_scored)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(match_id) DO UPDATE SET start_time = excluded.start_time, duration_s = excluded.duration_s,
             match_mode = excluded.match_mode, game_mode = excluded.game_mode,
             winning_team = excluded.winning_team, average_badge = excluded.average_badge,
             fetched_at = excluded.fetched_at, not_scored = excluded.not_scored, detailed = 1""",
        (
            m["match_id"],
            _unix(m["start_time"]),
            m["duration_s"],
            m["match_mode"],
            m["game_mode"],
            _team(m["winning_team"]),
            m.get("average_badge"),
            int(time.time()),
            None if m.get("not_scored") is None else int(m["not_scored"]),
        ),
    )
    # Replaces history-only lines (negative slots) with the full match.
    conn.execute("DELETE FROM match_players WHERE match_id = ?", (m["match_id"],))
    for p in m["players"]:
        row = player_row(m["match_id"], p)
        conn.execute(
            f"INSERT OR REPLACE INTO match_players ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()),
        )


NOT_SCORED_OUTCOME = 5  # EPlayerMatchOutcome k_EPlayerMatchOutcome_NotScored
MATCH_MODES = {1: "Unranked", 4: "Ranked"}  # ECitadelMatchMode, as deadlock-api names them
GAME_MODES = {1: "Normal"}


def store_history_entry(conn: sqlite3.Connection, account_id: int, h: dict) -> bool:
    """Store one line of a player's match history: their own K/D/A, net worth, hero, team and
    the result, but no damage, healing or MVP rank and no other players. Enough for games
    played and win rate right away; store_match replaces it once the match file is downloaded.
    Returns whether anything new was stored."""
    known = conn.execute("SELECT detailed FROM matches WHERE match_id = ?", (h["match_id"],)).fetchone()
    if known and known[0]:
        return False
    if not known:
        conn.execute(
            """INSERT INTO matches
                 (match_id, start_time, duration_s, match_mode, game_mode, winning_team, fetched_at, detailed,
                  not_scored)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (
                h["match_id"],
                h["start_time"],
                h["match_duration_s"],
                MATCH_MODES.get(h["match_mode"], str(h["match_mode"])),
                GAME_MODES.get(h["game_mode"], str(h["game_mode"])),
                h["match_result"],  # the winning team (checked against deadlock-api)
                int(time.time()),
                int(h.get("player_match_outcome") == NOT_SCORED_OUTCOME) if h.get("player_match_outcome") else None,
            ),
        )
    # The real slot is only in the match file; -account_id keeps lines of two tracked players
    # in the same match apart until then.
    cur = conn.execute(
        """INSERT OR IGNORE INTO match_players
             (match_id, player_slot, account_id, team, hero_id, kills, deaths, assists, net_worth)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            h["match_id"],
            -account_id,
            account_id,
            h["player_team"],
            h["hero_id"],
            h["player_kills"],
            h["player_deaths"],
            h["player_assists"],
            h["net_worth"],
        ),
    )
    return cur.rowcount > 0


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
            if exc.code == "rate_limited":
                # Temporary, and the bot will get there: deadlock-api would only add a partial view.
                log.info("Steam rate limit while reading %s's history; next sync continues", account_id)
                with transaction(conn):
                    conn.execute(
                        "UPDATE accounts SET last_error = ? WHERE account_id = ?",
                        ("Steam rate limit; trying again with the next sync", account_id),
                    )
                return 0
            log.warning("Steam bot sync failed for %s (%s); using deadlock-api", account_id, exc)
            fallback_reason = f"Steam bot: {exc} (used deadlock-api instead)"
    return _sync_from_api(conn, api, account_id, settings, full, fallback_reason)


def _sync_from_fetcher(conn: sqlite3.Connection, fetcher: Fetcher, account_id: int, settings: Settings) -> int:
    """Store the player's whole match history from Valve at once, then download the match files
    still missing (all 12 players, damage, healing, MVP ranks), newest first. Steam limits how
    fast one account may fetch match files: on its rate limit the sync stops there and the next
    one continues. Returns matches newly stored."""
    started = int(time.time())
    fetcher.ensure_ready()
    entries = [
        h
        for h in fetcher.history(account_id)
        if h["game_mode"] == NORMAL_GAME_MODE and h["start_time"] >= settings.backfill_since
    ]
    with transaction(conn):
        stored = sum(store_history_entry(conn, account_id, h) for h in entries)
    undetailed = [
        r[0]
        for r in conn.execute(
            """SELECT m.match_id FROM matches m JOIN match_players mp ON mp.match_id = m.match_id
               WHERE mp.account_id = ? AND m.detailed = 0 ORDER BY m.match_id DESC""",
            (account_id,),
        )
    ]
    for match_id in undetailed:
        try:
            match = fetcher.match(match_id, account_id)
        except FetcherError as exc:
            if exc.code == "rate_limited":
                # Only this match's salts were refused (deadlock-api didn't have them and the bot's
                # Steam quota is spent); the next one may still be served, so keep going. The bot
                # pauses on its own, so this costs Steam nothing.
                log.info("no salts for %s of %s right now; the next sync retries", match_id, account_id)
                continue
            if exc.code not in SKIPPABLE_MATCH_ERRORS:
                raise
            log.info("no match file for %s of %s: %s", match_id, account_id, exc)
            continue
        with transaction(conn):
            store_match(conn, match)
    with transaction(conn):
        conn.execute(
            """UPDATE accounts SET last_synced_at = ?, last_full_sync_at = ?, last_error = NULL,
                 last_source = 'Steam bot', pending_matches = (
                   SELECT NULLIF(COUNT(*), 0) FROM matches m JOIN match_players mp ON mp.match_id = m.match_id
                   WHERE mp.account_id = ? AND m.detailed = 0)
               WHERE account_id = ?""",
            (started, started, account_id, account_id),
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
                 pending_matches = NULL, last_full_sync_at = CASE WHEN ? THEN ? ELSE last_full_sync_at END
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
