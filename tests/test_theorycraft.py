import copy
import json
import re
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app import db, gamedata, jobs
from app.config import Settings
from app.main import create_app

from .conftest import FakeApi

FIXTURES = Path(__file__).parent / "fixtures"
GAME = json.loads((FIXTURES / "gamedata.json").read_text())
BUILDS = json.loads((FIXTURES / "builds_haze.json").read_text())
HAZE = 13
EXTRA_HEALTH = next(i["id"] for i in GAME["items"] if i["name"] == "Extra Health")
FORTITUDE = next(i["id"] for i in GAME["items"] if i["name"] == "Fortitude")
TOXIC = next(i["id"] for i in GAME["items"] if i["name"] == "Toxic Bullets")


class GameApi(FakeApi):
    def __init__(self, version=6848, game=None, builds=None, fail_builds=False):
        super().__init__()
        self.version = version
        self.game = copy.deepcopy(game or GAME)
        self.builds_list = BUILDS if builds is None else builds
        self.fail_builds = fail_builds
        self.asset_calls = 0
        self.build_calls = []

    def steam_info(self):
        return {"client_version": self.version, "version_datetime": "2026-11-03T19:52:58"}

    def asset_items(self):
        self.asset_calls += 1
        return self.game["items"]

    def asset_heroes(self):
        return self.game["heroes"]

    def generic_data(self):
        return self.game["generic"]

    def patches(self):
        return [
            {"title": "11-03-2026", "pub_date": "2026-11-03T20:00:00Z", "link": "https://forums.playdeadlock.com/x"},
            {"title": "evil", "pub_date": "", "link": "javascript:alert(1)"},
        ]

    def _maybe_fail(self):
        if self.fail_builds:
            raise httpx.ConnectError("down")

    def builds(self, **filters):
        self._maybe_fail()
        self.build_calls.append(("search", filters))
        return self.builds_list

    def build(self, hero_id, build_id):
        self._maybe_fail()
        for b in self.builds_list:
            if b["hero_build"]["hero_build_id"] == build_id:
                return b
        raise httpx.HTTPStatusError("nope", request=httpx.Request("GET", "http://x"), response=httpx.Response(404))

    def builds_by_author(self, account_id):
        self._maybe_fail()
        self.build_calls.append(("live", account_id))
        older = copy.deepcopy(self.builds_list[0])
        older["hero_build"]["version"] -= 1
        older["hero_build"]["name"] = "old version"
        return [older, *self.builds_list]


def test_normalise_items_and_heroes():
    data = gamedata.normalise(GAME["items"], GAME["heroes"], GAME["generic"])
    eh = data["items_by_id"][EXTRA_HEALTH]
    assert eh["slot"] == "vitality" and eh["tier"] == 1 and eh["cost"] == 800
    assert any(s["stat"] == "MODIFIER_VALUE_HEALTH_MAX" and s["text"].startswith("+") for s in eh["stats"])
    assert FORTITUDE in eh["upgrades_to"]
    assert EXTRA_HEALTH in data["items_by_id"][FORTITUDE]["components"]
    toxic = data["items_by_id"][TOXIC]
    assert any(s["conditional"] and s["label"] == "Incoming Healing" for s in toxic["stats"])
    assert not any(s["label"] == "Cooldown" for s in toxic["stats"])  # zero values left out
    haze = data["heroes_by_id"][HAZE]
    assert [a["name"] for a in haze["abilities"]] == ["Sleep Dagger", "Smoke Bomb", "Fixation", "Bullet Dance"]
    assert len(haze["abilities"][0]["upgrades"]) == 3
    assert ("Magazine", "25") in haze["weapon"]
    assert haze["investment_rows"][0][0] == 800 and set(haze["investment_rows"][0][1]) == set(gamedata.SLOTS)
    planner = gamedata.planner_json(data, 6848)
    assert all(s["stat"] for i in planner["items"] for s in i["stats"])


def test_clean_html_keeps_valve_markup_only():
    out = gamedata.clean_html('a <span class="highlight">b</span><script>x()</script><img src=x onerror=y> <br>')
    assert out == 'a <span class="highlight">b</span>&lt;script&gt;x()&lt;/script&gt;&lt;img src=x onerror=y&gt; <br>'
    assert gamedata.clean_html('<span class="x" onclick="evil()">t</span>') == '<span class="x">t</span>'


def stored_version(conn):
    return conn.execute("SELECT client_version FROM game_data WHERE kind = 'items'").fetchone()[0]


def test_update_detector(conn, monkeypatch):
    clock = [1_800_000_000]
    monkeypatch.setattr(jobs.time, "time", lambda: clock[0])
    api = GameApi()
    assert jobs.refresh_game_data(conn, api) is True
    assert stored_version(conn) == 6848 and api.asset_calls == 1
    # same version: one small request, nothing downloaded
    assert jobs.refresh_game_data(conn, api) is False and api.asset_calls == 1
    # a new version, but deadlock-api hasn't rebuilt its copy yet: keep the old data, look again hourly
    api.version = 6849
    clock[0] += 900
    assert jobs.refresh_game_data(conn, api) is False and api.asset_calls == 2
    clock[0] += 900
    assert jobs.refresh_game_data(conn, api) is False and api.asset_calls == 2  # not an hour yet
    # the rebuilt data arrives
    api.game["items"][0]["name"] = "Renamed"
    clock[0] += 3600
    assert jobs.refresh_game_data(conn, api) is True
    assert stored_version(conn) == 6849
    rows = conn.execute("SELECT client_version, data_at FROM game_versions ORDER BY client_version").fetchall()
    assert [(r[0], r[1] is not None) for r in rows] == [(6848, True), (6849, True)]


def test_update_detector_accepts_unchanged_data_after_a_day(conn, monkeypatch):
    clock = [1_800_000_000]
    monkeypatch.setattr(jobs.time, "time", lambda: clock[0])
    api = GameApi()
    jobs.refresh_game_data(conn, api)
    api.version = 6850  # an update that didn't touch items or heroes
    jobs.refresh_game_data(conn, api)
    clock[0] += jobs.GAME_DATA_WAIT_S + 1
    assert jobs.refresh_game_data(conn, api) is True and stored_version(conn) == 6850


def make_client(tmp_path, api=None, with_data=True):
    s = Settings(database_path=str(tmp_path / "t.sqlite3"), run_scheduler=False)
    api = api or GameApi()
    c = TestClient(create_app(s, api=api))
    if with_data:
        conn = db.connect(s.database_path)
        jobs.refresh_game_data(conn, api)
        with db.transaction(conn):
            conn.execute("INSERT INTO accounts (account_id, added_at, persona_name) VALUES (42, 0, 'tester')")
        conn.close()
    return c, api


def test_game_pages(tmp_path):
    c, _ = make_client(tmp_path)
    for url in (
        "/items",
        "/items?slot=vitality&tier=1",
        "/items?q=health",
        f"/items/{FORTITUDE}",
        "/heroes",
        f"/heroes/{HAZE}",
        "/planner",
        f"/planner?hero={HAZE}&items={EXTRA_HEALTH},{FORTITUDE}",
        "/patches",
        "/me",
        "/manifest.webmanifest",
        "/sw.js",
    ):
        assert c.get(url).status_code == 200, url
    assert "Extra Health" in c.get(f"/items/{FORTITUDE}").text  # builds from
    assert "Toxic Bullets" not in c.get("/items?slot=vitality").text
    hero = c.get(f"/heroes/{HAZE}").text
    assert "Sleep Dagger" in hero and "Investment payoffs" in hero
    assert c.get("/items/1").status_code == 404 and c.get("/heroes/1").status_code == 404
    patches = c.get("/patches").text
    assert "6848" in patches and "11-03-2026" in patches and "javascript:" not in patches


def test_gamedata_json_and_etag(tmp_path):
    c, _ = make_client(tmp_path)
    r = c.get("/gamedata.json")
    assert r.status_code == 200 and r.json()["version"] == 6848
    assert c.get("/gamedata.json", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_without_game_data(tmp_path):
    c, _ = make_client(tmp_path, with_data=False)
    assert "hasn't been loaded yet" in c.get("/items").text
    assert c.get("/gamedata.json").status_code == 503
    assert c.get("/planner").status_code == 200


def test_planner_save_and_reopen(tmp_path):
    c, _ = make_client(tmp_path)
    c.cookies.set("me", "42")
    r = c.post(
        "/planner/save",
        data={"hero_id": HAZE, "items": f"{EXTRA_HEALTH},{FORTITUDE},999", "name": "Tanky"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    url = r.headers["location"]
    page = c.get(url).text
    assert "Tanky" in page
    state = json.loads(re.search(r'id="planner-state" type="application/json">(.*?)</script>', page).group(1))
    assert state == {"hero": HAZE, "items": [EXTRA_HEALTH, FORTITUDE]}  # unknown item dropped
    build_id = int(url.rsplit("/", 1)[1])
    c.post("/planner/save", data={"hero_id": HAZE, "items": "", "name": "Renamed", "build_id": build_id})
    assert "Renamed" in c.get(url).text
    assert "Renamed" in c.get("/players/42/builds").text  # listed as the player's
    c.post(f"/planner/{build_id}/delete")
    assert c.get(url).status_code == 404


def test_build_browser(tmp_path):
    c, api = make_client(tmp_path)
    hb = BUILDS[0]["hero_build"]
    page = c.get(f"/builds?hero={HAZE}").text
    assert hb["name"] in page
    detail = c.get(f"/builds/{HAZE}/{hb['hero_build_id']}").text
    assert "Open in planner" in detail
    planner = c.get(f"/planner?build={HAZE}/{hb['hero_build_id']}").text
    assert hb["name"] in planner
    assert c.get(f"/builds/{HAZE}/1").status_code == 404


def test_build_browser_when_deadlock_api_is_down(tmp_path):
    c, _ = make_client(tmp_path, api=GameApi(fail_builds=True))
    assert "answer: down" in c.get("/builds").text


def test_player_builds(tmp_path):
    c, api = make_client(tmp_path)
    assert BUILDS[0]["hero_build"]["name"] in c.get("/players/42/builds").text
    search = {"author_id": 42, "only_latest": "true", "limit": 200, "sort_by": "updated_at"}
    assert api.build_calls[-1] == ("search", search)
    # cached for a day: a second view doesn't ask again
    n = len(api.build_calls)
    c.get("/players/42/builds")
    assert len(api.build_calls) == n
    r = c.post("/players/42/builds/refresh", follow_redirects=False)
    assert r.status_code == 303 and api.build_calls[-1] == ("live", 42)
    page = c.get("/players/42/builds").text
    assert "old version" not in page and "Asked Steam" in page
    assert c.get("/players/7/builds").status_code == 404  # only tracked players


def test_pick_player(tmp_path):
    c, _ = make_client(tmp_path)
    r = c.get("/me?set=42&next=/planner", follow_redirects=False)
    assert r.headers["location"] == "/planner" and "me=42" in r.headers["set-cookie"]
    assert "tester" in c.get("/items").text  # shown in the nav
    assert c.get("/me?set=42&next=//evil.example", follow_redirects=False).headers["location"] == "/"
    r = c.get("/me?set=7", follow_redirects=False)  # not a tracked player: forgotten, not set
    assert "me=42" not in r.headers.get("set-cookie", "")
