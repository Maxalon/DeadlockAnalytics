"""Read-side queries for the web UI."""

import re
import sqlite3
from functools import lru_cache

from . import scoring

STEAMID64_BASE = 76561197960265728


def parse_account_id(text: str) -> int:
    """Accept SteamID3 (123 or [U:1:123]), SteamID64, or a steamcommunity.com/profiles/<id64> URL."""
    text = text.strip()
    if m := re.fullmatch(r"\[?U:1:(\d+)\]?", text):
        return int(m.group(1))
    if m := re.search(r"/profiles/(\d{17})", text):
        text = m.group(1)
    if not text.isdigit():
        raise ValueError("Enter a SteamID3, SteamID64 or steamcommunity.com/profiles/… link")
    value = int(text)
    if value >= STEAMID64_BASE:
        value -= STEAMID64_BASE
    if not 0 < value < 2**32:
        raise ValueError("That is not a valid Steam account ID")
    return value


def snapshot_for(conn: sqlite3.Connection, start_time: int):
    """Baselines for a match: the earliest snapshot whose window contains the match (computed
    soonest after it); else the first one computed after it; else the latest."""
    containing = conn.execute(
        "SELECT * FROM baseline_snapshots WHERE window_start <= ? AND window_end >= ? ORDER BY window_end LIMIT 1",
        (start_time, start_time),
    ).fetchone()
    if containing:
        return containing
    after = conn.execute(
        "SELECT * FROM baseline_snapshots WHERE window_end >= ? ORDER BY window_end LIMIT 1", (start_time,)
    ).fetchone()
    return after or conn.execute("SELECT * FROM baseline_snapshots ORDER BY window_end DESC LIMIT 1").fetchone()


@lru_cache(maxsize=64)
def _baselines(db_path: str, snapshot_id: int) -> dict:
    # Snapshots are immutable once written, so caching by id is safe.
    from .db import connect

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT hero_id, stat, mean, sd FROM hero_baselines WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchall()
    finally:
        conn.close()
    return {(r["hero_id"], r["stat"]): (r["mean"], r["sd"]) for r in rows}


def baselines_for(conn: sqlite3.Connection, db_path: str, start_time: int) -> tuple[dict, sqlite3.Row | None]:
    snap = snapshot_for(conn, start_time)
    if snap is None:
        return {}, None
    if db_path == ":memory:":  # tests: can't reopen an in-memory db
        rows = conn.execute(
            "SELECT hero_id, stat, mean, sd FROM hero_baselines WHERE snapshot_id = ?", (snap["snapshot_id"],)
        ).fetchall()
        return {(r["hero_id"], r["stat"]): (r["mean"], r["sd"]) for r in rows}, snap
    return _baselines(db_path, snap["snapshot_id"]), snap


def ranks(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    return {r["tier"]: r for r in conn.execute("SELECT * FROM ranks")}


def heroes(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    return {r["hero_id"]: r for r in conn.execute("SELECT * FROM heroes")}


def accounts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT a.*, COUNT(mp.match_id) - SUM(IFNULL(m.not_scored, 0)) AS games,
                  SUM(mp.team = m.winning_team AND NOT IFNULL(m.not_scored, 0)) AS wins,
                  SUM(IFNULL(m.not_scored, 0)) AS not_scored,
                  SUM(IFNULL(mp.mvp_rank, 0) = 1) AS mvps,
                  SUM(IFNULL(mp.mvp_rank, 0) IN (2, 3)) AS key_players,
                  MAX(m.start_time) AS last_match
           FROM accounts a
           LEFT JOIN match_players mp ON mp.account_id = a.account_id
           LEFT JOIN matches m ON m.match_id = mp.match_id
           GROUP BY a.account_id ORDER BY a.added_at"""
    ).fetchall()
    return [dict(r) for r in rows]


def match_players(conn: sqlite3.Connection, match_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """SELECT mp.*, p.persona_name, p.avatar_url, p.badge AS current_badge, p.badge_at AS current_badge_at
               FROM match_players mp
               LEFT JOIN players p ON p.account_id = mp.account_id
               WHERE mp.match_id = ? ORDER BY mp.team, mp.player_slot""",
            (match_id,),
        )
    ]


def average_badge(badges: list[int]) -> int | None:
    """Average of rank badges (tier*10 + subrank, subranks 1-6) on a contiguous scale, as Valve's
    match files and deadlock-api average them."""
    if not badges:
        return None
    avg = round(sum(b // 10 * 6 + b % 10 - 1 for b in badges) / len(badges))
    return avg // 6 * 10 + avg % 6 + 1


def scored_match(conn: sqlite3.Connection, db_path: str, match_id: int) -> dict | None:
    match = conn.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,)).fetchone()
    if match is None:
        return None
    base, snap = baselines_for(conn, db_path, match["start_time"])
    players = match_players(conn, match_id)
    for p in players:
        p["components"] = scoring.components(p, base)
        p["score"] = scoring.score(p, base)
        p["sort_score"] = p["score"] if p["score"] is not None else float("-inf")
    predicted = scoring.predict_ranks(players, {p["player_slot"]: p["score"] for p in players}, match["winning_team"])
    for p in players:
        p["predicted_rank"] = predicted.get(p["player_slot"])
    # Rank within own team by score (1 = team's best).
    for team in (0, 1):
        ranked = sorted(
            (p for p in players if p["team"] == team and p["score"] is not None), key=lambda p: p["score"], reverse=True
        )
        for i, p in enumerate(ranked, 1):
            p["team_rank"] = i
    # Where Valve's MVP / Key Player ranks and the model's picks differ (only with baselines,
    # without them the model has no opinion).
    ranked = [p for p in players if p["mvp_rank"] or p["predicted_rank"]]
    model_check = None
    if snap and ranked:
        model_check = {"total": len(ranked), "agree": sum(p["mvp_rank"] == p["predicted_rank"] for p in ranked)}
    # Each team's average rank over the players one is known for: the match's own if it is
    # ranked, else the ranks looked up around it.
    team_badges = {}
    for team in (0, 1):
        known = [p["badge"] for p in players if p["team"] == team and p["badge"]]
        team_badges[team] = {"average": average_badge(known), "known": len(known)}
    return {
        "match": dict(match),
        "players": players,
        "team_badges": team_badges,
        "snapshot": dict(snap) if snap else None,
        "model_check": model_check,
    }


def player_matches(conn: sqlite3.Connection, db_path: str, account_id: int, limit: int, offset: int = 0) -> list[dict]:
    rows = conn.execute(
        """SELECT m.*, mp.* FROM match_players mp JOIN matches m ON m.match_id = mp.match_id
           WHERE mp.account_id = ? ORDER BY m.start_time DESC LIMIT ? OFFSET ?""",
        (account_id, limit, offset),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        base, _ = baselines_for(conn, db_path, d["start_time"])
        d["won"] = d["team"] == d["winning_team"]
        d["score"] = scoring.score(d, base)
        # The score that won MVP in this match, to show how far off the player was.
        best = None
        for p in match_players(conn, d["match_id"]):
            s = scoring.score(p, base)
            if p["team"] == d["winning_team"] and s is not None and (best is None or s > best):
                best = s
        d["mvp_gap"] = None if best is None or d["score"] is None else d["score"] - best
        out.append(d)
    return out


def hero_summary(conn: sqlite3.Connection, account_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT mp.hero_id, COUNT(*) AS games, SUM(mp.team = m.winning_team) AS wins,
                  SUM(IFNULL(mp.mvp_rank, 0) = 1) AS mvps, SUM(IFNULL(mp.mvp_rank, 0) IN (2, 3)) AS key_players,
                  AVG(mp.kills) AS kills, AVG(mp.deaths) AS deaths, AVG(mp.assists) AS assists,
                  AVG(mp.net_worth) AS net_worth, AVG(mp.player_damage) AS player_damage
           FROM match_players mp JOIN matches m ON m.match_id = mp.match_id
           WHERE mp.account_id = ? AND NOT IFNULL(m.not_scored, 0)  -- like in game
           GROUP BY mp.hero_id ORDER BY games DESC""",
        (account_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def latest_snapshot(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM baseline_snapshots ORDER BY snapshot_id DESC LIMIT 1").fetchone()


def snapshot_table(conn: sqlite3.Connection, snapshot_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT hero_id, stat, n, mean, sd FROM hero_baselines WHERE snapshot_id = ? ORDER BY hero_id",
        (snapshot_id,),
    ).fetchall()
    table: dict[int, dict] = {}
    for r in rows:
        entry = table.setdefault(r["hero_id"], {"hero_id": r["hero_id"], "n": r["n"]})
        entry[r["stat"]] = (r["mean"], r["sd"])
    return list(table.values())


def job_runs(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["job"]: dict(r) for r in conn.execute("SELECT * FROM job_runs")}
