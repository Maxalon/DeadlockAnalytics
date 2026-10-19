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

    def status(self) -> dict:
        """/health: login, GC session, and the bot's remaining daily Steam budgets."""
        return self._request("GET", "/health", timeout=5)

    def history(self, account_id: int, pages: int | None = None) -> list[dict]:
        """`pages`: how many history pages to follow (one Steam request each); None: all."""
        params = {"pages": pages} if pages else None
        return self._request("GET", f"/history/{account_id}", params=params)["matches"]

    def match(self, match_id: int, account_id: int | None = None, steam: bool = True) -> dict:
        """account_id: whose history the match is from; the game sends it with the request too.
        steam=False: only if deadlock-api already has the salts; never spends the bot's budget."""
        params = {}
        if account_id:
            params["account"] = account_id
        if not steam:
            params["steam"] = 0
        return self._request("GET", f"/matches/{match_id}", params=params or None)

    def friends(self) -> dict:
        """The bot's open friend requests, friends and blocked accounts. Short timeout: a page waits on it."""
        return self._request("GET", "/friends", timeout=5)

    def friend_action(self, account_id: int, action: str) -> None:
        """accept / decline / remove / block / unblock. Requests are never accepted automatically."""
        self._request("POST", f"/friends/{account_id}/{action}", timeout=15)

    def decline_all(self) -> int:
        return self._request("POST", "/friends/decline-all", timeout=15)["declined"]
