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

CI builds `ghcr.io/maxalon/deadlockanalytics:latest` on every push to `main`. Two ways to get it
onto the box. Use either one or both; they don't conflict.

### First-time setup (both options)

```sh
sudo useradd -m -s /bin/sh deploy && sudo usermod -aG docker deploy
sudo install -d -o deploy -g deploy /srv/deadlockanalytics
sudo -u deploy git clone https://github.com/Maxalon/DeadlockAnalytics /srv/deadlockanalytics
sudo -u deploy cp /srv/deadlockanalytics/.env.example /srv/deadlockanalytics/.env   # set TRACKED_ACCOUNT_IDS
cd /srv/deadlockanalytics && sudo -u deploy docker compose up -d
```

If the GHCR package is private, log in once as the user that pulls (`deploy`), with a GitHub
token that only has `read:packages`: `sudo -u deploy docker login ghcr.io -u Maxalon`.
If the package is public, make it public once under the repo's *Packages* settings instead.
The checkout is updated with `git fetch` as `deploy`. That works anonymously while the repo is
public; if you make it private, add a read-only GitHub deploy key for the `deploy` user.

### Option A: push deploy from CI over Tailscale (about 1 min after merge)

The `deploy` job in `.github/workflows/ci.yml` runs after the image is pushed. It:
1. joins the tailnet as an ephemeral `tag:deadlock-deploy` node (`tailscale/github-action`);
2. SSHes to the box as `deploy`.

That key's `authorized_keys` entry forces `deploy/remote-deploy.sh`, which only fast-forwards
this checkout to `origin/main`, pulls the image, recreates the container and waits for the
healthcheck. The client cannot run anything else.

**Tailscale** (admin console). Anything else on your tailnet must keep working, so the
policy changes only add a narrowly scoped tag for this job:

1. Use a **new tag, `tag:deadlock-deploy`**. Don't reuse an existing `tag:ci`: it may already
   carry grants from earlier setups, and this job would inherit them.
2. Add it, plus the one grant it needs:
   ```json
   "tagOwners": { "tag:deadlock-deploy": ["autogroup:admin"] },
   "hosts":     { "homebox": "100.x.y.z" },
   "grants":    [ { "src": ["tag:deadlock-deploy"], "dst": ["homebox"], "ip": ["tcp:22"] } ]
   ```
   Merge these into your existing sections rather than replacing them.
3. **Only if** the policy still has the default allow-all rule (`"src": ["*"]`): grants only
   ever add access, so `*` would also hand the new tag your whole tailnet. Replace `"*"` with
   what it covers today **minus the new tag**: `"autogroup:member"`, plus every tag already in
   use (see *Machines*, filtered by tag, or your `tagOwners`), plus `"autogroup:shared"` if
   you share devices with other tailnets. Every existing device keeps exactly the access it
   has now. If there is no allow-all rule, skip this step.
4. Add `tests`. The admin console refuses to save a policy that fails them:
   ```json
   "tests": [
     { "src": "tag:deadlock-deploy", "accept": ["homebox:22"], "deny": ["homebox:443", "homebox:80"] }
   ]
   ```
   Add a line for each other flow you rely on, so a later edit can't silently break it.
5. Under *Settings → Trust credentials*, create a **new** OAuth client with the
   **Auth Keys: Write** scope and tag `tag:deadlock-deploy`.

**Box:**
- Generate a key pair: `ssh-keygen -t ed25519 -f deadlock-deploy -N "" -C github-deploy`.
- Add one line to `/home/deploy/.ssh/authorized_keys` (`.ssh` mode 700, file mode 600):
  ```
  restrict,command="/srv/deadlockanalytics/deploy/remote-deploy.sh" ssh-ed25519 AAAA... github-deploy
  ```
- If `sshd_config` uses `AllowUsers`, add `deploy`.
- From another tailnet device, run `ssh-keyscan -t ed25519 100.x.y.z` and keep the output line.

**GitHub** (*Settings → Environments → New environment* `production`; deployment branches: `main` only):

| Name | Kind | Value |
|---|---|---|
| `TS_OAUTH_CLIENT_ID` | secret | OAuth client ID |
| `TS_OAUTH_SECRET` | secret | OAuth client secret |
| `DEPLOY_SSH_KEY` | secret | contents of the `deadlock-deploy` private key |
| `DEPLOY_KNOWN_HOSTS` | secret | the `ssh-keyscan` line |
| `DEPLOY_HOST` | variable | the box's Tailscale IP, exactly as in the keyscan line |

Then delete the local private key. Until these are set, the job logs a notice and skips.

### Option B: pull timer on the box (no credentials outside the box, up to 15 min delay)

```sh
sudo cp deploy/deadlock-analytics-update.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now deadlock-analytics-update.timer
```

The service runs as `deploy`, so it uses the same checkout and GHCR login.

### Reverse proxy and backups

The container is `deadlock-analytics:8000` on the external `edge` Docker network; point
your reverse proxy's vhost at it.

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
