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


def test_repull_stores_history_and_only_free_details(conn, fetcher_match):
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
        api_has={115701682},
    )
    api = FakeApi()
    assert jobs.sync_account(conn, api, ME, settings(), fetcher=fetcher) == 2
    # the bot's Steam budget is never touched by a sync: only deadlock-api's salts
    assert {steam for _, _, steam in fetcher.fetched} == {False} and api.calls == []
    assert fetcher.history_pages == [None]  # first time: the whole history
    detailed = dict(conn.execute("SELECT match_id, detailed FROM matches").fetchall())
    assert detailed == {115701682: 1, 115700000: 0}
    row = conn.execute("SELECT * FROM accounts").fetchone()
    assert row["last_source"] == "Steam bot" and row["last_error"] is None and row["pending_matches"] == 1

    assert jobs.sync_account(conn, api, ME, settings(), fetcher=fetcher) == 0
    assert fetcher.history_pages == [None, 1]  # afterwards: just the newest page


def test_opening_a_match_fetches_its_details_with_the_bot(conn, fetcher_match):
    track(conn)
    fetcher = FakeFetcher(history=[history_entry(115701682)], matches={115701682: fetcher_match})
    jobs.sync_account(conn, FakeApi(), ME, settings(), fetcher=fetcher)
    assert conn.execute("SELECT pending_matches FROM accounts").fetchone()[0] == 1

    jobs.fetch_match_details(conn, fetcher, 115701682)
    assert fetcher.fetched[-1] == (115701682, ME, True)
    assert conn.execute("SELECT COUNT(*) FROM match_players WHERE match_id = 115701682").fetchone()[0] == 12
    assert conn.execute("SELECT pending_matches FROM accounts").fetchone()[0] is None


def test_details_that_cant_be_fetched_now_leave_the_history_line(conn):
    track(conn)
    fetcher = FakeFetcher(
        history=[history_entry(115800000)],
        errors={115800000: FetcherError("quota", "the bot's daily budget for match salts is used up")},
    )
    jobs.sync_account(conn, FakeApi(), ME, settings(), fetcher=fetcher)
    with pytest.raises(FetcherError, match="budget"):
        jobs.fetch_match_details(conn, fetcher, 115800000)
    assert conn.execute("SELECT detailed FROM matches").fetchone()[0] == 0


def test_scheduled_pass_completes_matches_once_deadlock_api_has_them(conn, fetcher_match):
    track(conn)
    fetcher = FakeFetcher(history=[history_entry(115701682)], matches={115701682: fetcher_match})
    jobs.sync_account(conn, FakeApi(), ME, settings(), fetcher=fetcher)
    assert jobs.enrich_from_deadlock_api(conn, fetcher) == 0
    fetcher.api_has.add(115701682)
    assert jobs.enrich_from_deadlock_api(conn, fetcher) == 1
    assert {steam for _, _, steam in fetcher.fetched} == {False}
    assert conn.execute("SELECT pending_matches FROM accounts").fetchone()[0] is None


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
    fetcher.history = lambda account_id, pages=None: (_ for _ in ()).throw(
        FetcherError("quota", "the bot's daily budget for match history is used up")
    )
    api = FakeApi()
    assert jobs.sync_account(conn, api, ME, settings(), fetcher=fetcher) == 0
    assert api.calls == []  # waits for the budget instead of falling back to a partial view
    assert "budget" in conn.execute("SELECT last_error FROM accounts").fetchone()[0]


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
    fetcher.history = lambda account_id, pages=None: (_ for _ in ()).throw(FetcherError("private", "not friends?"))
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
