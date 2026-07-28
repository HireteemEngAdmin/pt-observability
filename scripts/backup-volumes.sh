#!/usr/bin/env bash
# Export the stack's volumes to ./backup/*.tgz.
#
# Run with the stack stopped, otherwise the TSDB and the Loki chunks are
# captured mid-write:
#   docker compose stop prometheus loki grafana
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backup

for vol in pt-obs-prom-data pt-obs-grafana-data pt-obs-caddy-data pt-obs-loki-data; do
  echo "exporting ${vol}..."
  docker run --rm -v "${vol}":/data -v "${PWD}/backup":/backup alpine \
    tar czf "/backup/${vol}.tgz" -C /data .
done

echo "backups in ./backup/ ; copy them to the new host with scp/rsync"
