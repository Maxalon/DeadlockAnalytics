from fastapi.testclient import TestClient

from app import jobs
from app.config import Settings
from app.main import create_app

from .conftest import FakeApi, FakeFetcher


def client(tmp_path, fetcher=None, **kw):
    s = Settings(database_path=str(tmp_path / "t.sqlite3"), run_scheduler=False, **kw)
    return TestClient(create_app(s, api=FakeApi(), fetcher=fetcher)), s


def seed(s, match_payload, research_baselines):
    from app import db

    from .conftest import insert_snapshot

    c = db.connect(s.database_path)
    with db.transaction(c):
        jobs.store_match(c, match_payload[0])
        c.execute("INSERT OR IGNORE INTO accounts (account_id, added_at) VALUES (113956776, 0)")
    jobs.refresh_heroes(c, FakeApi())
    insert_snapshot(c, research_baselines)
    c.close()


def test_pages_render(tmp_path, match_payload, research_baselines):
    c, s = client(tmp_path)
    seed(s, match_payload, research_baselines)
    for url in ("/", "/accounts", "/model", "/players/113956776", "/matches/115701682", "/healthz"):
        r = c.get(url)
        assert r.status_code == 200, url
    match = c.get("/matches/115701682").text
    assert "+3.05" in match and "+2.16" in match
    assert c.get("/players/113956776").text.count("<svg") == 0  # one match: no trend yet
    assert c.get("/matches/1").status_code == 404


def test_add_and_remove_account(tmp_path):
    c, _ = client(tmp_path)
    r = c.post("/accounts", data={"account": "[U:1:42]"}, follow_redirects=False)
    assert r.status_code == 303
    assert "42" in c.get("/accounts").text
    bad = c.post("/accounts", data={"account": "nope"}, follow_redirects=False)
    assert "error=" in bad.headers["location"]
    c.post("/accounts/42/delete")
    assert "None yet" in c.get("/accounts").text


def test_cross_site_posts_are_refused(tmp_path):
    c, _ = client(tmp_path)
    r = c.post("/accounts", data={"account": "42"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    ok = c.post("/accounts", data={"account": "42"}, headers={"origin": "http://testserver"}, follow_redirects=False)
    assert ok.status_code == 303


def test_dark_is_default_and_light_is_remembered(tmp_path):
    c, _ = client(tmp_path)
    assert 'data-theme="dark"' in c.get("/").text
    r = c.get("/theme?to=light&next=/accounts", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/accounts"
    assert 'data-theme="light"' in c.get("/").text
    for bad in ("//evil.example", "/\\evil.example", "https://evil.example"):
        assert c.get("/theme", params={"to": "dark", "next": bad}, follow_redirects=False).headers["location"] == "/"
    assert 'data-theme="dark"' in c.get("/").text


def test_viewing_a_player_queues_a_repull_with_cooldown(tmp_path, match_payload, research_baselines):
    c, s = client(tmp_path)
    seed(s, match_payload, research_baselines)
    sched = c.app.state.scheduler
    page = c.get("/players/113956776").text
    assert sched.is_syncing(113956776)
    assert 'http-equiv="refresh"' in page and "Pulling matches" in page
    # Once pulled, views within the cooldown don't queue again (no refresh loop).
    from app import db

    conn = db.connect(s.database_path)
    sched._sync_pending_accounts(conn)
    conn.close()
    assert not sched.is_syncing(113956776)
    page = c.get("/players/113956776").text
    assert not sched.is_syncing(113956776)
    assert 'http-equiv="refresh"' not in page and "Re-pull matches" in page


def test_repull_button_queues_only_tracked_accounts(tmp_path, match_payload, research_baselines):
    c, s = client(tmp_path)
    seed(s, match_payload, research_baselines)
    sched = c.app.state.scheduler
    r = c.post("/players/113956776/sync", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/players/113956776"
    assert sched.is_syncing(113956776)
    c.post("/players/894295721/sync")  # in a stored match, but not tracked
    assert not sched.is_syncing(894295721)


def test_adding_an_account_queues_only_that_account(tmp_path):
    c, _ = client(tmp_path)
    c.post("/accounts", data={"account": "42"})
    assert c.app.state.scheduler.is_syncing(42)
    assert not c.app.state.scheduler.sync_requested.is_set()


def test_bot_friend_requests_are_handled_on_the_accounts_page(tmp_path):
    fetcher = FakeFetcher()
    fetcher.bot["incoming"] = [{"account_id": 42, "name": "someone"}]
    fetcher.bot["friends"] = [{"account_id": 7, "name": None}]
    c, _ = client(tmp_path, fetcher=fetcher)
    page = c.get("/accounts").text
    assert "someone" in page and "Accept &amp; track" in page and "Decline all 1" in page

    for action in ("accept", "decline", "block", "remove", "unblock"):
        assert c.post(f"/bot/friends/42/{action}", follow_redirects=False).status_code == 303
    assert c.post("/bot/friends/42/befriend").status_code == 404
    c.post("/bot/friends/decline-all")
    assert fetcher.actions == [(42, a) for a in ("accept", "decline", "block", "remove", "unblock")] + [
        ("all", "decline")
    ]

    # accept & track: befriends and starts tracking in one step
    c.post("/bot/friends/42/accept-track")
    assert fetcher.actions[-1] == (42, "accept")
    assert "/players/42" in c.get("/accounts").text


def test_bot_errors_are_shown_not_raised(tmp_path):
    from app.fetcher import FetcherError

    fetcher = FakeFetcher()

    def down():
        raise FetcherError("unreachable", "fetcher unreachable: refused")

    fetcher.friends = down
    c, _ = client(tmp_path, fetcher=fetcher)
    assert "fetcher unreachable" in c.get("/accounts").text


def test_without_a_bot_there_is_no_friends_section(tmp_path):
    c, _ = client(tmp_path)
    assert "Steam bot friends" not in c.get("/accounts").text
    assert c.post("/bot/friends/42/accept").status_code == 404


def test_pages_render_with_history_only_matches(tmp_path, research_baselines):
    from app import db

    from .conftest import insert_snapshot
    from .test_fetcher import history_entry

    c, s = client(tmp_path)
    conn = db.connect(s.database_path)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO accounts (account_id, added_at, pending_matches, last_synced_at, last_full_sync_at)"
            " VALUES (113956776, 0, 2, 4000000000, 4000000000)"  # synced "now": the page doesn't re-pull
        )
        jobs.store_history_entry(conn, 113956776, history_entry(115701682))
        jobs.store_history_entry(conn, 113956776, {**history_entry(115701683), "player_match_outcome": 5})
    insert_snapshot(conn, research_baselines)
    conn.close()
    for url in ("/", "/accounts", "/players/113956776", "/matches/115701682"):
        assert c.get(url).status_code == 200, url
    player = c.get("/players/113956776").text
    assert "2 matches without details yet" in player and "not scored" in player
    assert "Only the tracked player" in c.get("/matches/115701682").text


def test_old_database_is_migrated_without_losing_matches(tmp_path, match_payload):
    import sqlite3

    from app import db

    path = str(tmp_path / "old.sqlite3")
    old = sqlite3.connect(path)
    old.executescript(
        """CREATE TABLE matches (match_id INTEGER PRIMARY KEY, start_time INTEGER NOT NULL,
             duration_s INTEGER NOT NULL, match_mode TEXT NOT NULL, game_mode TEXT NOT NULL,
             winning_team INTEGER NOT NULL, average_badge INTEGER, fetched_at INTEGER NOT NULL);
           CREATE TABLE match_players (match_id INTEGER NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
             player_slot INTEGER NOT NULL, account_id INTEGER NOT NULL, team INTEGER NOT NULL,
             hero_id INTEGER NOT NULL, kills INTEGER NOT NULL, deaths INTEGER NOT NULL, assists INTEGER NOT NULL,
             net_worth INTEGER NOT NULL, player_damage INTEGER NOT NULL, objective_damage INTEGER NOT NULL,
             ally_healing INTEGER NOT NULL, mvp_rank INTEGER, abandon_time_s INTEGER NOT NULL DEFAULT 0,
             PRIMARY KEY (match_id, player_slot));
           CREATE INDEX match_players_account ON match_players(account_id, match_id);
           INSERT INTO matches VALUES (1, 0, 60, 'Unranked', 'Normal', 1, NULL, 0);
           INSERT INTO match_players VALUES (1, 3, 113956776, 1, 3, 1, 2, 3, 4000, 500, 60, 7, 1, 0);"""
    )
    old.close()
    conn = db.connect(path)
    db.init(conn)
    row = conn.execute("SELECT * FROM match_players").fetchone()
    assert (row["player_slot"], row["player_damage"], row["mvp_rank"]) == (3, 500, 1)
    assert tuple(conn.execute("SELECT detailed, not_scored FROM matches").fetchone()) == (1, None)
    nullable = {r["name"]: not r["notnull"] for r in conn.execute("PRAGMA table_info(match_players)")}
    assert nullable["player_damage"] and nullable["ally_healing"]
    indexes = [r["name"] for r in conn.execute("PRAGMA index_list(match_players)")]
    assert "match_players_account" in indexes
    db.init(conn)  # idempotent


def test_opening_a_match_without_details_queues_its_fetch(tmp_path):
    from app import db

    from .test_fetcher import history_entry

    fetcher = FakeFetcher()
    c, s = client(tmp_path, fetcher=fetcher)
    conn = db.connect(s.database_path)
    with db.transaction(conn):
        conn.execute("INSERT INTO accounts (account_id, added_at) VALUES (113956776, 0)")
        jobs.store_history_entry(conn, 113956776, history_entry(115701682))
    conn.close()
    sched = c.app.state.scheduler

    page = c.get("/matches/115701682").text
    assert sched.is_fetching_match(115701682) and "Fetching this match" in page and 'http-equiv="refresh"' in page
    # viewing the player doesn't spend the bot's budget on its own
    c.get("/players/113956776")
    assert not sched.is_syncing(113956776)

    # the bot can't right now: the reason is shown, and it isn't retried on every reload
    sched._match_queue.clear()
    sched.match_errors[115701682] = ("the bot's daily budget for match salts is used up", 4_000_000_000)
    page = c.get("/matches/115701682").text
    assert "budget for match salts is used up" in page and not sched.is_fetching_match(115701682)


def test_accounts_page_shows_the_bots_budget(tmp_path):
    c, _ = client(tmp_path, fetcher=FakeFetcher())
    page = c.get("/accounts").text
    assert "38/40 match details" in page and "17/20 history pulls" in page


def test_chart_points_link_to_their_match_with_details():
    from app import charts

    def m(i, score, gap, rank=None):
        return {
            "match_id": 1000 + i,
            "start_time": 1791848958 + i * 3600,
            "hero_id": 3,
            "won": i % 2 == 0,
            "not_scored": 0,
            "kills": 12,
            "deaths": 2,
            "assists": 21,
            "score": score,
            "mvp_gap": gap,
            "mvp_rank": rank,
        }

    svg = str(charts.score_trend([m(0, 1.5, -0.5), m(1, 2.0, 0.0, 1), m(2, -1.0, None)], {3: "Vindicta"}))
    assert svg.count('class="hit"') == 3
    assert 'href="/matches/1001"' in svg and 'data-rank="MVP"' in svg
    assert 'data-hero="Vindicta"' in svg and 'data-kda="12 / 2 / 21"' in svg and 'data-mvp="+2.00"' in svg
    assert "<title>2026-10-12 23:49 UTC · Vindicta · Win" in svg  # native tooltip without JavaScript
