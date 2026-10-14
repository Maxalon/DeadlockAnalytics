"""SQLite storage. One file on the /data volume; WAL mode so the web UI can read while the
background jobs write."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id      INTEGER PRIMARY KEY,          -- SteamID3
    added_at        INTEGER NOT NULL,
    persona_name    TEXT,
    avatar_url      TEXT,
    last_synced_at  INTEGER,
    last_full_sync_at INTEGER,
    last_source     TEXT,                         -- where the last sync read matches from
    last_error      TEXT
);

CREATE TABLE IF NOT EXISTS matches (
    match_id        INTEGER PRIMARY KEY,
    start_time      INTEGER NOT NULL,             -- unix seconds, UTC
    duration_s      INTEGER NOT NULL,
    match_mode      TEXT NOT NULL,
    game_mode       TEXT NOT NULL,
    winning_team    INTEGER NOT NULL,             -- 0 or 1
    average_badge   INTEGER,
    fetched_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS match_players (
    match_id         INTEGER NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
    player_slot      INTEGER NOT NULL,
    account_id       INTEGER NOT NULL,
    team             INTEGER NOT NULL,
    hero_id          INTEGER NOT NULL,
    kills            INTEGER NOT NULL,
    deaths           INTEGER NOT NULL,
    assists          INTEGER NOT NULL,
    net_worth        INTEGER NOT NULL,
    player_damage    INTEGER NOT NULL,
    objective_damage INTEGER NOT NULL,            -- boss_damage in Valve's metadata
    ally_healing     INTEGER NOT NULL,            -- teammate_healing
    mvp_rank         INTEGER,                     -- Valve's: 1 = MVP, 2-3 = Key Player
    abandon_time_s   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (match_id, player_slot)
);
CREATE INDEX IF NOT EXISTS match_players_account ON match_players(account_id, match_id);

CREATE TABLE IF NOT EXISTS baseline_snapshots (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    computed_at     INTEGER NOT NULL,
    window_start    INTEGER NOT NULL,
    window_end      INTEGER NOT NULL,
    n_matches       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS hero_baselines (
    snapshot_id     INTEGER NOT NULL REFERENCES baseline_snapshots(snapshot_id) ON DELETE CASCADE,
    hero_id         INTEGER NOT NULL,
    stat            TEXT NOT NULL,
    n               INTEGER NOT NULL,
    mean            REAL NOT NULL,
    sd              REAL NOT NULL,
    PRIMARY KEY (snapshot_id, hero_id, stat)
);

CREATE TABLE IF NOT EXISTS heroes (
    hero_id         INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    icon_url        TEXT
);

CREATE TABLE IF NOT EXISTS job_runs (
    job             TEXT PRIMARY KEY,
    last_started_at INTEGER,
    last_finished_at INTEGER,
    last_error      TEXT
);
"""


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Columns added after the first release; CREATE TABLE IF NOT EXISTS won't add them to an existing db.
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}
    if "last_full_sync_at" not in columns:
        conn.execute("ALTER TABLE accounts ADD COLUMN last_full_sync_at INTEGER")
    if "last_source" not in columns:
        conn.execute("ALTER TABLE accounts ADD COLUMN last_source TEXT")
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
