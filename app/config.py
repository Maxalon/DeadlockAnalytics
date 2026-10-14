"""Runtime settings, read once from environment variables (see .env.example)."""

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def _ids(name: str) -> list[int]:
    raw = os.environ.get(name, "")
    return [int(x) for x in raw.replace(";", ",").split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    database_path: str = field(default_factory=lambda: os.environ.get("DATABASE_PATH", "/data/deadlock.sqlite3"))
    api_base: str = field(default_factory=lambda: os.environ.get("DEADLOCK_API_BASE", "https://api.deadlock-api.com"))
    # Optional; raises the deadlock-api rate limits. Sent as X-API-KEY.
    api_key: str = field(default_factory=lambda: os.environ.get("DEADLOCK_API_KEY", ""))
    # Accounts added on first start (SteamID3 or SteamID64). More can be added in the UI.
    seed_account_ids: list[int] = field(default_factory=lambda: _ids("TRACKED_ACCOUNT_IDS"))
    # How often tracked accounts are checked for new matches.
    sync_interval_s: int = field(default_factory=lambda: _int("SYNC_INTERVAL_S", 900))
    # Opening a player's page re-pulls their full history, at most this often per player.
    view_sync_cooldown_s: int = field(default_factory=lambda: _int("VIEW_SYNC_COOLDOWN_S", 120))
    # How often hero baselines are recomputed, and over how many days / matches.
    baseline_interval_s: int = field(default_factory=lambda: _int("BASELINE_INTERVAL_S", 86400))
    baseline_days: int = field(default_factory=lambda: _int("BASELINE_DAYS", 7))
    baseline_max_matches: int = field(default_factory=lambda: _int("BASELINE_MAX_MATCHES", 30000))
    # Backfill for a newly added account starts here (Valve began reporting MVP ranks on 2026-01-06).
    backfill_since: int = field(default_factory=lambda: _int("BACKFILL_SINCE_UNIX", 1767657600))
    # Set to 0 to run the web UI without the background jobs (e.g. in tests).
    run_scheduler: bool = field(default_factory=lambda: os.environ.get("RUN_SCHEDULER", "1") != "0")
