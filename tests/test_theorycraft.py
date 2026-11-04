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


def make_client(tmp_path, api=None, with_data=True, fetcher=None):
    s = Settings(database_path=str(tmp_path / "t.sqlite3"), run_scheduler=False)
    api = api or GameApi()
    c = TestClient(create_app(s, api=api, fetcher=fetcher))
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
    assert "Fortitude" in c.get(f"/items/{FORTITUDE}").text
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


def editor_state(page: str) -> dict:
    return json.loads(re.search(r'id="editor-state" type="application/json">(.*?)</script>', page, re.S).group(1))


def details(mods, order=()):
    return {
        "mod_categories": [
            {"name": "Early", "description": "lane", "optional": False, "mods": mods},
            {"name": "Late", "description": "", "optional": True, "mods": []},
        ],
        "ability_order": {"currency_changes": list(order)},
    }


def test_editor_save_and_reopen(tmp_path):
    c, _ = make_client(tmp_path)
    c.cookies.set("me", "42")
    dagger = next(a["id"] for a in gamedata.normalise(GAME["items"], GAME["heroes"])["heroes"][0]["abilities"])
    body = {
        "hero_id": HAZE,
        "name": "Tanky",
        "description": "be tanky",
        "details": details(
            [{"ability_id": EXTRA_HEALTH, "annotation": "first", "sell_priority": 7}, {"ability_id": 999}],
            [
                {"ability_id": dagger, "currency_type": 2, "delta": -1},
                {"ability_id": dagger, "currency_type": 1, "delta": -1},
                {"ability_id": dagger, "currency_type": 1, "delta": -5},
            ],  # T2 costs 2: dropped
        ),
    }
    r = c.post("/api/builds", json=body)
    assert r.status_code == 200
    url = r.json()["url"]
    state = editor_state(c.get(url).text)
    assert state["name"] == "Tanky" and state["description"] == "be tanky" and state["hero"] == HAZE
    cats = state["details"]["mod_categories"]
    assert [c_["name"] for c_ in cats] == ["Early", "Late"] and cats[1]["optional"] is True
    assert cats[0]["mods"] == [{"ability_id": EXTRA_HEALTH, "annotation": "first", "sell_priority": 7}]  # 999 dropped
    assert len(state["details"]["ability_order"]["currency_changes"]) == 2
    build_id = r.json()["id"]
    assert c.post("/api/builds", json={**body, "id": build_id, "name": "Renamed"}).json()["id"] == build_id
    assert "Renamed" in c.get("/players/42/builds").text  # listed as the player's
    assert c.post("/api/builds", json={**body, "id": build_id, "as_new": True}).json()["id"] != build_id
    assert c.post("/api/builds", json={**body, "hero_id": 1}).status_code == 400
    c.post(f"/planner/{build_id}/delete")
    assert c.get(url).status_code == 404


def test_old_saves_open_as_one_category(tmp_path):
    c, _ = make_client(tmp_path)
    conn = db.connect(str(tmp_path / "t.sqlite3"))
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO saved_builds (id, hero_id, name, items, notes, created_at, updated_at)"
            " VALUES (5, ?, 'old', ?, 'n', 0, 0)",
            (HAZE, json.dumps([EXTRA_HEALTH, FORTITUDE])),
        )
    conn.close()
    state = editor_state(c.get("/planner/5").text)
    assert state["description"] == "n"
    assert [m["ability_id"] for m in state["details"]["mod_categories"][0]["mods"]] == [EXTRA_HEALTH, FORTITUDE]


def test_build_browser(tmp_path):
    c, api = make_client(tmp_path)
    hb = BUILDS[0]["hero_build"]
    heroes = c.get("/builds").text
    assert f'/builds?hero={HAZE}"' in heroes and not api.build_calls  # pick a hero first
    page = c.get(f"/builds?hero={HAZE}").text
    assert hb["name"] in page
    detail = c.get(f"/builds/{HAZE}/{hb['hero_build_id']}").text
    assert "Open in editor" in detail and "Ability order" in detail and 'class="ab-step' in detail
    state = editor_state(c.get(f"/planner?build={HAZE}/{hb['hero_build_id']}").text)
    assert state["name"] == hb["name"] and state["details"] == hb["details"]  # copied as it is
    assert c.get(f"/builds/{HAZE}/1").status_code == 404


def test_build_browser_when_deadlock_api_is_down(tmp_path):
    c, _ = make_client(tmp_path, api=GameApi(fail_builds=True))
    page = c.get(f"/builds?hero={HAZE}").text
    assert "answer; try again" in page and "down" not in page


class PublishingFetcher:
    def __init__(self, error=None):
        self.published = []
        self.error = error

    def publish_build(self, build):
        from app.fetcher import FetcherError

        if self.error:
            raise FetcherError(*self.error)
        self.published.append(build)
        return {"hero_build_id": 900, "version": len(self.published)}


def test_publish_to_game(tmp_path):
    bot = PublishingFetcher()
    c, _ = make_client(tmp_path, fetcher=bot)
    build_id = c.post(
        "/api/builds", json={"hero_id": HAZE, "name": "Pub", "details": details([{"ability_id": TOXIC}])}
    ).json()["id"]
    assert "Publish to game" in c.get(f"/planner/{build_id}").text
    r = c.post(f"/api/builds/{build_id}/publish")
    assert r.json() == {"hero_build_id": 900, "version": 1}
    first = bot.published[0]
    assert first["hero_id"] == HAZE and first["name"] == "Pub" and "hero_build_id" not in first
    assert first["details"]["mod_categories"][0]["mods"] == [{"ability_id": TOXIC}]
    c.post(f"/api/builds/{build_id}/publish")  # again: updates the same in-game build
    assert bot.published[1]["hero_build_id"] == 900 and bot.published[1]["version"] == 1
    assert editor_state(c.get(f"/planner/{build_id}").text)["game_build_id"] == 900

    refusing, _ = make_client(tmp_path / "b", fetcher=PublishingFetcher(error=("quota", "used up")))
    bid = refusing.post("/api/builds", json={"hero_id": HAZE, "name": "x", "details": details([])}).json()["id"]
    r = refusing.post(f"/api/builds/{bid}/publish")
    assert r.status_code == 429 and "used up" in r.json()["detail"]


def test_publishing_needs_the_bot(tmp_path):
    c, _ = make_client(tmp_path)
    build_id = c.post("/api/builds", json={"hero_id": HAZE, "name": "x", "details": details([])}).json()["id"]
    assert "Publish to game" not in c.get(f"/planner/{build_id}").text
    assert c.post(f"/api/builds/{build_id}/publish").status_code == 404


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
    evils = ("//evil.example", "/\t/evil.example", "/\\evil.example", "https://evil.example/x", "/a//b", "javascript:x")
    for evil in evils:
        assert c.get("/me", params={"set": 42, "next": evil}, follow_redirects=False).headers["location"] == "/", evil
    for ok in ("/", "/heroes/13", "/players/42/builds", "/planner/"):
        assert c.get("/me", params={"set": 42, "next": ok}, follow_redirects=False).headers["location"] == ok
    r = c.get("/me?set=7", follow_redirects=False)  # not a tracked player: forgotten, not set
    assert "me=42" not in r.headers.get("set-cookie", "")


def test_builds_format():
    from app import builds

    a, b = 11, 22
    order = [
        {"ability_id": a, "currency_type": 2, "delta": -1},
        {"ability_id": a, "currency_type": 1, "delta": -1},
        {"ability_id": a, "currency_type": 1, "delta": -2},
        {"ability_id": b, "currency_type": 1, "delta": -1},  # not unlocked yet: stops here
        {"ability_id": b, "currency_type": 2, "delta": -1},
    ]
    assert len(builds.valid_ability_order(order, {a, b})) == 3
    steps = builds.ability_steps({"ability_order": {"currency_changes": order[:3]}}, [{"id": a, "name": "A"}])
    assert [(s["unlock"], s["tier"]) for s in steps] == [(True, 0), (False, 1), (False, 2)]
    clean = builds.clean_details(
        {
            "mod_categories": [
                {"name": "x" * 100, "mods": [{"ability_id": 5, "annotation": " hi ", "sell_priority": 500}]}
            ]
        },
        {5},
        set(),
    )
    cat = clean["mod_categories"][0]
    assert len(cat["name"]) == builds.MAX_NAME and cat["mods"] == [{"ability_id": 5, "annotation": "hi"}]


def test_tooltips_and_new_heroes():
    violet = {**copy.deepcopy(GAME["heroes"][0]), "id": 86, "name": "Violet", "in_development": True}
    data = gamedata.normalise(GAME["items"], GAME["heroes"] + [violet])
    assert data["heroes_by_id"][86]["new"] is True and not data["heroes_by_id"][HAZE]["new"]
    toxic = data["items_by_id"][TOXIC]["tooltip"]
    assert toxic[0]["type"] == "passive" and "Bleed" in toxic[0]["text"]
    assert {p["label"] for p in toxic[0]["big"]} >= {"Bleed Damage", "Incoming Healing"}
    eh = data["items_by_id"][EXTRA_HEALTH]["tooltip"]
    assert eh[0]["type"] == "innate" and eh[0]["big"][0]["value"].startswith("+")
    dagger = data["heroes_by_id"][HAZE]["abilities"][0]
    assert dagger["header"]["cooldown"].endswith("s")
    assert any(p["label"] == "Damage" for p in dagger["tooltip"][0]["big"])
    assert data["heroes_by_id"][HAZE]["panel"]["weapon"][0][0] == "DPS"


def test_clean_html_inline_icons_and_placeholders():
    cdn = "https://assets-bucket.deadlock-api.com/a/b.png"
    out = gamedata.clean_html(
        f'Empower ( a b i l i t y1 ): <Panel class="x"><img src="{cdn}" class="i"/></Panel>'
        '<img src="https://evil.example/x.png"><svg viewBox="0 0 1 1"><path d="M0"/></svg>{s:hero_name}'
    )
    expected = (
        f'Empower (Ability 1): <img class="inline-icon" src="{cdn}" alt="">&lt;img src="https://evil.example/x.png"&gt;'
    )
    assert out == expected
