#!/usr/bin/env bash
# Snapshots the application state volume (login session, selected node,
# settings) into state/wmsxwd-home.tar.gz so a production deploy starts
# pre-configured. The archive contains live credentials: ship it alongside
# vendor/*.deb, never commit it.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p state
echo "Stopping the GUI container for a consistent snapshot..."
docker compose stop wmsxwd >/dev/null

docker run --rm \
    -v proxy-service_wmsxwd_home:/home/app:ro \
    -v "${PWD}/state:/out" \
    debian:12-slim \
    tar -C /home/app -czf /out/wmsxwd-home.tar.gz .

docker compose start wmsxwd >/dev/null
echo "Wrote state/wmsxwd-home.tar.gz — treat it like a credential file."
