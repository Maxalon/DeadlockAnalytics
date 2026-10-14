import math
import random

from app import jobs, queries
from app.config import Settings

from .conftest import FakeApi, insert_snapshot


def settings(**kw):
    return Settings(database_path=":memory:", run_scheduler=False, **kw)


def test_store_match_maps_valve_columns(conn, stored_match):
    rows = queries.match_players(conn, stored_match["match_id"])
    assert len(rows) == 12
    apollo = next(r for r in rows if r["player_slot"] == 7)
    assert (apollo["player_damage"], apollo["objective_damage"], apollo["ally_healing"]) == (29782, 7641, 0)
    assert apollo["mvp_rank"] == 1 and apollo["team"] == 1
    match = conn.execute("SELECT * FROM matches").fetchone()
    assert match["winning_team"] == 1 and match["start_time"] == 1791848958


def test_sync_account_stores_matches_and_is_idempotent(conn, match_payload):
    conn.execute("INSERT INTO accounts (account_id, added_at) VALUES (113956776, 0)")
    api = FakeApi(pages=[match_payload, match_payload])
    assert jobs.sync_account(conn, api, 113956776, settings()) == 1
    assert jobs.sync_account(conn, api, 113956776, settings()) == 1
    assert conn.execute("SELECT COUNT(*) FROM match_players").fetchone()[0] == 12
    # second sync looks back from the last sync time instead of the backfill date
    assert api.calls[0]["min_unix_timestamp"] == settings().backfill_since
    assert api.calls[1]["min_unix_timestamp"] > settings().backfill_since


def test_sync_paginates(conn, match_payload, monkeypatch):
    monkeypatch.setattr(jobs, "PAGE_SIZE", 1)
    conn.execute("INSERT INTO accounts (account_id, added_at) VALUES (1, 0)")
    api = FakeApi(pages=[match_payload, []])
    jobs.sync_account(conn, api, 1, settings())
    assert api.calls[1]["min_match_id"] == 115701683


def test_welford_matches_two_pass():
    xs = [random.Random(1).gauss(50, 9) for _ in range(500)]
    w = jobs.Welford()
    for x in xs:
        w.add(x)
    mean = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (len(xs) - 1))
    assert math.isclose(w.mean, mean) and math.isclose(w.sd, sd)


def test_compute_baselines_stores_snapshot(conn, match_payload):
    api = FakeApi(baseline_pages=[match_payload * 3, []])
    sid = jobs.compute_baselines(conn, api, settings(baseline_max_matches=100))
    snap = conn.execute("SELECT * FROM baseline_snapshots WHERE snapshot_id = ?", (sid,)).fetchone()
    assert snap["n_matches"] == 3
    row = conn.execute(
        "SELECT * FROM hero_baselines WHERE snapshot_id = ? AND hero_id = 77 AND stat = 'kills'", (sid,)
    ).fetchone()
    assert row["n"] == 3 and row["mean"] == 10 and row["sd"] == 0


def test_snapshot_choice_prefers_the_one_computed_after_the_match(conn, research_baselines):
    old = insert_snapshot(conn, research_baselines, window_end=1791000000)
    after = insert_snapshot(conn, research_baselines, window_end=1791900000)
    newer = insert_snapshot(conn, research_baselines, window_end=1792500000)
    assert queries.snapshot_for(conn, 1791848958)["snapshot_id"] == after  # window contains the match
    short = insert_snapshot(conn, research_baselines, window_end=1791850000)
    conn.execute("UPDATE baseline_snapshots SET window_start = 1791849000 WHERE snapshot_id = ?", (short,))
    assert queries.snapshot_for(conn, 1791848958)["snapshot_id"] == after  # short window misses the match
    assert queries.snapshot_for(conn, 1799999999)["snapshot_id"] == newer
    assert queries.snapshot_for(conn, 1700000000)["snapshot_id"] == old


def test_parse_account_id():
    assert queries.parse_account_id("113956776") == 113956776
    assert queries.parse_account_id("[U:1:113956776]") == 113956776
    assert queries.parse_account_id(str(76561197960265728 + 113956776)) == 113956776
    assert queries.parse_account_id(f"https://steamcommunity.com/profiles/{76561197960265728 + 5}/") == 5
    for bad in ("", "abc", "0", "https://steamcommunity.com/id/vanity"):
        try:
            queries.parse_account_id(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_full_resync_once_a_day_picks_up_late_old_matches(conn, match_payload):
    conn.execute("INSERT INTO accounts (account_id, added_at) VALUES (113956776, 0)")
    api = FakeApi(pages=[[], [], match_payload])
    jobs.sync_account(conn, api, 113956776, settings())  # first sync: full history
    jobs.sync_account(conn, api, 113956776, settings())  # same day: only the lookback window
    assert api.calls[0]["min_unix_timestamp"] == settings().backfill_since
    assert api.calls[1]["min_unix_timestamp"] > settings().backfill_since
    # A day later the full history is read again, so an old match ingested meanwhile is stored.
    conn.execute("UPDATE accounts SET last_full_sync_at = last_full_sync_at - ?", (jobs.FULL_RESYNC_S,))
    assert jobs.sync_account(conn, api, 113956776, settings()) == 1
    assert api.calls[2]["min_unix_timestamp"] == settings().backfill_since
    assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 1


def test_init_adds_new_columns_to_an_existing_db():
    from app import db

    c = db.connect(":memory:")
    c.execute(
        "CREATE TABLE accounts (account_id INTEGER PRIMARY KEY, added_at INTEGER NOT NULL, last_synced_at INTEGER)"
    )
    db.init(c)
    db.init(c)  # idempotent
    columns = {r["name"] for r in c.execute("PRAGMA table_info(accounts)")}
    assert "last_full_sync_at" in columns
