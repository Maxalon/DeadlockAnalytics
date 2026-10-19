# DeadlockAnalytics

A self-hosted website with continuously updated analytics and game statistics for the
Deadlock accounts the operator configures.

What it shows, per tracked account:

- every Normal-mode (ranked + unranked) match since 2026-01-06, when Valve started reporting MVP ranks
- a reconstruction of **Valve's MVP score** for every player in each match, broken down by stat,
  next to Valve's actual MVP / Key Player result. The model is explained on the `/model` page
  and derived in `research/mvp-115701682/REPORT.md`
- your score against the match's MVP score over time, and per-hero results

## How it works

A FastAPI app with server-rendered pages and a background thread, plus a SQLite file on a
volume. Optionally a second container, the fetcher (`fetcher/`), reads tracked players' matches
from Valve through a Steam account of your own that the players add as a friend.

| Job | Default interval | What it does |
|---|---|---|
| Account sync | 15 min | With the fetcher: completes history-only matches whose salts deadlock-api already has (free). Without it: fetches new matches of every tracked account (all 12 players) from `api.deadlock-api.com/v1/matches/metadata` |
| Hero baselines | daily | Samples 30,000 recent matches spread over the last 7 days and stores per-hero mean/SD for each scored stat |
| Heroes / profiles | daily / each sync | Hero names and icons; Steam persona names and avatars |

Each match is scored against the baseline snapshot computed right after it, so old matches
keep the baselines of their own patch. Baselines always come from
[deadlock-api.com](https://deadlock-api.com). Without the fetcher, tracked matches do too, but
it only has the matches it happened to collect, usually 30–90 minutes after they end.

With the fetcher, a player's whole match history is stored at once; the match files (all 12
players, damage, healing, MVP ranks) follow. Downloading those needs a per-match key ("salt")
that Steam hands out sparingly, so the fetcher asks deadlock-api for it first, which already has
the salts of most matches. The bot itself keeps to a daily budget per Steam account like
deadlock-api's ingest tool (40 salts, 20 history pages per 24 h) and only spends it on request:
a player's history when you press "Re-pull matches" (the newest page; all pages when the player
is added), a match's file when you open a match that has no details yet. `/accounts` shows
what's left. Salts the bot fetches are handed to deadlock-api in return (`CONTRIBUTE_SALTS=0`
turns that off).

Match pages show every player's Steam name, avatar and rank. Names come from deadlock-api,
or for players it doesn't know yet from Steam through the bot's ordinary Steam connection (not
Deadlock's Game Coordinator, so outside the daily budget). Ranks: the badge a player entered a
ranked match with (from the match file); otherwise their rank after their latest ranked match
from deadlock-api, kept on the match when looked up within two days of it, else shown as "now".
Unranked matches carry no ranks of their own. The page also notes the matchmaking pool a match
was made in (low-priority, new-player, high-skill parties).

Accounts are added and removed at `/accounts` (SteamID3, SteamID64 or a
`steamcommunity.com/profiles/…` link). There is no login, so run it on a private network or
behind an authenticating proxy.

## Running it

CI publishes `ghcr.io/maxalon/deadlockanalytics:latest` on every push to `main`.

```sh
cp .env.example .env    # all settings are optional and documented there
docker compose up -d
```

`compose.yml` publishes no ports: it joins an external Docker network named `edge` for a
reverse proxy. Adjust that to your setup. Data lives in the `deadlock_data` volume.

The fetcher needs a Steam account with Deadlock access, logged in once with
`docker compose run --rm -it fetcher npm run login`. Players then send it a friend request, which
you accept, decline or block at `/accounts`; the bot never accepts one on its own.
Until then the site falls back to deadlock-api, and `/accounts` shows which source each sync used.

To keep the container up to date, either enable the systemd timer in `deploy/`, or use the
optional `deploy` job in CI, which runs `deploy/remote-deploy.sh` on the host over SSH. That job
skips itself unless its secrets are configured.

## Development

```sh
pip install -e ".[dev]"
pytest
ruff check app tests && ruff format --check app tests
DATABASE_PATH=./dev.sqlite3 uvicorn app.main:create_app --factory --reload
```

Settings are documented in `.env.example`.
