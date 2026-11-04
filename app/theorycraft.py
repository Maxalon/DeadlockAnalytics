"""Item and hero browser, build planner, in-game build browser and patch list.

Game data comes from the `game_data` table (see gamedata.py and jobs.refresh_game_data); builds
people made in the game come live from deadlock-api's mirror of Valve's build browser.
"""

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from urllib.parse import quote, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from . import builds, jobs, queries
from .api import DeadlockApi
from .db import transaction
from .fetcher import FetcherError
from .gamedata import INVESTMENT, SLOTS, GameData

log = logging.getLogger(__name__)
# A path on this site: segments of plain characters, no empty segment (so never //).
_LOCAL_PATH = re.compile(r"/(?:[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/?)?")
BUILD_SORTS = {
    "weekly_favorites": "Popular this week",
    "favorites": "Most favourited",
    "updated_at": "Recently updated",
}
# What the editor says when publishing fails, per fetcher error code.
PUBLISH_ERRORS = {
    "quota": (429, "The bot's daily budget for publishing builds is used up; try again tomorrow."),
    "not_ready": (503, "The Steam bot isn't connected to the game right now; try again later."),
    "unreachable": (502, "The Steam bot (fetcher) couldn't be reached; see its log."),
    "timeout": (504, "The game didn't answer; try again in a moment."),
    "conflict": (409, "The build changed in the game since it was published; use “Save as new”, then publish."),
    "bad_request": (400, "The game refused the build; check the name and description length."),
}
API_DOWN = "deadlock-api didn't answer; try again in a moment."
PLAYER_BUILDS_MAX_AGE_S = 86400
CACHE_S = 300


class TtlCache:
    """deadlock-api answers for a few minutes, so paging back and forth doesn't ask again."""

    def __init__(self, ttl: int = CACHE_S, size: int = 200):
        self.ttl, self.size = ttl, size
        self._data: dict = {}
        self._lock = threading.Lock()

    def get(self, key, fetch: Callable):
        now = time.time()
        with self._lock:
            hit = self._data.get(key)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        value = fetch()
        with self._lock:
            if len(self._data) >= self.size:
                self._data.clear()
            self._data[key] = (now, value)
        return value


def me_id(request: Request) -> int | None:
    value = request.cookies.get("me", "")
    return int(value) if value.isdigit() else None


def register(app: FastAPI, render: Callable, db: Callable, api: DeadlockApi, static_dir, fetcher=None) -> None:
    game = GameData()
    cache = TtlCache()

    def data_or_none():
        return game.get(db())

    def accounts_by_id():
        return {a["account_id"]: a for a in queries.accounts(db())}

    def page(request: Request, name: str, **ctx):
        ctx.setdefault("game", data_or_none())
        return render(request, name, **ctx)

    def safe_next(next_url: str) -> str:
        """Where to go after picking a player: a plain path on this site, nothing else (no other host,
        no scheme, no //, backslashes or control characters a browser might read as one)."""
        parts = urlsplit(next_url[:200])
        if parts.scheme or parts.netloc or not _LOCAL_PATH.fullmatch(parts.path):
            return "/"
        return parts.path

    # ---- who's this? -------------------------------------------------------------------------
    @app.get("/me", response_class=HTMLResponse)
    def pick_player(request: Request, set: int | None = None, next: str = "/"):
        if set is not None:
            resp = RedirectResponse(safe_next(next), status_code=303)
            account = accounts_by_id().get(set) if set else None
            if account:  # the tracked account's own id, from the database
                resp.set_cookie("me", str(int(account["account_id"])), max_age=365 * 86400, samesite="lax")
            else:
                resp.delete_cookie("me")
            return resp
        return page(request, "me.html", accounts=queries.accounts(db()), next=safe_next(next))

    # ---- game data ---------------------------------------------------------------------------
    @app.get("/items", response_class=HTMLResponse)
    def items_page(request: Request, slot: str = "", tier: int = 0, q: str = ""):
        data = data_or_none()
        items = data["items"] if data else []
        if slot in SLOTS:
            items = [i for i in items if i["slot"] == slot]
        if tier:
            items = [i for i in items if i["tier"] == tier]
        if q:
            ql = q.lower()
            items = [i for i in items if ql in i["name"].lower() or any(ql in s["label"].lower() for s in i["stats"])]
        return page(request, "items.html", items=items, slot=slot, tier=tier, q=q, slots=SLOTS)

    @app.get("/items/{item_id}", response_class=HTMLResponse)
    def item_page(request: Request, item_id: int):
        data = data_or_none()
        item = data and data["items_by_id"].get(item_id)
        if not item:
            raise HTTPException(404)
        return page(request, "item.html", item=item, by_id=data["items_by_id"])

    @app.get("/heroes", response_class=HTMLResponse)
    def heroes_page(request: Request):
        data = data_or_none()
        return page(request, "heroes.html", hero_list=data["heroes"] if data else [])

    @app.get("/heroes/{hero_id}", response_class=HTMLResponse)
    def hero_page(request: Request, hero_id: int):
        data = data_or_none()
        hero = data and data["heroes_by_id"].get(hero_id)
        if not hero:
            raise HTTPException(404)
        return page(request, "hero.html", hero=hero, investment=INVESTMENT)

    @app.get("/gamedata.json")
    def gamedata_json(request: Request):
        data = data_or_none()
        if not data:
            return JSONResponse({"error": "no game data yet"}, status_code=503)
        etag = data["etag"]
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return Response(
            data["planner_body"], media_type="application/json", headers={"ETag": etag, "Cache-Control": "no-cache"}
        )

    # ---- build editor --------------------------------------------------------------------------
    def saved_list(account_id: int | None):
        rows = db().execute(
            "SELECT * FROM saved_builds ORDER BY (account_id IS ?) DESC, updated_at DESC LIMIT 200", (account_id,)
        )
        return [dict(r) for r in rows]

    def saved_details(row) -> dict:
        if row["details"]:
            return json.loads(row["details"])
        return builds.from_item_list(json.loads(row["items"] or "[]"))

    def editor(request: Request, state: dict, saved: dict | None = None, source=None):
        data = data_or_none()
        if data and state.get("hero") not in data["heroes_by_id"]:
            state["hero"] = None
        return page(
            request,
            "planner.html",
            state=state,
            saved=saved,
            saved_builds=saved_list(me_id(request)),
            source=source,
            accounts=accounts_by_id(),
            publishing=fetcher is not None,
        )

    @app.get("/planner", response_class=HTMLResponse)
    def planner_page(request: Request, hero: int | None = None, items: str = "", build: str = ""):
        ids = [int(x) for x in items.split(",") if x.strip().isdigit()]
        details = builds.empty()
        if ids:
            details["mod_categories"][0]["mods"] = [{"ability_id": i} for i in ids]
        state = {"hero": hero, "name": "", "description": "", "details": details}
        source = None
        if build:  # "hero_id/build_id": start from an in-game build, everything copied
            try:
                hero_id, build_id = (int(x) for x in build.split("/", 1))
                b = cache.get(("build", hero_id, build_id), lambda: api.build(hero_id, build_id))
            except (ValueError, httpx.HTTPError) as exc:
                log.info("build %s not available: %s", build, exc)
                raise HTTPException(404, f"build {build} is not available") from exc
            hb = b.get("hero_build") or {}
            state = {
                "hero": hero_id,
                "name": hb.get("name") or "",
                "description": hb.get("description") or "",
                "details": builds.details_of(b),
            }
            source = hb
        return editor(request, state, source=source)

    @app.get("/planner/{build_id}", response_class=HTMLResponse)
    def saved_planner(request: Request, build_id: int):
        row = db().execute("SELECT * FROM saved_builds WHERE id = ?", (build_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        state = {
            "id": row["id"],
            "hero": row["hero_id"],
            "name": row["name"],
            "description": row["description"] or row["notes"] or "",
            "details": saved_details(row),
            "game_build_id": row["game_build_id"],
        }
        return editor(request, state, saved=dict(row))

    def cleaned(payload: dict) -> tuple[int, str, str, dict]:
        data = data_or_none()
        if not data:
            raise HTTPException(503, "no game data yet")
        try:
            hero_id = int(payload.get("hero_id"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "pick a hero") from exc
        hero = data["heroes_by_id"].get(hero_id)
        if not hero:
            raise HTTPException(400, "unknown hero")
        details = builds.clean_details(
            payload.get("details") or {}, set(data["items_by_id"]), {a["id"] for a in hero["abilities"]}
        )
        name = str(payload.get("name") or "").strip()[: builds.MAX_NAME] or f"{hero['name']} build"
        description = str(payload.get("description") or "").strip()[: builds.MAX_DESCRIPTION]
        return hero_id, name, description, details

    @app.post("/api/builds")
    async def save_build(request: Request):
        payload = await request.json()
        hero_id, name, description, details = cleaned(payload)
        data = data_or_none()
        now = int(time.time())
        build_id = payload.get("id") if isinstance(payload.get("id"), int) else None
        items_json = json.dumps(builds.all_item_ids(details))
        with transaction(db()) as conn:
            exists = build_id and conn.execute("SELECT 1 FROM saved_builds WHERE id = ?", (build_id,)).fetchone()
            if exists and not payload.get("as_new"):
                conn.execute(
                    """UPDATE saved_builds SET hero_id = ?, name = ?, description = ?, details = ?, items = ?,
                       client_version = ?, updated_at = ? WHERE id = ?""",
                    (
                        hero_id,
                        name,
                        description,
                        json.dumps(details),
                        items_json,
                        data["client_version"],
                        now,
                        build_id,
                    ),
                )
            else:
                build_id = conn.execute(
                    """INSERT INTO saved_builds (account_id, hero_id, name, description, details, items, client_version,
                       created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        me_id(request),
                        hero_id,
                        name,
                        description,
                        json.dumps(details),
                        items_json,
                        data["client_version"],
                        now,
                        now,
                    ),
                ).lastrowid
        return {"id": build_id, "url": f"/planner/{build_id}", "name": name}

    @app.post("/api/builds/{build_id}/publish")
    def publish_build(build_id: int):
        """Publish a saved build to the game's build browser through the Steam bot. The first time
        creates it; later times update the same in-game build."""
        if fetcher is None:
            raise HTTPException(404, "publishing needs the Steam bot (fetcher)")
        row = db().execute("SELECT * FROM saved_builds WHERE id = ?", (build_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        body = {
            "hero_id": row["hero_id"],
            "name": row["name"],
            "description": row["description"] or "",
            "details": saved_details(row),
        }
        if row["game_build_id"]:
            body["hero_build_id"] = row["game_build_id"]
            body["version"] = row["game_version"] or 0
        try:
            result = fetcher.publish_build(body)
        except FetcherError as exc:
            # The details stay in the log: the fetcher's messages can name internal hosts.
            log.warning("publishing saved build %s failed: %s (%s)", build_id, exc, exc.code)
            status, message = PUBLISH_ERRORS.get(exc.code, PUBLISH_ERRORS["unreachable"])
            return JSONResponse({"detail": message}, status_code=status)
        with transaction(db()) as conn:
            conn.execute(
                "UPDATE saved_builds SET game_build_id = ?, game_version = ? WHERE id = ?",
                (result["hero_build_id"], result["version"], build_id),
            )
        return {"hero_build_id": result["hero_build_id"], "version": result["version"]}

    @app.post("/planner/{build_id}/delete")
    def delete_build(build_id: int):
        with transaction(db()) as conn:
            conn.execute("DELETE FROM saved_builds WHERE id = ?", (build_id,))
        return RedirectResponse("/planner", status_code=303)

    # ---- in-game builds ----------------------------------------------------------------------
    def build_rows(found: list[dict]) -> list[dict]:
        out = []
        for b in found:
            hb = b.get("hero_build") or {}
            cats = builds.categories(builds.details_of(b))
            out.append(
                {
                    "hero_id": hb.get("hero_id"),
                    "id": hb.get("hero_build_id"),
                    "name": hb.get("name") or "(no name)",
                    "author": hb.get("author_account_id"),
                    "version": hb.get("version"),
                    "updated": hb.get("last_updated_timestamp"),
                    "published": hb.get("publish_timestamp"),
                    "favorites": b.get("num_favorites"),
                    "weekly": b.get("num_weekly_favorites"),
                    "n_items": sum(len(c["mods"]) for c in cats),
                    "preview": [m["id"] for c in cats for m in c["mods"]][:10],
                }
            )
        return out

    @app.get("/builds", response_class=HTMLResponse)
    def builds_page(
        request: Request, hero: int | None = None, q: str = "", sort: str = "weekly_favorites", start: int = 0
    ):
        data = data_or_none()
        hero_list = data["heroes"] if data else []
        me = me_id(request)
        if hero is None and not q.strip():
            mine = [b for b in saved_list(me) if me and b["account_id"] == me][:6]
            return page(request, "builds_heroes.html", hero_list=hero_list, mine=mine)
        sort = sort if sort in BUILD_SORTS else "weekly_favorites"
        params = {
            "hero_id": hero,
            "search_name": q.strip() or None,
            "sort_by": sort,
            "sort_direction": "desc",
            "only_latest": "true",
            "limit": 30,
            "start": max(0, start),
        }
        error = None
        try:
            found = cache.get(("builds", tuple(sorted(params.items()))), lambda: api.builds(**params))
        except httpx.HTTPError as exc:
            log.warning("build search failed: %s", exc)
            found, error = [], API_DOWN
        return page(
            request,
            "builds.html",
            builds=build_rows(found),
            hero=hero,
            hero_info=data["heroes_by_id"].get(hero) if data and hero else None,
            q=q,
            sort=sort,
            sorts=BUILD_SORTS,
            start=start,
            error=error,
            hero_list=hero_list,
            by_id=data["items_by_id"] if data else {},
        )

    @app.get("/builds/{hero_id}/{build_id}", response_class=HTMLResponse)
    def build_page(request: Request, hero_id: int, build_id: int):
        try:
            b = cache.get(("build", hero_id, build_id), lambda: api.build(hero_id, build_id))
        except httpx.HTTPStatusError as exc:
            missing = exc.response.status_code == 404
            raise HTTPException(404 if missing else 502, "build not found" if missing else API_DOWN) from exc
        except httpx.HTTPError as exc:
            log.warning("build %s/%s failed: %s", hero_id, build_id, exc)
            raise HTTPException(502, API_DOWN) from exc
        data = data_or_none()
        hero = data["heroes_by_id"].get(hero_id) if data else None
        details = builds.details_of(b)
        return page(
            request,
            "build.html",
            build=build_rows([b])[0],
            hero=hero,
            description=(b.get("hero_build") or {}).get("description") or "",
            categories=builds.categories(details),
            steps=builds.ability_steps(details, hero["abilities"]) if hero else [],
            by_id=data["items_by_id"] if data else {},
            accounts=accounts_by_id(),
        )

    # ---- a tracked player's own builds ---------------------------------------------------------
    @app.get("/players/{account_id}/builds", response_class=HTMLResponse)
    def player_builds(request: Request, account_id: int, error: str | None = None):
        account = accounts_by_id().get(account_id)
        if not account:
            raise HTTPException(404)
        row = db().execute("SELECT * FROM player_builds WHERE account_id = ?", (account_id,)).fetchone()
        if row is None or time.time() - row["fetched_at"] > PLAYER_BUILDS_MAX_AGE_S:
            try:
                jobs.refresh_player_builds(db(), api, account_id)
            except httpx.HTTPError as exc:
                log.warning("builds of %s failed: %s", account_id, exc)
                error = error or API_DOWN
            row = db().execute("SELECT * FROM player_builds WHERE account_id = ?", (account_id,)).fetchone()
        builds = build_rows(json.loads(row["json"])) if row else []
        builds.sort(key=lambda b: b["updated"] or 0, reverse=True)
        return page(
            request,
            "player_builds.html",
            account=account,
            builds=builds,
            fetched=row,
            error=error,
            saved=[b for b in saved_list(account_id) if b["account_id"] == account_id],
        )

    @app.post("/players/{account_id}/builds/refresh")
    def refresh_player_builds(account_id: int):
        account = accounts_by_id().get(account_id)
        if not account:
            raise HTTPException(404)
        page_url = f"/players/{int(account['account_id'])}/builds"  # from the database row
        try:
            jobs.refresh_player_builds(db(), api, account["account_id"], live=True)
        except httpx.HTTPError as exc:
            log.warning("live builds of %s failed: %s", account_id, exc)
            return RedirectResponse(f"{page_url}?error={quote(API_DOWN)}", status_code=303)
        return RedirectResponse(page_url, status_code=303)

    # ---- patches -----------------------------------------------------------------------------
    @app.get("/patches", response_class=HTMLResponse)
    def patches_page(request: Request):
        versions = [dict(r) for r in db().execute("SELECT * FROM game_versions ORDER BY client_version DESC LIMIT 50")]
        error = None
        try:
            notes = cache.get("patches", api.patches)
        except httpx.HTTPError as exc:
            log.warning("patch notes failed: %s", exc)
            notes, error = [], API_DOWN
        notes = [
            {"title": n.get("title"), "date": (n.get("pub_date") or "")[:10], "link": n.get("link")}
            for n in notes
            if str(n.get("link") or "").startswith("https://")
        ][:30]
        jobs_ = queries.job_runs(db())
        return page(request, "patches.html", versions=versions, notes=notes, error=error, check=jobs_.get("game_data"))

    # ---- installable on a phone ----------------------------------------------------------------
    @app.get("/manifest.webmanifest")
    def manifest():
        return JSONResponse(
            {
                "name": "Deadlock Analytics",
                "short_name": "Deadlock",
                "start_url": "/planner",
                "scope": "/",
                "display": "standalone",
                "background_color": "#0f0f0d",
                "theme_color": "#0f0f0d",
                "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
            },
            media_type="application/manifest+json",
        )

    @app.get("/sw.js")
    def service_worker():
        # Served from the root so it may handle every page.
        return FileResponse(static_dir / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})
