# DeadlockAnalytics

A self-hosted website with continuously updated analytics and game statistics for the
Deadlock accounts the operator configures. Built to run on a home server behind a
reverse proxy, reachable over Tailscale only.

What it shows, per tracked account:

- every Normal-mode (ranked + unranked) match since 2026-01-06, when Valve started reporting MVP ranks
- a reconstruction of **Valve's MVP score** for every player in each match, broken down by stat,
  next to Valve's actual MVP / Key Player result. The model is explained on the `/model` page
  and derived in `research/mvp-115701682/REPORT.md`
- your score against the match's MVP score over time, and per-hero results

## How it works

One container: a FastAPI app with server-rendered pages and a background thread, plus a SQLite
file on a volume.

| Job | Default interval | What it does |
|---|---|---|
| Account sync | 15 min | Fetches new matches of every tracked account (all 12 players) from `api.deadlock-api.com/v1/matches/metadata` |
| Hero baselines | daily | Samples 30,000 recent matches spread over the last 7 days and stores per-hero mean/SD for each scored stat |
| Heroes / profiles | daily / each sync | Hero names and icons; Steam persona names and avatars |

Each match is scored against the baseline snapshot computed right after it, so old matches
keep the baselines of their own patch. Data source: [deadlock-api.com](https://deadlock-api.com).
Matches usually appear there 30–90 minutes after they end.

Accounts: set `TRACKED_ACCOUNT_IDS` for the first start, then add or remove accounts at
`/accounts` (SteamID3, SteamID64 or a `steamcommunity.com/profiles/…` link). There is no
login: access control is Tailscale.

## Deploy (home box)

CI builds `ghcr.io/maxalon/deadlockanalytics:latest` on every push to `main`. The box pulls
it itself, so nothing outside can reach into the server and no self-hosted runner is involved.

```sh
sudo git clone https://github.com/Maxalon/DeadlockAnalytics /srv/deadlockanalytics
cd /srv/deadlockanalytics
cp .env.example .env            # set TRACKED_ACCOUNT_IDS
docker compose up -d

# automatic updates
sudo cp deploy/deadlock-analytics-update.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now deadlock-analytics-update.timer
```

If the repository (and so the GHCR package) is private, log the box in once with a
GitHub token that only has `read:packages`:
`echo <token> | docker login ghcr.io -u Maxalon --password-stdin`.
If it is public, make the package public once under the repo's *Packages* settings instead.

Reverse proxy: the container is `deadlock-analytics:8000` on the external `edge` Docker
network; point your proxy's vhost at it.

Back up the `deadlock_data` volume if you care about history. Everything in it can be
re-fetched, but a full backfill takes a while for many accounts.

## Development

```sh
pip install -e ".[dev]"
pytest
ruff check app tests && ruff format --check app tests
DATABASE_PATH=./dev.sqlite3 TRACKED_ACCOUNT_IDS=<id> uvicorn app.main:create_app --factory --reload
```

Settings are documented in `.env.example`.
