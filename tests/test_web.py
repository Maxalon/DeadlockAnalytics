from fastapi.testclient import TestClient

from app import jobs
from app.config import Settings
from app.main import create_app

from .conftest import FakeApi


def client(tmp_path, **kw):
    s = Settings(database_path=str(tmp_path / "t.sqlite3"), run_scheduler=False, **kw)
    return TestClient(create_app(s, api=FakeApi())), s


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
    assert c.get("/theme?to=dark&next=//evil.example", follow_redirects=False).headers["location"] == "/"
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
