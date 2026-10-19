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


def test_scheduler_repulls_queued_accounts_in_full(match_payload):
    from app import db
    from app.scheduler import Scheduler

    conn = db.connect(":memory:")
    db.init(conn)
    conn.execute(
        "INSERT INTO accounts (account_id, added_at, last_synced_at, last_full_sync_at) VALUES (1, 0, ?, ?)",
        (2_000_000_000, 2_000_000_000),
    )
    api = FakeApi(pages=[match_payload])
    sched = Scheduler(settings(), api)
    sched.request_account_sync(1)
    sched.request_account_sync(1)  # queued twice -> pulled once
    sched._sync_pending_accounts(conn)
    assert len(api.calls) == 1 and api.calls[0]["min_unix_timestamp"] == settings().backfill_since
    assert not sched.is_syncing(1)
    assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 1
    assert conn.execute("SELECT persona_name FROM accounts").fetchone()[0] == "player1"


def _set_now(monkeypatch, now):
    monkeypatch.setattr(jobs.time, "time", lambda: now)


def test_names_deadlock_api_first_then_the_bots_steam_connection_misses_retried_a_day_later(
    conn, stored_match, monkeypatch
):
    from .conftest import FakeFetcher

    ids = {p["account_id"] for p in stored_match["players"]}
    on_steam, nowhere = 894295721, 113956776
    api = FakeApi(profiles=ids - {on_steam, nowhere})
    fetcher = FakeFetcher()
    fetcher.steam_profiles = {on_steam}
    _set_now(monkeypatch, 1_800_000_000)
    assert jobs.refresh_player_names(conn, api, fetcher) == 12
    assert [sorted(c) for c in fetcher.profile_calls] == [sorted([on_steam, nowhere])]  # only deadlock-api's misses
    names = dict(conn.execute("SELECT account_id, persona_name FROM players").fetchall())
    assert names[on_steam] == f"steam{on_steam}" and names[nowhere] is None
    assert sum(n is not None and n.startswith("player") for n in names.values()) == 10
    assert jobs.refresh_player_names(conn, api, fetcher) == 0  # remembered
    _set_now(monkeypatch, 1_800_000_000 + jobs.PROFILE_MISS_RETRY_S + 1)
    assert jobs.refresh_player_names(conn, api, fetcher) == 1  # only the one nobody knew


def test_names_without_a_bot_or_with_the_bot_down(conn, stored_match):
    from app.fetcher import FetcherError

    from .conftest import FakeFetcher

    class Down(FakeFetcher):
        def profiles(self, ids):
            raise FetcherError("not_ready", "the bot is not logged in to Steam")

    assert jobs.refresh_player_names(conn, FakeApi(profiles=set()), Down()) == 12
    assert conn.execute("SELECT COUNT(*) FROM players WHERE persona_name IS NULL").fetchone()[0] == 12
    conn.execute("DELETE FROM players")
    assert jobs.refresh_player_names(conn, FakeApi(profiles=set()), None) == 12


def test_ranks_refreshed_daily_and_kept_on_matches_from_the_last_two_days(conn, stored_match, monkeypatch):
    match_id, start = 115701682, 1791848958
    apollo, other = 894295721, 113956776
    api = FakeApi(badges={apollo: (64, 1791000000), other: (91, 1791800000)})

    def players():
        return {p["account_id"]: p for p in queries.match_players(conn, match_id)}

    # a day after the match: the lookup counts as the rank at the time
    _set_now(monkeypatch, start + 86400)
    assert jobs.refresh_player_ranks(conn, api) == 12
    p = players()
    assert (p[apollo]["badge"], p[apollo]["badge_source"], p[apollo]["current_badge"]) == (64, "current", 64)
    assert p[other]["current_badge_at"] == 1791800000
    assert sum(x["current_badge"] == 0 and x["badge"] is None for x in p.values()) == 10
    assert jobs.refresh_player_ranks(conn, api) == 0  # not again within a day
    # a week later the rank moved: the match keeps the one from back then
    api.badges[apollo] = (66, 1792000000)
    _set_now(monkeypatch, start + 8 * 86400)
    assert jobs.refresh_player_ranks(conn, api) == 12
    assert (players()[apollo]["badge"], players()[apollo]["current_badge"]) == (64, 66)
    # downloading the match file again keeps ranks looked up meanwhile
    jobs.store_match(conn, stored_match)
    assert (players()[apollo]["badge"], players()[apollo]["badge_source"]) == (64, "current")


def test_ranks_too_late_for_a_match_are_not_kept_on_it(conn, stored_match, monkeypatch):
    _set_now(monkeypatch, 1791848958 + 3 * 86400)
    jobs.refresh_player_ranks(conn, FakeApi(badges={894295721: (64, 1791000000)}))
    p = {x["account_id"]: x for x in queries.match_players(conn, 115701682)}
    assert (p[894295721]["badge"], p[894295721]["current_badge"]) == (None, 64)


def test_ranked_match_badges_and_matchmaking_pool_are_stored(conn, fetcher_match):
    m = {**fetcher_match, "match_mode": "Ranked", "low_pri_pool": True, "average_badge_team0": 63}
    m["players"] = [{**p, "badge": 60 + i % 6 + 1} for i, p in enumerate(fetcher_match["players"])]
    jobs.store_match(conn, m)
    row = conn.execute("SELECT * FROM matches").fetchone()
    assert (row["low_pri_pool"], row["new_player_pool"], row["average_badge_team0"]) == (1, None, 63)
    players = queries.match_players(conn, m["match_id"])
    assert all(p["badge_source"] == "match" for p in players)
    assert {p["badge"] for p in players} == {61, 62, 63, 64, 65, 66}


def test_average_badge_keeps_subranks_one_to_six_apart():
    assert queries.average_badge([]) is None
    assert queries.average_badge([26, 26]) == 26
    assert queries.average_badge([26, 31]) == 31
    assert queries.average_badge([61, 66, 63]) == 63


# Match 115701682: team 0 holds 110786971 and 1028417007 (friends 7 minutes before it started),
# team 1 holds 925178794 and 894295721 (friends since 2022); 79553708 is on team 0.
START = 1791848958


def test_parties_are_teammates_who_were_friends_before_the_match(conn, stored_match):
    api = FakeApi(
        friends={
            110786971: [(1028417007, "2026-10-12T23:41:55Z"), (79553708, "2026-10-13T08:00:00Z")],  # 2nd: after
            925178794: [(894295721, "2022-05-26T19:41:19Z"), (113956776, "2020-01-01T00:00:00Z")],
            894295721: [(110786971, "2020-01-01T00:00:00Z")],  # friends, but on the other team
        }
    )
    jobs.refresh_player_names(conn, api)
    match = conn.execute("SELECT * FROM matches").fetchone()
    players = queries.match_players(conn, 115701682)
    party_of, known = queries.parties(conn, match, players)
    assert known == 3  # public friend lists
    assert party_of[110786971] == party_of[1028417007]
    assert 79553708 not in party_of  # befriended only after the match
    # one friend list is enough, and friends of friends are one party: a trio
    assert party_of[925178794] == party_of[894295721] == party_of[113956776] != party_of[110786971]
    data = queries.scored_match(conn, ":memory:", 115701682)
    assert data["team_badges"][0]["parties"] == [2] and data["team_badges"][1]["parties"] == [3]


def test_profiles_are_looked_up_again_after_a_newer_match_for_its_friendships(conn, stored_match, monkeypatch):
    _set_now(monkeypatch, START - 3600)  # profile from before the match
    assert jobs.refresh_player_names(conn, FakeApi()) == 12
    _set_now(monkeypatch, START + 3600)
    assert jobs.refresh_player_names(conn, FakeApi()) == 12
    assert jobs.refresh_player_names(conn, FakeApi()) == 0


def test_hero_stats_before_the_match_once_per_match(conn, stored_match):
    apollo = 894295721
    api = FakeApi(
        hero_stats=[(apollo, 77, 4, 3, START - 86400), (apollo, 3, 6, 1, START - 86400), (apollo, 77, 1, 1, START)]
    )
    assert jobs.fetch_player_stats(conn, api, 115701682)
    assert api.hero_stats_calls[0][1] == START - 1  # only matches that started before this one
    p = {x["account_id"]: x for x in queries.match_players(conn, 115701682)}
    assert (p[apollo]["hero_matches"], p[apollo]["hero_wins"], p[apollo]["total_matches"]) == (4, 3, 10)
    assert (p[79553708]["hero_matches"], p[79553708]["total_matches"]) == (0, 0)
    assert not jobs.fetch_player_stats(conn, api, 115701682)  # once
    jobs.store_match(conn, stored_match)  # player lines replaced: looked up again
    assert jobs.fetch_player_stats(conn, api, 115701682)
