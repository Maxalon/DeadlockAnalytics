"""FastAPI app: server-rendered pages, no client-side framework."""

import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import charts, queries, scoring
from .api import DeadlockApi
from .config import Settings
from .db import connect, init, transaction
from .scheduler import Scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
HERE = Path(__file__).parent
PAGE_SIZE = 50


def create_app(settings: Settings | None = None, api: DeadlockApi | None = None) -> FastAPI:
    settings = settings or Settings()
    setup = connect(settings.database_path)
    init(setup)
    with transaction(setup) as conn:
        for account_id in settings.seed_account_ids:
            conn.execute(
                "INSERT OR IGNORE INTO accounts (account_id, added_at) VALUES (?, ?)",
                (queries.parse_account_id(str(account_id)), int(time.time())),
            )
    if settings.database_path != ":memory:":
        setup.close()
    scheduler = Scheduler(settings, api or DeadlockApi(settings.api_base, settings.api_key))
    local = threading.local()

    def db():
        # sqlite3 connections must not be shared between FastAPI's worker threads.
        if settings.database_path == ":memory:":
            return setup
        if getattr(local, "conn", None) is None:
            local.conn = connect(settings.database_path)
        return local.conn

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.run_scheduler:
            scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(title="Deadlock Analytics", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def same_origin_posts(request: Request, call_next):
        # No login (Tailscale is the access control), so stop other sites from submitting
        # forms here through the browser: a cross-site POST carries a foreign Origin.
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return PlainTextResponse("Cross-site request refused", status_code=403)
        return await call_next(request)

    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["labels"] = scoring.LABELS
    templates.env.filters["dt"] = lambda ts: (
        datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC") if ts else "—"
    )
    templates.env.filters["mins"] = lambda s: f"{s // 60}:{s % 60:02d}"
    templates.env.filters["num"] = lambda v: "—" if v is None else f"{v:,.0f}"
    templates.env.filters["signed"] = lambda v, d=2: "—" if v is None else f"{v:+.{d}f}"

    def render(request: Request, name: str, **ctx):
        ctx.setdefault("heroes", queries.heroes(db()))
        ctx["theme"] = "light" if request.cookies.get("theme") == "light" else "dark"
        return templates.TemplateResponse(request, name, ctx)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return render(
            request,
            "index.html",
            accounts=queries.accounts(db()),
            jobs=queries.job_runs(db()),
            snapshot=queries.latest_snapshot(db()),
        )

    @app.get("/accounts", response_class=HTMLResponse)
    def accounts_page(request: Request, error: str | None = None):
        return render(request, "accounts.html", accounts=queries.accounts(db()), error=error)

    @app.post("/accounts")
    def add_account(account: str = Form(...)):
        try:
            account_id = queries.parse_account_id(account)
        except ValueError as exc:
            return RedirectResponse(f"/accounts?error={quote(str(exc))}", status_code=303)
        with transaction(db()) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO accounts (account_id, added_at) VALUES (?, ?)", (account_id, int(time.time()))
            )
        scheduler.request_sync()
        return RedirectResponse("/accounts", status_code=303)

    @app.post("/accounts/{account_id}/delete")
    def remove_account(account_id: int):
        # Stops tracking; already-stored matches stay (other tracked players may share them).
        with transaction(db()) as conn:
            conn.execute("DELETE FROM accounts WHERE account_id = ?", (account_id,))
        return RedirectResponse("/accounts", status_code=303)

    @app.post("/sync")
    def sync_now():
        scheduler.request_sync()
        return RedirectResponse("/", status_code=303)

    @app.get("/players/{account_id}", response_class=HTMLResponse)
    def player(request: Request, account_id: int, page: int = 1):
        account = db().execute("SELECT * FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
        page = max(page, 1)
        matches = queries.player_matches(db(), settings.database_path, account_id, PAGE_SIZE, (page - 1) * PAGE_SIZE)
        if account is None and not matches:
            raise HTTPException(404, "Account not tracked")
        trend = list(reversed(queries.player_matches(db(), settings.database_path, account_id, 100)))
        return render(
            request,
            "player.html",
            account=account,
            account_id=account_id,
            matches=matches,
            page=page,
            page_size=PAGE_SIZE,
            heroes_played=queries.hero_summary(db(), account_id),
            chart=charts.score_trend(trend),
        )

    @app.get("/matches/{match_id}", response_class=HTMLResponse)
    def match(request: Request, match_id: int):
        data = queries.scored_match(db(), settings.database_path, match_id)
        if data is None:
            raise HTTPException(404, "Match not stored (only matches of tracked accounts are)")
        tracked = {r["account_id"] for r in db().execute("SELECT account_id FROM accounts")}
        return render(request, "match.html", tracked=tracked, **data)

    @app.get("/model", response_class=HTMLResponse)
    def model(request: Request):
        snap = queries.latest_snapshot(db())
        table = queries.snapshot_table(db(), snap["snapshot_id"]) if snap else []
        return render(request, "model.html", snapshot=snap, table=table, stats=scoring.STATS)

    @app.get("/theme")
    def set_theme(to: str = "dark", next: str = "/"):
        # Only same-site paths, so this can't be used as an open redirect.
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        resp = RedirectResponse(target, status_code=303)
        resp.set_cookie("theme", "light" if to == "light" else "dark", max_age=365 * 86400, samesite="lax")
        return resp

    @app.get("/healthz")
    def healthz():
        db().execute("SELECT 1")
        return {"ok": True}

    return app
