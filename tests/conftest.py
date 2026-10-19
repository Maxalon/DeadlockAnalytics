import json
from pathlib import Path

import pytest

from app import db as dbmod
from app import jobs

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def match_payload():
    return json.loads((FIXTURES / "match_115701682.json").read_text())


@pytest.fixture
def research_baselines():
    rows = json.loads((FIXTURES / "baselines_2026-10-13.json").read_text())["rows"]
    return {(r["hero_id"], r["stat"]): (r["mean"], r["sd"]) for r in rows}


@pytest.fixture
def conn():
    c = dbmod.connect(":memory:")
    dbmod.init(c)
    yield c
    c.close()


def insert_snapshot(conn, rows, window_end=1791900000):
    with dbmod.transaction(conn):
        sid = conn.execute(
            "INSERT INTO baseline_snapshots (computed_at, window_start, window_end, n_matches) VALUES (?, ?, ?, ?)",
            (window_end, window_end - 7 * 86400, window_end, 1000),
        ).lastrowid
        conn.executemany(
            "INSERT INTO hero_baselines (snapshot_id, hero_id, stat, n, mean, sd) VALUES (?, ?, ?, ?, ?, ?)",
            [(sid, h, s, 1000, m, sd) for (h, s), (m, sd) in rows.items()],
        )
    return sid


class FakeApi:
    """Stands in for DeadlockApi; records calls."""

    def __init__(self, pages=None, baseline_pages=None, profiles=None, badges=None):
        self.pages = list(pages or [])
        self.profiles = profiles  # account ids deadlock-api has a profile for; None: all
        self.badges = dict(badges or {})  # account_id -> (badge, start of their latest ranked match)
        self.rank_calls = []
        self.baseline_pages = list(baseline_pages or [])
        self.calls = []

    def matches(self, **filters):
        self.calls.append(filters)
        source = self.pages if "account_ids" in filters else self.baseline_pages
        return source.pop(0) if source else []

    def heroes(self):
        return [
            {"id": 3, "name": "Vindicta", "images": {"icon_image_small_webp": "https://example/v.webp"}},
            {"id": 77, "name": "Apollo", "images": {}},
        ]

    def steam_profiles(self, ids):
        return [
            {"account_id": i, "personaname": f"player{i}", "avatarmedium": None}
            for i in ids
            if self.profiles is None or i in self.profiles
        ]

    def player_ranks(self, ids):
        self.rank_calls.append(list(ids))
        out = []
        for i in ids:
            badge, at = self.badges.get(i, (0, None))
            out.append({"account_id": i, "badge": badge, "last_match": {"start_time": at} if at else None})
        return out

    def ranks(self):
        return [
            {"tier": 0, "name": "Obscurus", "images": {}},
            {"tier": 6, "name": "Ritualist", "images": {"large_webp": "https://example/r6.webp"}},
            {"tier": 9, "name": "Phantom", "images": {}},
        ]


@pytest.fixture
def stored_match(conn, match_payload):
    with dbmod.transaction(conn):
        jobs.store_match(conn, match_payload[0])
    return match_payload[0]


@pytest.fixture
def fetcher_match():
    """Match 115701682 as the fetcher returns it (decoded from Valve's match file)."""
    return json.loads((FIXTURES / "fetcher_match_115701682.json").read_text())


class FakeFetcher:
    """Stands in for app.fetcher.Fetcher."""

    def __init__(self, history=(), matches=None, ready=True, errors=None, api_has=()):
        self._history = list(history)
        self.matches = dict(matches or {})
        self.ready = ready
        self.errors = dict(errors or {})  # match_id -> FetcherError (when the bot is asked)
        self.api_has = set(api_has)  # matches deadlock-api has salts for
        self.fetched = []  # (match_id, account_id, steam) per match request
        self.history_pages = []
        self.actions = []
        self.bot = {"logged_in": True, "incoming": [], "friends": [], "blocked": []}
        self.steam_profiles = set()  # accounts Steam answers for

    def ensure_ready(self):
        from app.fetcher import FetcherError

        if not self.ready:
            raise FetcherError("not_ready", "Steam bot not ready: not logged in")

    def history(self, account_id, pages=None):
        self.history_pages.append(pages)
        return self._history

    def match(self, match_id, account_id=None, steam=True):
        from app.fetcher import FetcherError

        self.fetched.append((match_id, account_id, steam))
        if match_id in self.api_has:
            return self.matches[match_id]
        if not steam:
            raise FetcherError("no_salts", f"deadlock-api has no salts for {match_id} (yet)")
        if match_id in self.errors:
            raise self.errors[match_id]
        return self.matches[match_id]

    def status(self):
        budget = {"limit": 40, "remaining": 38, "next_free_at": None}
        return {
            "logged_in": True,
            "gc_ready": True,
            "salts_budget": budget,
            "history_budget": {**budget, "limit": 20, "remaining": 17},
        }

    def profiles(self, ids):
        self.profile_calls = getattr(self, "profile_calls", []) + [list(ids)]
        return [{"account_id": i, "name": f"steam{i}", "avatar_url": None} for i in ids if i in self.steam_profiles]

    def friends(self):
        return self.bot

    def friend_action(self, account_id, action):
        self.actions.append((account_id, action))

    def decline_all(self):
        self.actions.append(("all", "decline"))
        return len(self.bot["incoming"])
