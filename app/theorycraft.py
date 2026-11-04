"""Item and hero browser, build planner, in-game build browser and patch list.

Game data comes from the `game_data` table (see gamedata.py and jobs.refresh_game_data); builds
people made in the game come live from deadlock-api's mirror of Valve's build browser.
"""

import json
import logging
import threading
import time
from collections.abc import Callable
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from . import jobs, queries
from .api import DeadlockApi
from .db import transaction
from .gamedata import INVESTMENT, SLOTS, GameData

log = logging.getLogger(__name__)
BUILD_SORTS = {
    "weekly_favorites": "Popular this week",
    "favorites": "Most favourited",
    "updated_at": "Recently updated",
}
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


def build_items(build: dict) -> list[dict]:
    """A build's categories with the mod (item) ids in each, in the author's order."""
    details = (build.get("hero_build") or {}).get("details") or {}
    return [
        {
            "name": c.get("name") or "",
            "description": c.get("description") or "",
            "mods": [{"id": m.get("ability_id"), "note": m.get("annotation")} for m in c.get("mods") or []],
        }
        for c in details.get("mod_categories") or []
    ]


def planner_items_from_build(build: dict, items_by_id: dict) -> list[int]:
    """Shop items of a build in its order, each once (abilities and unknown ids left out)."""
    seen, out = set(), []
    for cat in build_items(build):
        for m in cat["mods"]:
            if m["id"] in items_by_id and m["id"] not in seen:
                seen.add(m["id"])
                out.append(m["id"])
    return out


def register(app: FastAPI, render: Callable, db: Callable, api: DeadlockApi, static_dir) -> None:
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
        return next_url if next_url.startswith("/") and not next_url.startswith("//") and "\\" not in next_url else "/"

    # ---- who's this? -------------------------------------------------------------------------
    @app.get("/me", response_class=HTMLResponse)
    def pick_player(request: Request, set: int | None = None, next: str = "/"):
        if set is not None:
            resp = RedirectResponse(safe_next(next), status_code=303)
            if set and set in accounts_by_id():
                resp.set_cookie("me", str(set), max_age=365 * 86400, samesite="lax")
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
        etag = f'"{data["client_version"]}-{data["fetched_at"]}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(data["planner"], headers={"ETag": etag, "Cache-Control": "no-cache"})

    # ---- planner -----------------------------------------------------------------------------
    def saved_list(account_id: int | None):
        rows = db().execute(
            "SELECT * FROM saved_builds ORDER BY (account_id IS ?) DESC, updated_at DESC LIMIT 200", (account_id,)
        )
        return [dict(r) for r in rows]

    def planner(request: Request, hero_id: int | None, items: list[int], saved: dict | None = None, source=None):
        data = data_or_none()
        if data:
            items = [i for i in items if i in data["items_by_id"]]
            if hero_id not in data["heroes_by_id"]:
                hero_id = None
        return page(
            request,
            "planner.html",
            state={"hero": hero_id, "items": items},
            saved=saved,
            saved_builds=saved_list(me_id(request)),
            source=source,
            accounts=accounts_by_id(),
        )

    @app.get("/planner", response_class=HTMLResponse)
    def planner_page(request: Request, hero: int | None = None, items: str = "", build: str = ""):
        ids = [int(x) for x in items.split(",") if x.strip().isdigit()]
        source = None
        if build:  # "hero_id/build_id": start from an in-game build
            try:
                hero_id, build_id = (int(x) for x in build.split("/", 1))
                b = cache.get(("build", hero_id, build_id), lambda: api.build(hero_id, build_id))
            except (ValueError, httpx.HTTPError) as exc:
                raise HTTPException(404, f"build {build} not available: {exc}") from exc
            data = data_or_none()
            ids = planner_items_from_build(b, data["items_by_id"] if data else {})
            hero = hero_id
            source = b
        return planner(request, hero, ids, source=source)

    @app.get("/planner/{build_id}", response_class=HTMLResponse)
    def saved_planner(request: Request, build_id: int):
        row = db().execute("SELECT * FROM saved_builds WHERE id = ?", (build_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        return planner(request, row["hero_id"], json.loads(row["items"]), saved=dict(row))

    @app.post("/planner/save")
    def save_build(
        request: Request,
        hero_id: int = Form(...),
        items: str = Form(""),
        name: str = Form(""),
        notes: str = Form(""),
        build_id: int | None = Form(None),
        as_new: str = Form(""),
    ):
        ids = [int(x) for x in items.split(",") if x.strip().isdigit()][:60]
        data = data_or_none()
        now = int(time.time())
        name = name.strip()[:80] or "Untitled build"
        version = data["client_version"] if data else None
        with transaction(db()) as conn:
            exists = build_id and conn.execute("SELECT 1 FROM saved_builds WHERE id = ?", (build_id,)).fetchone()
            if exists and not as_new:
                conn.execute(
                    """UPDATE saved_builds SET hero_id = ?, name = ?, items = ?, notes = ?, client_version = ?,
                       updated_at = ? WHERE id = ?""",
                    (hero_id, name, json.dumps(ids), notes[:2000], version, now, build_id),
                )
            else:
                build_id = conn.execute(
                    """INSERT INTO saved_builds (account_id, hero_id, name, items, notes, client_version, created_at,
                       updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (me_id(request), hero_id, name, json.dumps(ids), notes[:2000], version, now, now),
                ).lastrowid
        return RedirectResponse(f"/planner/{build_id}", status_code=303)

    @app.post("/planner/{build_id}/delete")
    def delete_build(build_id: int):
        with transaction(db()) as conn:
            conn.execute("DELETE FROM saved_builds WHERE id = ?", (build_id,))
        return RedirectResponse("/planner", status_code=303)

    # ---- in-game builds ----------------------------------------------------------------------
    def build_rows(builds: list[dict]) -> list[dict]:
        out = []
        for b in builds:
            hb = b.get("hero_build") or {}
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
                    "n_items": sum(len(c["mods"]) for c in build_items(b)),
                }
            )
        return out

    @app.get("/builds", response_class=HTMLResponse)
    def builds_page(
        request: Request, hero: int | None = None, q: str = "", sort: str = "weekly_favorites", start: int = 0
    ):
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
            builds = cache.get(("builds", tuple(sorted(params.items()))), lambda: api.builds(**params))
        except httpx.HTTPError as exc:
            builds, error = [], f"deadlock-api didn't answer: {exc}"
        data = data_or_none()
        return page(
            request,
            "builds.html",
            builds=build_rows(builds),
            hero=hero,
            q=q,
            sort=sort,
            sorts=BUILD_SORTS,
            start=start,
            error=error,
            hero_list=data["heroes"] if data else [],
        )

    @app.get("/builds/{hero_id}/{build_id}", response_class=HTMLResponse)
    def build_page(request: Request, hero_id: int, build_id: int):
        try:
            b = cache.get(("build", hero_id, build_id), lambda: api.build(hero_id, build_id))
        except httpx.HTTPStatusError as exc:
            raise HTTPException(404 if exc.response.status_code == 404 else 502, str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, str(exc)) from exc
        data = data_or_none()
        return page(
            request,
            "build.html",
            build=build_rows([b])[0],
            description=(b.get("hero_build") or {}).get("description") or "",
            categories=build_items(b),
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
                error = error or f"deadlock-api didn't answer: {exc}"
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
        if account_id not in accounts_by_id():
            raise HTTPException(404)
        try:
            jobs.refresh_player_builds(db(), api, account_id, live=True)
        except httpx.HTTPError as exc:
            return RedirectResponse(f"/players/{account_id}/builds?error={quote(str(exc)[:200])}", status_code=303)
        return RedirectResponse(f"/players/{account_id}/builds", status_code=303)

    # ---- patches -----------------------------------------------------------------------------
    @app.get("/patches", response_class=HTMLResponse)
    def patches_page(request: Request):
        versions = [dict(r) for r in db().execute("SELECT * FROM game_versions ORDER BY client_version DESC LIMIT 50")]
        error = None
        try:
            notes = cache.get("patches", api.patches)
        except httpx.HTTPError as exc:
            notes, error = [], f"deadlock-api didn't answer: {exc}"
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
                "background_color": "#121214",
                "theme_color": "#1b1b1f",
                "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
            },
            media_type="application/manifest+json",
        )

    @app.get("/sw.js")
    def service_worker():
        # Served from the root so it may handle every page.
        return FileResponse(static_dir / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})
