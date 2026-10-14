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

    def _get(self, path: str, params: dict) -> httpx.Response:
        for attempt in range(5):
            resp = self.client.get(path, params=params)
            if resp.status_code == 429 or resp.status_code >= 500:
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

    def steam_profiles(self, account_ids: list[int]) -> list[dict]:
        if not account_ids:
            return []
        return self._get("/v1/players/steam", {"account_ids": ",".join(map(str, account_ids))}).json()
