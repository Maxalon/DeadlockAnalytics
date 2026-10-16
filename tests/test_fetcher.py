import json

import httpx
import pytest

from app import jobs, queries
from app.config import Settings
from app.fetcher import Fetcher, FetcherError

from .conftest import FakeApi, FakeFetcher

ME = 113956776


def settings():
    return Settings(database_path=":memory:", run_scheduler=False)


# The 115701682 line of the tracked player's real match history, as the fetcher returns it.
REAL_ENTRY = {
    "match_id": 115701682,
    "hero_id": 3,
    "match_duration_s": 1613,
    "start_time": 1791848958,
    "match_result": 1,
    "player_team": 1,
    "player_kills": 12,
    "player_deaths": 2,
    "player_assists": 21,
    "last_hits": 128,
    "denies": 1,
    "hero_level": 31,
    "net_worth": 35006,
    "match_mode": 1,
    "game_mode": 1,
}


def history_entry(match_id, start_time=1791848958, game_mode=1, won=True):
    return {
        **REAL_ENTRY,
        "match_id": match_id,
        "start_time": start_time,
        "game_mode": game_mode,
        "match_result": 1 if won else 0,
    }


def track(conn):
    conn.execute("INSERT INTO accounts (account_id, added_at) VALUES (?, 0)", (ME,))


def test_fetcher_match_stores_the_same_rows_as_deadlock_api(conn, fetcher_match, match_payload):
    from app.db import transaction

    with transaction(conn):
        jobs.store_match(conn, match_payload[0])
    via_api = queries.match_players(conn, 115701682)
    with transaction(conn):
        conn.execute("DELETE FROM matches")
        jobs.store_match(conn, fetcher_match)
    assert queries.match_players(conn, 115701682) == via_api


def test_history_is_stored_at_once_then_enriched_newest_first(conn, fetcher_match):
    track(conn)
    other = {**fetcher_match, "match_id": 115700000}
    fetcher = FakeFetcher(
        history=[
            history_entry(115701682),
            history_entry(115700000, won=False),
            history_entry(115600000, game_mode=4),  # street brawl: not scored
            history_entry(100, start_time=1_700_000_000),  # before the backfill date
        ],
        matches={115701682: fetcher_match, 115700000: other},
    )
    api = FakeApi()
    assert jobs.sync_account(conn, api, ME, settings(), fetcher=fetcher) == 2
    assert fetcher.fetched == [115701682, 115700000] and api.calls == []  # newest first
    assert conn.execute("SELECT COUNT(*) FROM matches WHERE detailed = 1").fetchone()[0] == 2
    row = conn.execute("SELECT * FROM accounts").fetchone()
    assert row["last_source"] == "Steam bot" and row["last_error"] is None and row["pending_matches"] is None

    # nothing new: no downloads
    assert jobs.sync_account(conn, api, ME, settings(), fetcher=fetcher) == 0
    assert fetcher.fetched == [115701682, 115700000]


def test_rate_limited_matches_count_right_away_and_are_enriched_later(conn, fetcher_match):
    from app import queries

    track(conn)
    limited = FetcherError("rate_limited", "Steam rate-limited match metadata requests")
    fetcher = FakeFetcher(
        history=[history_entry(115701682), history_entry(115600000, won=False), history_entry(115500000)],
        errors={115701682: limited, 115600000: limited, 115500000: limited},
    )
    assert jobs.sync_account(conn, FakeApi(), ME, settings(), fetcher=fetcher) == 3
    # every match is tried: deadlock-api may have the salts of one the bot can't get right now
    assert fetcher.fetched == [115701682, 115600000, 115500000]

    # games and wins are right before any match file arrived (win = player_team == match_result)
    me = queries.accounts(conn)[0]
    assert (me["games"], me["wins"], me["pending_matches"]) == (3, 2, 3)
    assert me["last_error"] is None
    line = conn.execute("SELECT * FROM match_players WHERE match_id = 115701682").fetchone()
    assert (line["kills"], line["deaths"], line["assists"], line["net_worth"]) == (12, 2, 21, 35006)
    assert line["player_damage"] is None and line["player_slot"] == -ME

    # next sync: the match file replaces the history line with all 12 players
    del fetcher.errors[115701682]
    fetcher.matches[115701682] = fetcher_match
    fetcher.errors[115600000] = limited
    assert jobs.sync_account(conn, FakeApi(), ME, settings(), fetcher=fetcher) == 0
    assert conn.execute("SELECT COUNT(*) FROM match_players WHERE match_id = 115701682").fetchone()[0] == 12
    me = queries.accounts(conn)[0]
    assert (me["games"], me["wins"], me["pending_matches"]) == (3, 2, 2)


def test_unavailable_match_file_keeps_the_history_line(conn):
    track(conn)
    fetcher = FakeFetcher(
        history=[history_entry(115800000)], errors={115800000: FetcherError("in_flight", "still being processed")}
    )
    assert jobs.sync_account(conn, FakeApi(), ME, settings(), fetcher=fetcher) == 1
    row = conn.execute("SELECT * FROM accounts").fetchone()
    assert row["last_error"] is None and row["pending_matches"] == 1


def test_two_tracked_players_in_one_history_only_match(conn):
    from app.db import transaction

    track(conn)
    with transaction(conn):
        assert jobs.store_history_entry(conn, ME, history_entry(1))
        assert jobs.store_history_entry(conn, 7, {**history_entry(1), "player_team": 0})
        assert not jobs.store_history_entry(conn, 7, history_entry(1))  # already there
    assert conn.execute("SELECT COUNT(*) FROM match_players WHERE match_id = 1").fetchone()[0] == 2


def test_history_line_never_overwrites_a_full_match(conn, fetcher_match):
    from app.db import transaction

    with transaction(conn):
        jobs.store_match(conn, fetcher_match)
        assert not jobs.store_history_entry(conn, ME, history_entry(115701682))
    assert conn.execute("SELECT COUNT(*) FROM match_players").fetchone()[0] == 12


def test_rate_limit_on_history_waits_for_the_next_sync(conn):
    track(conn)
    fetcher = FakeFetcher()
    fetcher.history = lambda account_id: (_ for _ in ()).throw(FetcherError("rate_limited", "slow down"))
    api = FakeApi()
    assert jobs.sync_account(conn, api, ME, settings(), fetcher=fetcher) == 0
    assert api.calls == []
    assert "next sync" in conn.execute("SELECT last_error FROM accounts").fetchone()[0]


def test_falls_back_to_deadlock_api_and_says_why(conn, match_payload):
    track(conn)
    api = FakeApi(pages=[match_payload])
    assert jobs.sync_account(conn, api, ME, settings(), fetcher=FakeFetcher(ready=False)) == 1
    account = conn.execute("SELECT * FROM accounts").fetchone()
    assert account["last_source"] == "deadlock-api"
    assert "Steam bot not ready" in account["last_error"]


def test_bot_that_cant_see_the_player_falls_back(conn, match_payload):
    track(conn)
    fetcher = FakeFetcher()
    fetcher.history = lambda account_id: (_ for _ in ()).throw(FetcherError("private", "not friends?"))
    api = FakeApi(pages=[match_payload])
    assert jobs.sync_account(conn, api, ME, settings(), fetcher=fetcher) == 1
    assert "not friends?" in conn.execute("SELECT last_error FROM accounts").fetchone()[0]


def client(routes):
    def handler(request):
        status, body = routes[request.url.path]
        return httpx.Response(status, json=body)

    return Fetcher(
        "http://fetcher", client=httpx.Client(base_url="http://fetcher", transport=httpx.MockTransport(handler))
    )


def test_client_maps_fetcher_errors():
    f = client({"/matches/1": (409, {"error": "in_flight", "message": "match 1 is still being processed"})})
    with pytest.raises(FetcherError) as exc:
        f.match(1)
    assert exc.value.code == "in_flight" and "still being processed" in str(exc.value)


def test_client_not_ready_fails_fast():
    f = client({"/health": (200, {"logged_in": True, "gc_ready": False, "last_error": None})})
    with pytest.raises(FetcherError) as exc:
        f.ensure_ready()
    assert exc.value.code == "not_ready" and "no GC session" in str(exc.value)


def test_client_unreachable():
    def boom(request):
        raise httpx.ConnectError("refused")

    f = Fetcher("http://fetcher", client=httpx.Client(base_url="http://fetcher", transport=httpx.MockTransport(boom)))
    with pytest.raises(FetcherError) as exc:
        f.ensure_ready()
    assert exc.value.code == "unreachable"


def test_client_history_returns_matches():
    f = client({"/history/5": (200, {"account_id": 5, "complete": True, "matches": [history_entry(1)]})})
    assert json.loads(json.dumps(f.history(5)))[0]["match_id"] == 1


def test_client_friend_action_posts():
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        return httpx.Response(200, json={"logged_in": True, "incoming": [], "friends": [], "blocked": []})

    f = Fetcher(
        "http://fetcher", client=httpx.Client(base_url="http://fetcher", transport=httpx.MockTransport(handler))
    )
    f.friend_action(42, "block")
    assert seen == {"method": "POST", "path": "/friends/42/block"}


def test_not_scored_matches_are_kept_out_of_games_and_win_rate(conn, fetcher_match):
    from app import queries

    track(conn)
    fetcher = FakeFetcher(
        history=[
            history_entry(3, won=True),
            {**history_entry(2, won=True), "player_match_outcome": 5},  # someone left: not scored
            history_entry(1, won=False),
        ],
        errors={i: FetcherError("rate_limited", "slow down") for i in (1, 2, 3)},
    )
    jobs.sync_account(conn, FakeApi(), ME, settings(), fetcher=fetcher)
    me = queries.accounts(conn)[0]
    assert (me["games"], me["wins"], me["not_scored"]) == (2, 1, 1)
    assert [h["games"] for h in queries.hero_summary(conn, ME)] == [2]

    # the match file's flag wins once it's downloaded
    from app.db import transaction

    with transaction(conn):
        jobs.store_match(conn, {**fetcher_match, "match_id": 2, "not_scored": False})
    assert queries.accounts(conn)[0]["not_scored"] == 0
