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


def history_entry(match_id, start_time=1791848958, game_mode=1):
    return {"match_id": match_id, "start_time": start_time, "game_mode": game_mode, "match_mode": 1}


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


def test_sync_through_bot_downloads_only_new_normal_matches(conn, fetcher_match):
    track(conn)
    other = {**fetcher_match, "match_id": 115700000}
    fetcher = FakeFetcher(
        history=[
            history_entry(115701682),
            history_entry(115700000),
            history_entry(115600000, game_mode=4),  # street brawl: not scored
            history_entry(100, start_time=1_700_000_000),  # before the backfill date
        ],
        matches={115701682: fetcher_match, 115700000: other},
    )
    api = FakeApi()
    assert jobs.sync_account(conn, api, ME, settings(), fetcher=fetcher) == 2
    assert fetcher.fetched == [115701682, 115700000] and api.calls == []  # newest first
    account = conn.execute("SELECT * FROM accounts").fetchone()
    assert account["last_source"] == "Steam bot" and account["last_error"] is None

    # already stored matches aren't downloaded again
    assert jobs.sync_account(conn, api, ME, settings(), fetcher=fetcher) == 0
    assert fetcher.fetched == [115701682, 115700000]


def test_unfinished_match_is_skipped_until_next_sync(conn, fetcher_match):
    track(conn)
    fetcher = FakeFetcher(
        history=[history_entry(115701682), history_entry(115800000)],
        matches={115701682: fetcher_match},
        errors={115800000: FetcherError("in_flight", "still being processed")},
    )
    assert jobs.sync_account(conn, FakeApi(), ME, settings(), fetcher=fetcher) == 1
    assert conn.execute("SELECT last_error FROM accounts").fetchone()[0] is None


def test_rate_limit_keeps_progress_and_the_next_sync_continues(conn, fetcher_match):
    track(conn)
    newer = {**fetcher_match, "match_id": 115800000}
    limited = FetcherError("rate_limited", "Steam rate-limited match metadata requests")
    fetcher = FakeFetcher(
        history=[history_entry(115800000), history_entry(115701682), history_entry(115600000)],
        matches={115800000: newer, 115701682: fetcher_match},
        errors={115701682: limited},
    )
    api = FakeApi()
    assert jobs.sync_account(conn, api, ME, settings(), fetcher=fetcher) == 1
    account = conn.execute("SELECT * FROM accounts").fetchone()
    assert account["pending_matches"] == 2 and account["last_error"] is None
    assert account["last_source"] == "Steam bot" and api.calls == []  # no deadlock-api fallback

    del fetcher.errors[115701682]
    fetcher.matches[115600000] = {**fetcher_match, "match_id": 115600000}
    assert jobs.sync_account(conn, api, ME, settings(), fetcher=fetcher) == 2
    assert fetcher.fetched[-2:] == [115701682, 115600000]  # only the ones still missing
    assert conn.execute("SELECT pending_matches FROM accounts").fetchone()[0] is None


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
