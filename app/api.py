"""Thin client for https://api.deadlock-api.com.

Everything goes through the bulk `/v1/matches/metadata` endpoint (30 req/min per IP),
which serves both the tracked players' matches and the random sample used for hero
baselines. `/v1/sql` is avoided on purpose: it is deprecated and limited to 2 req/min.
"""

import json
import time

import httpx

# Per-player columns we store. The max_* columns are the last value of Valve's cumulative
# stat time series, i.e. the final match totals (verified equal to final_stats).
PLAYER_COLUMNS = (
    "hero_id,team,net_worth,max_player_damage,max_boss_damage,max_teammate_healing,mvp_rank,abandon_match_time_s"
)


class DeadlockApi:
    def __init__(self, base_url: str, api_key: str = "", client: httpx.Client | None = None):
        headers = {"User-Agent": "DeadlockAnalytics (self-hosted)"}
        if api_key:
            headers["X-API-KEY"] = api_key
        self.client = client or httpx.Client(base_url=base_url, headers=headers, timeout=180)

    def _get(self, path: str, params: dict, attempts: int = 5) -> httpx.Response:
        """`attempts`: tries on 429/5xx, waiting longer each time; 1 for what a page waits on."""
        for attempt in range(attempts):
            resp = self.client.get(path, params=params)
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < attempts - 1:
                time.sleep(min(60, 5 * 2**attempt))
                continue
            resp.raise_for_status()
            return resp
        resp.raise_for_status()
        return resp

    def matches(self, **filters) -> list[dict]:
        """Normal-mode ranked+unranked matches with K/D/A and the columns above for all players."""
        params = {
            "game_mode": "normal",
            "include_player_kda": "true",
            "extra_player_columns": PLAYER_COLUMNS,
            "format": "ndjson",
        }
        for key, value in filters.items():
            if value is None:
                continue
            params[key] = ",".join(map(str, value)) if isinstance(value, list | tuple) else value
        resp = self._get("/v1/matches/metadata", params)
        return [json.loads(line) for line in resp.text.splitlines() if line.strip()]

    def heroes(self) -> list[dict]:
        return self._get("/v1/assets/heroes", {"only_active": "true"}).json()

    def ranks(self) -> list[dict]:
        return self._get("/v1/assets/ranks", {}).json()

    def steam_profiles(self, account_ids: list[int]) -> list[dict]:
        if not account_ids:
            return []
        return self._get("/v1/players/steam", {"account_ids": ",".join(map(str, account_ids))}).json()

    def player_hero_stats(self, account_ids: list[int], max_unix_timestamp: int | None = None) -> list[dict]:
        """Per player and hero: matches_played, wins, ... over the matches deadlock-api has of them,
        optionally only those that started up to `max_unix_timestamp`."""
        if not account_ids:
            return []
        params = {"account_ids": ",".join(map(str, account_ids))}
        if max_unix_timestamp is not None:
            params["max_unix_timestamp"] = max_unix_timestamp
        return self._get("/v1/players/hero-stats", params).json()

    def player_ranks(self, account_ids: list[int]) -> list[dict]:
        """Each player's rank after their latest ranked match: `badge` (tier*10 + subrank, 0 if none
        is known) and `last_match`. Protected accounts are left out."""
        if not account_ids:
            return []
        return self._get("/v1/players/rank", {"account_ids": ",".join(map(str, account_ids))}).json()

    # Game data (datamined from the game files by deadlock-api, rebuilt for each game update).
    def steam_info(self) -> dict:
        """The game's current version: client_version, version_datetime, ..."""
        return self._get("/v1/assets/steam-info", {}).json()

    def asset_items(self) -> list[dict]:
        return self._get("/v1/assets/items", {}).json()

    def asset_heroes(self) -> list[dict]:
        return self._get("/v1/assets/heroes", {"only_active": "true"}).json()

    def generic_data(self) -> dict:
        return self._get("/v1/assets/generic-data", {}).json()

    def patches(self) -> list[dict]:
        return self._get("/v1/patches", {}, attempts=1).json()

    # Builds people made in the game (Valve's build browser, mirrored by deadlock-api).
    def builds(self, **filters) -> list[dict]:
        params = {k: v for k, v in filters.items() if v not in (None, "")}
        return self._get("/v1/builds", params, attempts=1).json()

    def build(self, hero_id: int, build_id: int) -> dict:
        return self._get(f"/v1/builds/{hero_id}/{build_id}", {}, attempts=1).json()

    def builds_by_author(self, account_id: int) -> list[dict]:
        """All builds of a player, asked from Valve live (20 req/min per IP: only on request)."""
        return self._get(f"/v1/builds/by-author/{account_id}", {}, attempts=1).json()
