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


def test_editor_offers_the_export(tmp_path):
    c, _ = make_client(tmp_path)
    page = c.get("/planner").text
    assert 'id="ed-export"' in page
    # Loaded after the editor itself, whose state it reads.
    scripts = re.findall(r'<script src="(/static/(\w+)\.js\?v=\w+)">', page)
    assert [name for _, name in scripts] == ["ui", "planner", "export"]
    js = c.get(scripts[-1][0])
    assert js.status_code == 200 and "DL.editor" in js.text


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


def test_tooltips_and_released_heroes():
    violet = {**copy.deepcopy(GAME["heroes"][0]), "id": 86, "name": "Violet", "in_development": True}
    data = gamedata.normalise(GAME["items"], GAME["heroes"] + [violet])
    assert 86 in data["heroes_by_id"]  # released, though Valve still flags it in_development
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


def test_gamedata_etag_follows_the_content(conn, monkeypatch):
    # Same game data, but the code that shapes it changed (e.g. a hero filter fix): phones must
    # get the new JSON instead of a 304 for their old copy.
    jobs.refresh_game_data(conn, GameApi())
    before = gamedata.GameData().get(conn)
    assert gamedata.GameData().get(conn)["etag"] == before["etag"]  # stable while nothing changes
    original = gamedata.planner_json
    monkeypatch.setattr(gamedata, "planner_json", lambda d, v: {**original(d, v), "extra": 1})
    assert gamedata.GameData().get(conn)["etag"] != before["etag"]


def test_scaling_and_distances():
    data = gamedata.normalise(GAME["items"], GAME["heroes"], GAME["generic"])
    dagger = next(a for a in data["heroes_by_id"][HAZE]["abilities"] if a["name"] == "Sleep Dagger")
    props = {p["key"]: p for s in dagger["tooltip"] for p in s["big"] + s["lines"]}
    # The game's "x2.2" on the damage: what each point of spirit power adds.
    assert props["Damage"]["value"] == "65"
    assert props["Damage"]["scale"] == {"stat": "spirit", "per": 2.2, "text": "2.2"}
    assert props["Damage"]["calc"] == {"base": 65.0, "sign": "", "postfix": "", "mods": {}, "dp": 1}
    # A build's ability duration bonus applies to durations, whatever else they scale with.
    assert props["SleepDuration"]["calc"]["mods"] == {"duration": 1.0} and "scale" not in props["SleepDuration"]
    assert props["SleepWakeUpDelay"]["scale"]["stat"] == "spirit"
    assert props["SleepWakeUpDelay"]["calc"]["mods"] == {"duration": 1.0}
    fn = lambda **sf: gamedata.build_mods({"scale_function": sf})  # noqa: E731
    assert fn(class_name="scale_function_tech_range") == {"range": 1.0}
    assert fn(class_name="scale_function_single_stat", specific_stat_scale_type="ETechRadius") == {"radius": 1.0}
    assert fn(class_name="scale_function_single_stat", specific_stat_scale_type="ETechDuration", stat_scale=1.1) == {
        "duration": 1.1
    }
    assert fn(class_name="scale_function_single_stat", specific_stat_scale_type="ETechCooldown") == {}
    # Which bonuses apply to a value (duration, range, cooldown) isn't scaling, nor is a zero factor.
    assert (
        gamedata.scaling({"scale_function": {"class_name": "scale_function_tech_duration", "stat_scale": 1.0}}) is None
    )
    assert (
        gamedata.scaling(
            {
                "scale_function": {
                    "class_name": "scale_function_single_stat",
                    "specific_stat_scale_type": "ETechCooldown",
                    "stat_scale": -1.0,
                }
            }
        )
        is None
    )
    fixation = {
        "class_name": "scale_function_ability_weapon_damage",
        "specific_stat_scale_type": "EBaseWeaponDamageIncrease",
        "stat_scale": 0.0,
    }
    assert gamedata.scaling({"scale_function": fixation}) is None
    assert gamedata.scaling({"scale_function": {**fixation, "stat_scale": 0.7}})["stat"] == "weapon"
    heal = {"class_name": "scale_function_healing_boon_scale", "stat_scale": 0.06}
    assert gamedata.scaling({"scale_function": heal}) == {"stat": "boon", "per": 0.06, "text": "0.06"}

    # Distances come with their unit in the value; a value that is only scaling still shows.
    props = {
        "Range": {"value": "14m", "label": "Range", "postfix": "m"},
        "Radius": {"value": "0m", "label": "Radius"},
        "Heal": {
            "value": "0",
            "label": "Heal",
            "scale_function": {"class_name": "scale_function_healing_spirit_scale", "stat_scale": 0.6},
        },
    }
    assert gamedata.prop(props, "Range")["value"] == "14m" and "calc" not in gamedata.prop(props, "Range")
    assert gamedata.prop(props, "Radius") is None
    assert gamedata.prop(props, "Heal")["value"] == "0" and gamedata.prop(props, "Heal")["scale"]["text"] == "0.6"

    # Without Valve's text for an upgrade, its changes are listed; one to scaling says so.
    ability = {
        "id": 1,
        "name": "Keg",
        "properties": {"Impact": {"value": 40.0, "label": "Impact Damage", "scale_function": heal}},
        "upgrades": [
            {
                "property_upgrades": [
                    {"name": "Impact", "bonus": 0.25, "scale_stat_filter": "ETechPower", "upgrade_type": "EAddToScale"},
                    {"name": "Impact", "bonus": "2m"},
                ]
            }
        ],
    }
    up = gamedata._ability(ability)["upgrades"][0]
    assert up["bonuses"] == ["+0.25 Impact Damage Scaling", "+2m Impact Damage"]
    assert up["text"] == (
        '<span class="highlight">+0.25</span> Impact Damage Scaling<br><span class="highlight">+2m</span> Impact Damage'
    )


def test_numbers_as_the_game_prints_them():
    # At most one decimal, half up, no trailing zeros; multipliers to two, the very small to three.
    assert [gamedata._round(v, 1) for v in (13.3333, 6.36, 6.48, 2.75, 40.0, -10.0)] == [
        "13.3",
        "6.4",
        "6.5",
        "2.8",
        "40",
        "-10",
    ]
    assert [gamedata._coef(v) for v in (0.609336, 0.525, 0.015, 2.2, 0.002)] == ["0.61", "0.53", "0.02", "2.2", "0.002"]
    props = {
        "DPS": {"value": "13.3333", "label": "Damage Per Second"},
        # A stat the item gives the hero is typed in the game's data and keeps two decimals.
        "Sprint": {
            "value": "0.75",
            "label": "Sprint Speed",
            "postfix": "m/s",
            "prefix": "{s:sign}",
            "provided_property_type": "MODIFIER_VALUE_SPRINT_SPEED",
        },
        # A distance without "m" is in engine units.
        "Range": {"value": "472.441", "label": "Cast Range", "postfix": "m", "display_units": "EDisplayUnit_Meters"},
    }
    assert gamedata.prop(props, "DPS")["value"] == "13.3"
    assert gamedata.prop(props, "Sprint")["value"] == "+0.75m/s"
    assert gamedata.prop(props, "Range")["value"] == "12m"
    # Prose keeps the numbers Valve wrote, without the data's artifacts.
    text = gamedata.clean_html('<span class="highlight">+13.3333</span> DPS, -10.0% for 6.0s, 0.25s, 49.5')
    assert text == '<span class="highlight">+13.3</span> DPS, -10% for 6s, 0.25s, 49.5'


def test_imbue_targets_and_item_tiles(tmp_path):
    """The game's "Imbue Target": the hero ability a build suggests for an imbue item."""
    from app import builds

    game = copy.deepcopy(GAME)
    next(i for i in game["items"] if i["id"] == TOXIC)["imbue"] = "imbue_active_non_ult"
    abilities = gamedata.normalise(game["items"], game["heroes"])["heroes"][0]["abilities"]
    dagger = next(a["id"] for a in abilities if not a["ultimate"])
    ultimate = next(a["id"] for a in abilities if a["ultimate"])
    found = copy.deepcopy(BUILDS)
    hb = found[0]["hero_build"]
    mods = [m for cat in hb["details"]["mod_categories"] for m in cat["mods"]]
    next(m for m in mods if m["ability_id"] == TOXIC)["imbue_target_ability_id"] = dagger
    c, _ = make_client(tmp_path, api=GameApi(game=game, builds=found))

    # A published build shows the suggestion under the item, as the game's tooltip names it.
    page = c.get(f"/builds/{HAZE}/{hb['hero_build_id']}").text
    assert "Imbue Suggestion: Sleep Dagger" in page
    # As on the game's shop card: imbue items say so, actives are round and carry no tag.
    assert '<span class="tile-imbue">Imbue</span>' in page and "tile-active" not in page
    data = c.get("/gamedata.json").json()
    assert next(i for i in data["items"] if i["id"] == TOXIC)["imbue"] == "imbue_active_non_ult"
    assert next(i for i in data["items"] if i["id"] == EXTRA_HEALTH)["imbue"] == ""

    # Saved from the editor: kept on an imbue item for an ability it can take, dropped otherwise.
    def saved(target, item=TOXIC):
        mod = {"ability_id": item, "imbue_target_ability_id": target}
        r = c.post("/api/builds", json={"hero_id": HAZE, "name": "x", "details": details([mod])})
        return editor_state(c.get(r.json()["url"]).text)["details"]["mod_categories"][0]["mods"][0]

    assert saved(dagger) == {"ability_id": TOXIC, "imbue_target_ability_id": dagger}
    assert saved(ultimate) == {"ability_id": TOXIC}  # this item can't imbue an ultimate
    assert saved(12345) == {"ability_id": TOXIC}  # not one of the hero's abilities
    assert saved(dagger, EXTRA_HEALTH) == {"ability_id": EXTRA_HEALTH}  # not an imbue item
    cats = builds.categories({"mod_categories": [{"mods": [{"ability_id": TOXIC, "imbue_target_ability_id": dagger}]}]})
    assert cats[0]["mods"][0]["imbue"] == dagger


def test_signs_as_the_game_writes_them():
    """The game data says what stands before a number; a slow is written 40 and shown -40%."""
    props = {
        "Slow": {
            "value": "40",
            "label": "Move Speed",
            "postfix": "%",
            "prefix": "-",
            "provided_property_type": "MODIFIER_VALUE_MOVEMENT_SPEED_SLOW_PERCENT",
        },
        "Speed": {
            "value": "4m",
            "label": "Move Speed",
            "postfix": " m",
            "prefix": "{s:sign}",
            "provided_property_type": "MODIFIER_VALUE_MOVEMENT_SPEED_MAX",
        },
        "Shred": {"value": "-10", "label": "Bullet Resist", "postfix": "%", "prefix": "{s:sign}"},
        "Amp": {"value": "7", "label": "Damage Amplification", "postfix": "%", "prefix": "+"},
        # Given to the hero, but the game writes no sign for it.
        "Reduction": {
            "value": "15",
            "label": "Damage Reduction",
            "postfix": "%",
            "provided_property_type": "MODIFIER_VALUE_ALL_DAMAGE_TAKEN_REDUCTION",
        },
    }
    shown = {k: gamedata.prop(props, k)["value"] for k in props}
    assert shown == {"Slow": "-40%", "Speed": "+4 m", "Shred": "-10%", "Amp": "+7%", "Reduction": "15%"}
    # Summed into a build's stats, a slow counts as a reduction.
    assert {s["key"]: s["value"] for s in gamedata.item_stats(props)}["Slow"] == -40
