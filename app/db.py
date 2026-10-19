"""SQLite storage. One file on the /data volume; WAL mode so the web UI can read while the
background jobs write."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

MATCH_PLAYERS = """
CREATE TABLE IF NOT EXISTS match_players (
    match_id         INTEGER NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
    player_slot      INTEGER NOT NULL,            -- -account_id until the match file is downloaded
    account_id       INTEGER NOT NULL,
    team             INTEGER NOT NULL,
    hero_id          INTEGER NOT NULL,
    kills            INTEGER NOT NULL,
    deaths           INTEGER NOT NULL,
    assists          INTEGER NOT NULL,
    net_worth        INTEGER NOT NULL,
    -- NULL until the match file is downloaded (not in the match history)
    player_damage    INTEGER,
    objective_damage INTEGER,                     -- boss_damage in Valve's metadata
    ally_healing     INTEGER,                     -- teammate_healing
    mvp_rank         INTEGER,                     -- Valve's: 1 = MVP, 2-3 = Key Player
    abandon_time_s   INTEGER NOT NULL DEFAULT 0,
    badge            INTEGER,                     -- rank badge, tier*10 + subrank (see badge_source)
    -- 'match': the badge the player entered this ranked match with (Valve's match file).
    -- 'current': their rank looked up within days of the match (unranked matches have none).
    badge_source     TEXT,
    -- Before this match, in the matches deadlock-api has of the player (not all of them):
    hero_matches     INTEGER,                     -- on this hero
    hero_wins        INTEGER,
    total_matches    INTEGER,                     -- on any hero
    PRIMARY KEY (match_id, player_slot)
);
"""

# matches' columns added after the first release.
ADDED_MATCH_COLUMNS = (
    "average_badge_team0",
    "average_badge_team1",
    "low_pri_pool",
    "new_player_pool",
    "high_skill_parties",
    "player_stats_at",
)

# match_players' columns before the badge ones, for rebuilding a table from the first release.
OLD_MATCH_PLAYER_COLUMNS = (
    "match_id, player_slot, account_id, team, hero_id, kills, deaths, assists, net_worth, player_damage, "
    "objective_damage, ally_healing, mvp_rank, abandon_time_s"
)

SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS accounts (
    account_id      INTEGER PRIMARY KEY,          -- SteamID3
    added_at        INTEGER NOT NULL,
    persona_name    TEXT,
    avatar_url      TEXT,
    last_synced_at  INTEGER,
    last_full_sync_at INTEGER,
    last_source     TEXT,                         -- where the last sync read matches from
    pending_matches INTEGER,                      -- left to download after a Steam rate limit
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
    fetched_at      INTEGER NOT NULL,
    -- 1: Valve's match file (all 12 players, every stat). 0: only the tracked player's line
    -- from their match history, until the match file can be downloaded.
    detailed        INTEGER NOT NULL DEFAULT 1,
    -- 1: played out but doesn't count (e.g. someone left early); excluded from games and win
    -- rate like in game. NULL: unknown (stored before this was recorded).
    not_scored      INTEGER,
    -- Matchmaking details from Valve's match file; NULL until it is downloaded.
    average_badge_team0 INTEGER,                  -- ranked matches only
    average_badge_team1 INTEGER,
    low_pri_pool    INTEGER,
    new_player_pool INTEGER,
    high_skill_parties INTEGER,                   -- is_high_skill_range_parties
    player_stats_at INTEGER                       -- when the players' hero stats were looked up
);
"""
    + MATCH_PLAYERS
    + """
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

-- Steam names and avatars of everyone in a stored match (not only tracked accounts), for the
-- match pages. Filled in the background from deadlock-api, then the bot's Steam connection.
CREATE TABLE IF NOT EXISTS players (
    account_id      INTEGER PRIMARY KEY,
    persona_name    TEXT,                         -- NULL: no profile found
    avatar_url      TEXT,
    fetched_at      INTEGER NOT NULL,             -- of the name; 0: not looked up yet
    -- Rank after their latest ranked match (deadlock-api); 0: none known.
    badge           INTEGER,
    badge_at        INTEGER,                      -- start of that ranked match
    badge_fetched_at INTEGER,
    friends_public  INTEGER                       -- 1: deadlock-api had a non-empty friend list
);

-- Public Steam friend lists (deadlock-api), to tell which players queued together: Valve's
-- match file doesn't record parties.
CREATE TABLE IF NOT EXISTS player_friends (
    account_id      INTEGER NOT NULL,
    friend_id       INTEGER NOT NULL,
    since           INTEGER,                      -- unix seconds
    PRIMARY KEY (account_id, friend_id)
);

CREATE TABLE IF NOT EXISTS heroes (
    hero_id         INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    icon_url        TEXT
);

CREATE TABLE IF NOT EXISTS ranks (
    tier            INTEGER PRIMARY KEY,          -- badge // 10; 0 is Obscurus (unranked)
    name            TEXT NOT NULL,
    image_url       TEXT
);

CREATE TABLE IF NOT EXISTS job_runs (
    job             TEXT PRIMARY KEY,
    last_started_at INTEGER,
    last_finished_at INTEGER,
    last_error      TEXT
);
"""
)


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
    if "pending_matches" not in columns:
        conn.execute("ALTER TABLE accounts ADD COLUMN pending_matches INTEGER")
    match_columns = {r["name"] for r in conn.execute("PRAGMA table_info(matches)")}
    if "detailed" not in match_columns:
        conn.execute("ALTER TABLE matches ADD COLUMN detailed INTEGER NOT NULL DEFAULT 1")
    if "not_scored" not in match_columns:
        conn.execute("ALTER TABLE matches ADD COLUMN not_scored INTEGER")
    for column in ADDED_MATCH_COLUMNS:
        if column not in match_columns:
            conn.execute(f"ALTER TABLE matches ADD COLUMN {column} INTEGER")
    player_columns = {r["name"] for r in conn.execute("PRAGMA table_info(players)")}
    for column in ("badge", "badge_at", "badge_fetched_at", "friends_public"):
        if column not in player_columns:
            conn.execute(f"ALTER TABLE players ADD COLUMN {column} INTEGER")
    if any(r["name"] == "player_damage" and r["notnull"] for r in conn.execute("PRAGMA table_info(match_players)")):
        # SQLite can't drop NOT NULL in place: rebuild the table with the nullable columns.
        conn.executescript(
            "BEGIN;"
            "ALTER TABLE match_players RENAME TO match_players_old;"
            + MATCH_PLAYERS
            + f"INSERT INTO match_players ({OLD_MATCH_PLAYER_COLUMNS})"
            f" SELECT {OLD_MATCH_PLAYER_COLUMNS} FROM match_players_old;"
            "DROP TABLE match_players_old;"
            "CREATE INDEX IF NOT EXISTS match_players_account ON match_players(account_id, match_id);"
            "COMMIT;"
        )
    mp_columns = {r["name"] for r in conn.execute("PRAGMA table_info(match_players)")}
    for column, kind in (
        ("badge", "INTEGER"),
        ("badge_source", "TEXT"),
        ("hero_matches", "INTEGER"),
        ("hero_wins", "INTEGER"),
        ("total_matches", "INTEGER"),
    ):
        if column not in mp_columns:
            conn.execute(f"ALTER TABLE match_players ADD COLUMN {column} {kind}")
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
