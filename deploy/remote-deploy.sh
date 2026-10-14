#!/bin/sh
# Forced command for the `deploy` user's SSH key, set in authorized_keys as
#   restrict,command="/srv/deadlockanalytics/deploy/remote-deploy.sh" ssh-ed25519 AAAA...
# Whatever command the SSH client sends is ignored. The only thing this key can do is
# fast-forward the checkout to origin/main and recreate the container from the image CI
# just pushed, so a leaked key can at worst redeploy what is already on main.
set -eu

main() {
    cd "$(dirname "$0")/.."
    git fetch --quiet origin main
    git merge --quiet --ff-only origin/main
    docker compose pull --quiet
    docker compose up -d --remove-orphans

    status=missing
    for _ in $(seq 1 45); do
        status=$(docker inspect -f '{{.State.Health.Status}}' deadlock-analytics 2>/dev/null || echo missing)
        if [ "$status" = healthy ]; then
            echo "deadlock-analytics healthy at $(git rev-parse --short HEAD)"
            exit 0
        fi
        sleep 2
    done
    echo "deadlock-analytics did not become healthy (status: $status)" >&2
    docker compose logs --tail 50 web >&2
    exit 1
}

# Everything runs inside main(), which the shell has fully read before the git merge can
# rewrite this file, so updating the script itself is safe.
main
