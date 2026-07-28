#!/usr/bin/env bash
# Exporta os volumes do stack para ./backup/*.tgz (rodar com o stack parado
# para consistencia do TSDB: docker compose stop prometheus grafana).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backup

for vol in pt-obs-prom-data pt-obs-grafana-data pt-obs-caddy-data pt-obs-loki-data; do
  echo "exportando ${vol}..."
  docker run --rm -v "${vol}":/data -v "${PWD}/backup":/backup alpine \
    tar czf "/backup/${vol}.tgz" -C /data .
done

echo "backups em ./backup/ ; copie para o novo host com scp/rsync"
