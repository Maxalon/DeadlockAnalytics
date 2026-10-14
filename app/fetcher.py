"""Client for the fetcher service (fetcher/): tracked players' match history and match files,
read from Valve through your own Steam bot account.

Its match JSON has the same shape as deadlock-api's /v1/matches/metadata, so jobs.store_match
stores either unchanged.
"""

import httpx


class FetcherError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code  # the fetcher's error code: not_ready, private, in_flight, gone, ...


class Fetcher:
    def __init__(self, base_url: str, client: httpx.Client | None = None):
        # A full history is many Game Coordinator requests, spaced out by the fetcher.
        self.client = client or httpx.Client(base_url=base_url, timeout=300)

    def _request(self, method: str, path: str, **kw) -> dict:
        try:
            resp = self.client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise FetcherError("unreachable", f"fetcher unreachable: {exc}") from exc
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code != 200:
            raise FetcherError(body.get("error", "http_error"), body.get("message") or f"HTTP {resp.status_code}")
        return body

    def ensure_ready(self) -> None:
        """Fail fast instead of letting each request wait for a GC session that isn't coming."""
        status = self._request("GET", "/health")
        if not status.get("gc_ready"):
            reason = status.get("last_error") or ("not logged in" if not status.get("logged_in") else "no GC session")
            raise FetcherError("not_ready", f"Steam bot not ready: {reason}")

    def history(self, account_id: int) -> list[dict]:
        return self._request("GET", f"/history/{account_id}")["matches"]

    def match(self, match_id: int) -> dict:
        return self._request("GET", f"/matches/{match_id}")

    def set_friends(self, account_ids: list[int]) -> None:
        """The tracked accounts: the bot accepts friend requests only from these."""
        self._request("PUT", "/friends", json={"account_ids": account_ids})
