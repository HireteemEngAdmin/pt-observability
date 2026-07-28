#!/usr/bin/env bash
# Restore ./backup/*.tgz into the named volumes. Run this BEFORE the first
# docker compose up -d on the new host.
set -euo pipefail
cd "$(dirname "$0")/.."

for vol in pt-obs-prom-data pt-obs-grafana-data pt-obs-caddy-data pt-obs-loki-data; do
  [ -f "backup/${vol}.tgz" ] || { echo "missing backup/${vol}.tgz"; exit 1; }
  docker volume create "${vol}" >/dev/null
  echo "restoring ${vol}..."
  docker run --rm -v "${vol}":/data -v "${PWD}/backup":/backup alpine \
    sh -c "cd /data && tar xzf /backup/${vol}.tgz"
done

echo "volumes restored; now run: docker compose up -d"
