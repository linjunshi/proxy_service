#!/usr/bin/env bash
# Seeds the application state volume from state/wmsxwd-home.tar.gz on a fresh
# deployment, before the first `docker compose up`. Refuses to overwrite an
# existing state so a live deployment cannot be clobbered by accident.
set -euo pipefail
cd "$(dirname "$0")/.."

archive="state/wmsxwd-home.tar.gz"
if [ ! -f "${archive}" ]; then
    echo "Missing ${archive} — produce it with scripts/export-state.sh." >&2
    exit 1
fi

docker volume create proxy-service_wmsxwd_home >/dev/null

docker run --rm \
    -v proxy-service_wmsxwd_home:/home/app \
    -v "${PWD}/state:/in:ro" \
    debian:12-slim \
    bash -ec '
        if [ -e /home/app/.local ]; then
            echo "Volume already holds application state; delete it first:" >&2
            echo "  docker compose down && docker volume rm proxy-service_wmsxwd_home" >&2
            exit 1
        fi
        tar -C /home/app -xzf /in/wmsxwd-home.tar.gz
        chown -R 10000:10000 /home/app
    '

echo "State imported. Run: docker compose up -d"
